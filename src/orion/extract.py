# =============================================================================
# extract.py
# -----------------------------------------------------------------------------
# Responsible for: The conditional LLM step for the DISCIPLINES signal — reading a
#                  project's own instruction/design/decision doc and faithfully
#                  reframing the principles its author already stated into short
#                  cards ({title, why, scope}).
# Role in project: The doc-centric "Disciplines & directions" collector (E2 Inc 4
#                  slice 4b) calls this once per doc, cache-gated, so the model runs
#                  only when a doc changes. The seam mirrors summarize.py's
#                  `Summarizer`: a provider-agnostic `DisciplineExtractor` Protocol
#                  with one `extract(text, *, source) -> tuple[Discipline, ...]`
#                  method; cli.py builds the configured backend and injects it.
# Observe-not-originate: this is OBSERVING — the model only reframes principles the
#                  author explicitly stated; it must never invent. The `source`
#                  (which doc a principle came from) is stamped by the CALLER and
#                  passed in here, never chosen by the model, so "observed · <doc>"
#                  on the dashboard is a literally honest claim.
# Security note: the model is the weakest layer and is never trusted as a control.
#                  The caller redacts each doc BEFORE it reaches the model, the
#                  backend is told to report principles (not code/secrets), and a
#                  redaction net runs again on the extracted text in the CLI.
# =============================================================================

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

import anthropic

# Extraction can return several cards from one doc, each a sentence or two, so it
# needs a larger budget than a single summary. Still bounded to keep cost down and
# stay under the SDK's non-streaming timeout guard.
MAX_TOKENS = 2048

# The two scopes a principle can carry. "global" is a convention that holds across
# all of the author's work; "project" is specific to the documented project. The
# serializer uses this to split the dashboard's Global group from per-project ones.
SCOPES = ("global", "project")

# Bump when the extraction PROMPT (or output contract) changes in a way that should
# re-extract unchanged docs. The collector folds this into its content-hash cache key,
# so a prompt tune busts the cache instead of serving stale cards from before the change
# (the cache otherwise keys on doc CONTENT only, which a prompt edit does not touch).
CACHE_VERSION = "2"


class ExtractError(Exception):
    """Raised when the extractor cannot produce a usable list of disciplines.

    Why:
        Lets the collector catch an extraction failure for ONE doc specifically and
        skip just that doc (fail-soft) rather than losing every project's
        disciplines, while a real misconfiguration (e.g. an unknown provider) still
        surfaces. Wrapping the SDK's exceptions in our own type also keeps the rest
        of the codebase from importing anthropic.
    """


@dataclass(frozen=True)
class Discipline:
    """One observed working principle, reframed as a dashboard card.

    Args:
        title: A short noun phrase naming the principle (e.g. "Untrusted text is
            inert"). Reframed by the model from the author's own words.
        why: One or two plain sentences paraphrasing the author's stated rationale,
            grounded in the doc — never invented.
        scope: One of SCOPES ("global" | "project"). Classified by the model:
            "global" for a convention spanning all the author's work, "project" for
            one specific to this documented project.
        source: The repo-relative path of the doc this principle was observed in
            (e.g. "CLAUDE.md"). STAMPED BY THE CALLER and passed into extract(), so
            it is deterministic and never model-chosen — that is what makes the
            dashboard's "observed · <source>" footer an honest claim.

    Why:
        A tiny frozen record (rather than a bare dict) makes each field explicit at
        every use site and gives a clean wire/cache shape. It is the producer-side
        twin of the relay's discipline JSON; `as_dict` is the single serialization
        point both the push payload and the extraction cache reuse (DRY).
    """

    title: str
    why: str
    scope: str
    source: str

    def as_dict(self) -> dict:
        """Serialize to the wire/cache dict ({title, why, scope, source}).

        Returns:
            A plain dict with all four fields, the shape push_disciplines sends and
            the extraction cache stores.

        Why:
            One serialization point keeps the push payload and the cache byte-shape
            in agreement, so a cache hit reconstructs exactly what a fresh extraction
            would have pushed.
        """
        return {
            "title": self.title,
            "why": self.why,
            "scope": self.scope,
            "source": self.source,
        }


# The security- and faithfulness-relevant instructions, shared by every backend
# (DRY) exactly as summarize.py shares its system prompt. The emphasis is on
# OBSERVING: reframe only what the author stated, invent nothing.
_SYSTEM_PROMPT = (
    "You read a developer's own project documentation (instruction files, design "
    "notes, decision records) and extract the SUBSTANTIVE working PRINCIPLES and "
    "CONVENTIONS the author has explicitly stated, reframing each as a short card to "
    "make the work legible to an outside reader.\n"
    "You are OBSERVING and faithfully reframing the author's own stated principles. "
    "You must NOT invent, infer, or add principles the author did not state.\n"
    "Rules:\n"
    "- Extract only principles that are explicitly stated or clearly asserted in the "
    "provided text. If the text states none, return an empty array.\n"
    "- Focus on the DEFINING principles an outside reader should understand: "
    "architecture, privacy/security stance, product philosophy, engineering values, "
    "and how the work is built. SKIP granular implementation and styling details "
    "(exact fonts, pixel sizes, specific component layouts, individual UI labels) and "
    "routine boilerplate — those are not disciplines.\n"
    "- `title`: a short noun phrase naming the principle (about 2-6 words).\n"
    "- `why`: one or two sentences paraphrasing the author's stated rationale, "
    "grounded in the text. Do not invent reasons.\n"
    "- `scope`: \"global\" for a CROSS-CUTTING principle — one about how software or "
    "products are built in general, or how this developer works across projects (e.g. "
    "privacy and security practices, architectural philosophy, engineering values). "
    "\"project\" for a principle specific to THIS project's particular domain, feature "
    "set, or structure.\n"
    "- Never reproduce code, file contents, secrets, keys, or tokens.\n"
    "- Use clean prose: no em-dashes, no semicolons. Avoid generic LLM filler and "
    "stock phrasings (e.g. 'delve', 'leverage' to mean 'use', 'seamless').\n"
    "Return ONLY a JSON array of objects, each {\"title\": str, \"why\": str, "
    "\"scope\": \"global\"|\"project\"}. No prose, no code fence, no preamble. "
    "An empty array is a valid answer."
)


def _parse_disciplines(raw: str, source: str) -> tuple[Discipline, ...]:
    """Parse a model response into validated Disciplines, stamping `source`.

    Args:
        raw: The model's reply — expected to be a JSON array of
            {title, why, scope} objects (a leading/trailing code fence tolerated).
        source: The repo-relative doc path to stamp on every returned Discipline.
            The model never supplies this; the caller owns it.

    Returns:
        The validated Disciplines, in the model's order. Malformed individual items
        (missing fields, unknown scope) are SKIPPED (fail-soft per item), mirroring
        the collectors' "anything that does not match is ignored" tolerance.

    Raises:
        ExtractError when the whole response is not a JSON array — a contract break,
        not a single bad item.

    Why:
        Validation lives in one place so the Anthropic backend (and any future one)
        share the same shape guarantees. We stamp `source` here, at the boundary
        where the trusted caller value meets the untrusted model output, so a
        Discipline can never carry a model-chosen source.
    """
    text = raw.strip()
    # Tolerate a ```json ... ``` fence some models add despite the instruction.
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else ""
        if text.endswith("```"):
            text = text[: -len("```")]
        text = text.strip()

    try:
        items = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExtractError(f"Extractor returned non-JSON output: {exc}") from exc
    if not isinstance(items, list):
        raise ExtractError("Extractor output was not a JSON array.")

    disciplines: list[Discipline] = []
    for item in items:
        # Skip anything that is not a well-formed {title, why, scope} object: a
        # single malformed card must not abort the doc's whole extraction.
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        why = item.get("why")
        scope = item.get("scope")
        if not isinstance(title, str) or not title.strip():
            continue
        if not isinstance(why, str) or not why.strip():
            continue
        if scope not in SCOPES:
            continue
        disciplines.append(
            Discipline(
                title=title.strip(),
                why=why.strip(),
                scope=scope,
                source=source,
            )
        )
    return tuple(disciplines)


class DisciplineExtractor(Protocol):
    """The provider-agnostic disciplines-extraction seam (E2 Inc 4 slice 4b).

    Why:
        Mirrors summarize.py's `Summarizer`: the one interface the collector depends
        on. A Protocol (structural typing) means a backend just needs the method —
        no base class to import — which keeps backends self-contained and makes a
        fake trivial to inject in tests (no network, no API key).
    """

    def extract(self, doc_text: str, *, source: str) -> tuple[Discipline, ...]:
        """Extract the disciplines stated in one (already-redacted) doc.

        Args:
            doc_text: The doc's text, ALREADY REDACTED by the caller. Backends never
                redact.
            source: The repo-relative path to stamp on every returned Discipline.

        Returns:
            The observed Disciplines (possibly empty). Backends raise ExtractError on
            an API failure or an unparseable response.
        """
        ...


class AnthropicDisciplineExtractor:
    """Disciplines extractor backed by the Anthropic Messages API (the default).

    Args:
        client: An anthropic.Anthropic instance, injected by the caller — same as
            AnthropicSummarizer, so secret handling stays in the CLI and tests can
            substitute a fake client (no network, no API key).
        model: The model id to request (Haiku via config — the lightest model
            adequate for this extraction/classification task).

    Why:
        Extraction is a structured read of already-written content, so it reuses the
        exact Anthropic plumbing the summarizer uses (plain, non-streaming Messages
        request; APIError -> ExtractError). The difference is the system prompt
        (faithful extraction, not narration) and that the reply is parsed as JSON.
    """

    def __init__(self, client: anthropic.Anthropic, model: str) -> None:
        self._client = client
        self._model = model

    def extract(self, doc_text: str, *, source: str) -> tuple[Discipline, ...]:
        """Extract disciplines from one redacted doc via the Anthropic API.

        Args:
            doc_text: The ALREADY-REDACTED doc text.
            source: The repo-relative doc path to stamp on each Discipline.

        Returns:
            The observed Disciplines (possibly empty).

        Why:
            See the class docstring. We collect the text blocks from the response
            (as the summarizer does) and hand them to _parse_disciplines, which
            validates the shape and stamps `source`. Any APIError becomes an
            ExtractError so the collector can fail soft on this one doc.
        """
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=MAX_TOKENS,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": doc_text}],
            )
        except anthropic.APIError as exc:
            # APIError is the base for status/connection/timeout errors — one catch
            # covers them all; the SDK already retried transient failures.
            raise ExtractError(f"Anthropic API call failed: {exc}") from exc

        reply = "".join(
            block.text
            for block in response.content
            if getattr(block, "type", None) == "text"
        )
        return _parse_disciplines(reply, source)

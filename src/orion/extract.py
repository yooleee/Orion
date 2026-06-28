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


# =============================================================================
# SKILLS extraction (E2 Inc 4 slice 4c — the "skills comb" / partial living resume)
# -----------------------------------------------------------------------------
# The same observe-not-originate seam as disciplines, but the INPUT is a per-project
# EVIDENCE BUNDLE (languages from tracked files, recent commit subjects, doc focus)
# rather than one prose doc, and the OUTPUT is competency cards the relay later merges
# ACROSS projects into the comb. The model reframes ONLY what the evidence supports —
# it must never list an aspirational or unevidenced skill (the resume-puffery guard).
# =============================================================================

# The closed set of evidence-signal kinds a skill may cite. The model is told this
# vocabulary and may only tag a skill with kinds drawn from it; the serializer and the
# SPA need a small stable set, not free text. "git" = languages + commit subjects,
# "tasks" = checklist/task titles, "docs" = the project's instruction/decision docs.
SKILL_SIGNALS = ("git", "tasks", "docs")

# The bounds of a skill's per-project WEIGHT — the model's estimate, grounded in the
# provided evidence, of how central the skill is to THIS project (1 incidental .. 3
# central). It is bounded and evidence-grounded (like a discipline's `scope`
# classification), not a free self-rating; the relay combines it with cross-project
# BREADTH to derive the comb's tooth height. Out-of-range or missing weights clamp to
# this range so one odd value never distorts a tooth.
SKILL_WEIGHT_MIN = 1
SKILL_WEIGHT_MAX = 3

# Bump when the SKILLS extraction prompt/output contract changes, so the collector's
# content-hash cache re-extracts unchanged bundles instead of serving stale cards.
# Independent of disciplines' CACHE_VERSION: the two prompts evolve separately.
SKILLS_CACHE_VERSION = "1"


@dataclass(frozen=True)
class Skill:
    """One competency a project's observed evidence demonstrates (a comb "tooth").

    Args:
        name: A short competency phrase (e.g. "LLM summarization pipelines", "Python
            stdlib-first backends"). Reframed by the model from the evidence bundle.
        category: The grouping this skill belongs to (e.g. "Backend", "ML / NLP",
            "Frontend"). The comb groups teeth by category; the model picks one from a
            small guided vocabulary.
        evidence: One plain sentence, grounded in the bundle, naming what demonstrates
            the skill (e.g. "Built the relay's stdlib HTTP API and CLI"). Never invented.
        weight: The model's evidence-grounded estimate of how CENTRAL this skill is to
            THIS project, clamped to [SKILL_WEIGHT_MIN, SKILL_WEIGHT_MAX]. The relay
            combines it with cross-project breadth to derive the tooth height.
        signals: The evidence kinds (a subset of SKILL_SIGNALS) the skill draws on —
            which parts of the bundle support it. Used by the SPA to show provenance.

    Why:
        The producer-side twin of the relay's skill JSON, and the per-project unit the
        relay merges across projects into the comb. A frozen record (not a bare dict)
        makes every field explicit at each use site; `as_dict` is the single
        serialization point the push payload and the extraction cache share (DRY),
        exactly as Discipline does. `source` is absent here (unlike Discipline): a
        skill's "source" is WHICH PROJECTS evidence it, which only the cross-project
        relay merge knows — the producing project is implicit in the push.
    """

    name: str
    category: str
    evidence: str
    weight: int
    signals: tuple[str, ...]

    def as_dict(self) -> dict:
        """Serialize to the wire/cache dict ({name, category, evidence, weight, signals}).

        Returns:
            A plain dict with all five fields — the shape push_skills sends and the
            extraction cache stores.

        Why:
            One serialization point keeps the push payload and the cache byte-shape in
            agreement, so a cache hit reconstructs exactly what a fresh extraction would
            have pushed. `signals` is emitted as a list (JSON has no tuples).
        """
        return {
            "name": self.name,
            "category": self.category,
            "evidence": self.evidence,
            "weight": self.weight,
            "signals": list(self.signals),
        }


# The skills system prompt, shared by every backend (DRY). Like the disciplines prompt
# its spine is OBSERVE-NOT-ORIGINATE, but it is tuned for the resume crux: the model
# must ground every skill in the provided evidence and must NOT list aspirational or
# generic skills the evidence does not support.
_SKILLS_SYSTEM_PROMPT = (
    "You read an EVIDENCE BUNDLE about one of a developer's projects — the programming "
    "languages it uses (from its tracked files), recent commit subjects, and short "
    "excerpts of its own instruction/decision docs — and you name the concrete SKILLS "
    "and TYPES OF WORK the project demonstrates, to build a 'partial living resume' "
    "that is grounded in real work rather than self-description.\n"
    "You are OBSERVING. Every skill you list MUST be supported by the provided "
    "evidence. You must NOT list aspirational skills, generic buzzwords, or anything "
    "the evidence does not actually show. If the bundle is thin, return fewer skills "
    "(an empty array is valid).\n"
    "Rules:\n"
    "- `name`: a short, specific competency phrase (about 2-6 words), e.g. 'Python "
    "stdlib-first backends', 'LLM summarization pipelines', 'React component design'. "
    "Prefer concrete craft over vague labels like 'software engineering'.\n"
    "- `category`: a short grouping for the skill. Prefer this small vocabulary when it "
    "fits: 'Backend', 'Frontend', 'ML / NLP', 'Systems & tooling', 'Foundations & "
    "theory', 'Product & design'. Use another short category only if none fit.\n"
    "- `evidence`: ONE sentence naming what in the bundle demonstrates the skill "
    "(a language, a body of commits, a documented decision). Do not invent specifics.\n"
    "- `weight`: an integer 1-3 for how CENTRAL the skill is to THIS project, judged "
    "from the evidence: 1 incidental, 2 notable, 3 central. Not a self-rating of "
    "ability — a reading of how much the evidence is ABOUT this skill.\n"
    "- `signals`: an array naming which evidence supported it, each one of \"git\", "
    "\"tasks\", \"docs\". Include only the kinds you actually used.\n"
    "- Never reproduce code, file contents, secrets, keys, or tokens.\n"
    "- Use clean prose: no em-dashes, no semicolons. Avoid generic LLM filler and stock "
    "phrasings (e.g. 'delve', 'leverage' to mean 'use', 'seamless').\n"
    "Return ONLY a JSON array of objects, each {\"name\": str, \"category\": str, "
    "\"evidence\": str, \"weight\": int, \"signals\": [str]}. No prose, no code fence, "
    "no preamble. An empty array is a valid answer."
)


def _parse_skills(raw: str) -> tuple[Skill, ...]:
    """Parse a model response into validated Skills.

    Args:
        raw: The model's reply — expected to be a JSON array of
            {name, category, evidence, weight, signals} objects (a leading/trailing
            code fence is tolerated).

    Returns:
        The validated Skills, in the model's order. Malformed individual items
        (missing name/category, non-list signals) are SKIPPED (fail-soft per item),
        mirroring the collectors' "anything that does not match is ignored" tolerance.
        `weight` is clamped to [SKILL_WEIGHT_MIN, SKILL_WEIGHT_MAX] (defaulting to the
        minimum when absent/unparseable), and `signals` is filtered to SKILL_SIGNALS,
        so one odd field never aborts an otherwise-usable card.

    Raises:
        ExtractError when the whole response is not a JSON array — a contract break,
        not a single bad item.

    Why:
        One validation home so every backend shares the same shape guarantees, exactly
        as _parse_disciplines does. We are lenient on the secondary fields (weight,
        signals) and strict on identity (name, category) because a usable skill needs a
        name and a group, but a missing weight should degrade (clamp) rather than drop a
        real competency.
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

    skills: list[Skill] = []
    for item in items:
        # Identity fields (name, category) are required; a card without them is
        # unusable and skipped. Secondary fields degrade rather than drop the card.
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        category = item.get("category")
        evidence = item.get("evidence")
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(category, str) or not category.strip():
            continue
        # evidence may be empty (the SPA simply shows no evidence line); coerce to "".
        evidence_text = evidence.strip() if isinstance(evidence, str) else ""

        # weight: clamp into range; a missing/odd value becomes the minimum so the
        # tooth is still drawn (shortest) rather than the card being dropped.
        raw_weight = item.get("weight")
        weight = raw_weight if isinstance(raw_weight, int) and not isinstance(raw_weight, bool) else SKILL_WEIGHT_MIN
        weight = max(SKILL_WEIGHT_MIN, min(SKILL_WEIGHT_MAX, weight))

        # signals: keep only known kinds, de-duplicated in SKILL_SIGNALS order so the
        # provenance display is stable regardless of how the model ordered them.
        raw_signals = item.get("signals")
        chosen = set(raw_signals) if isinstance(raw_signals, list) else set()
        signals = tuple(s for s in SKILL_SIGNALS if s in chosen)

        skills.append(
            Skill(
                name=name.strip(),
                category=category.strip(),
                evidence=evidence_text,
                weight=weight,
                signals=signals,
            )
        )
    return tuple(skills)


class SkillExtractor(Protocol):
    """The provider-agnostic skills-extraction seam (E2 Inc 4 slice 4c).

    Why:
        Mirrors DisciplineExtractor: the one interface the skills collector depends on.
        A Protocol means a backend just needs the method — and a fake is trivial to
        inject in tests (no network, no API key).
    """

    def extract(self, evidence_text: str) -> tuple[Skill, ...]:
        """Extract the skills demonstrated by one (already-redacted) evidence bundle.

        Args:
            evidence_text: The rendered evidence bundle, ALREADY REDACTED by the caller.

        Returns:
            The observed Skills (possibly empty). Backends raise ExtractError on an API
            failure or an unparseable response.
        """
        ...


class AnthropicSkillExtractor:
    """Skills extractor backed by the Anthropic Messages API (the default).

    Args:
        client: An anthropic.Anthropic instance, injected by the caller — same as the
            summarizer / disciplines extractor, so secret handling stays in the CLI and
            tests can substitute a fake client.
        model: The model id to request (Haiku via config — the lightest model adequate
            for this evidence-grounded extraction).

    Why:
        Reuses the exact Anthropic plumbing the disciplines extractor uses (plain,
        non-streaming Messages request; APIError -> ExtractError). Only the system
        prompt and the JSON shape differ.
    """

    def __init__(self, client: anthropic.Anthropic, model: str) -> None:
        self._client = client
        self._model = model

    def extract(self, evidence_text: str) -> tuple[Skill, ...]:
        """Extract skills from one redacted evidence bundle via the Anthropic API.

        Args:
            evidence_text: The ALREADY-REDACTED rendered evidence bundle.

        Returns:
            The observed Skills (possibly empty).

        Why:
            See the class docstring. We collect the response text blocks (as the
            summarizer does) and hand them to _parse_skills. Any APIError becomes an
            ExtractError so the collector can surface a clean setup/transport failure.
        """
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=MAX_TOKENS,
                system=_SKILLS_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": evidence_text}],
            )
        except anthropic.APIError as exc:
            raise ExtractError(f"Anthropic API call failed: {exc}") from exc

        reply = "".join(
            block.text
            for block in response.content
            if getattr(block, "type", None) == "text"
        )
        return _parse_skills(reply)


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

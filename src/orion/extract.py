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
from collections.abc import Sequence
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
# "2": the global two-pass rework (vocab + per-project attribution) replaced the
# single-pass per-project prompt, so every prior cache entry must re-extract.
# "3": added the "achieved, not demoed" anti-overclaim rule to both passes (a demo of how
# something might work is not a competency), so cached extractions from before it re-run.
SKILLS_CACHE_VERSION = "3"

# The global skills extraction is a HARDER task than the per-project summarizer reframe:
# pass 1 deduplicates and resume-grades competencies across the WHOLE portfolio, and
# pass 2 attributes them per project. So this step alone steps UP from the Haiku
# summarizer default to Sonnet, per the project's "step to Sonnet only for that step,
# and only when the lightest model visibly misses nuance" rule. The CLI builder passes
# this to the extractor rather than reusing the summarizer's configured model.
SKILLS_EXTRACTION_MODEL = "claude-sonnet-4-6"

# The two-pass calls emit more than a single summary — a portfolio-wide vocabulary
# (pass 1) and an attributed subset with evidence sentences (pass 2) — so they get a
# larger budget than MAX_TOKENS. A response that HITS this ceiling is truncated; the
# extractor treats truncation as a failure (raises ExtractError) rather than parsing a
# half-built array, so a sync run aborts and never pushes a partial skill set.
SKILLS_MAX_TOKENS = 4096


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


@dataclass(frozen=True)
class VocabSkill:
    """One entry in the portfolio-wide canonical skill vocabulary (pass 1 output).

    Args:
        name: The canonical competency phrase (e.g. "Bayesian inference", "Python
            stdlib-first backends"). Pass 1 deduplicates across the whole portfolio, so
            the same competency phrased differently in several projects collapses to one
            of these — which is what fixes the per-project near-duplicate problem.
        category: The grouping this skill belongs to (the comb's section).

    Why:
        Pass 1 sees EVERY project's evidence at once and emits ONLY names + categories —
        no per-project free text — so it can canonicalize globally without carrying
        anything that could identify a specific (possibly out-of-scope) project. Pass 2
        then attributes these fixed names per project (blind to other projects). A frozen
        record keeps the two-field shape explicit at every use site, like Skill.
    """

    name: str
    category: str


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


# PASS 1 (global). Reads EVERY project's evidence bundle at once and produces ONE
# deduplicated, portfolio-wide VOCABULARY of competencies. Two jobs the per-project
# single-pass prompt could not do: (a) collapse the same competency phrased differently
# across projects into one canonical name, and (b) frame entries as RESUME/JD-grade
# transferable skills, not project deliverables. Its OUTPUT is names + categories ONLY —
# no per-project free text — so it canonicalizes globally without emitting anything that
# could identify a specific project (pass 2 handles per-project attribution).
_SKILLS_VOCAB_SYSTEM_PROMPT = (
    "You read the EVIDENCE BUNDLES for SEVERAL of a developer's projects — each bundle "
    "lists a project's programming languages (from its tracked files), recent commit "
    "subjects, and short excerpts of its instruction/decision docs. From all of them "
    "together you produce ONE deduplicated VOCABULARY of the developer's SKILLS, to build "
    "a 'partial living resume' grounded in real work rather than self-description.\n"
    "You are OBSERVING. Every skill MUST be supported by the evidence across the "
    "projects. Do NOT list aspirational skills, generic buzzwords, or anything the "
    "evidence does not actually show.\n"
    "Three rules that define this task:\n"
    "1. DEDUPLICATE GLOBALLY. The same competency often appears under different wording "
    "in different projects (e.g. 'Python backend development', 'Python backends and "
    "integrations', 'Python stdlib-first backends'). Collapse these into ONE canonical "
    "entry. The output must contain no near-duplicates.\n"
    "2. SKILLS, NOT DELIVERABLES. A skill is a TRANSFERABLE COMPETENCY that a resume or a "
    "job description would name — not a system or feature built for one project. For "
    "example 'multi-drone tracking' is a deliverable; the underlying SKILLS are 'Bayesian "
    "inference' and 'geospatial data processing'. Name the competencies behind the work, "
    "not the work products.\n"
    "3. CLAIM ONLY WHAT THE WORK ACHIEVED, never what it merely gestured at. Ground each "
    "skill in a real, working capability the evidence shows — not in what a demo, "
    "prototype, or proof-of-concept only ILLUSTRATES. If the evidence shows that something "
    "was demonstrated, mocked, or simulated to show how it MIGHT work, rather than actually "
    "built and working, do NOT name it as a competency. When in doubt, prefer the narrower "
    "skill that is genuinely evidenced (e.g. the probability modeling behind a demo) over "
    "the broader capability the demo suggests (e.g. 'simulation' or 'coordination' that was "
    "only illustrated). A resume must not overstate.\n"
    "Rules for each entry:\n"
    "- `name`: a short, canonical competency phrase (about 2-5 words), e.g. 'Bayesian "
    "inference', 'LLM evaluation', 'Python stdlib-first backends', 'React component "
    "design'. Prefer concrete craft over vague labels like 'software engineering'.\n"
    "- `category`: a short grouping. Prefer this small vocabulary when it fits: 'Backend', "
    "'Frontend', 'ML / NLP', 'Systems & tooling', 'Foundations & theory', 'Product & "
    "design'. Use another short category only if none fit.\n"
    "- Never reproduce code, file contents, secrets, keys, or tokens. Do not name "
    "individual projects in the output.\n"
    "- Use clean prose: no em-dashes, no semicolons. Avoid generic LLM filler.\n"
    "Return ONLY a JSON array of objects, each {\"name\": str, \"category\": str}. No "
    "prose, no code fence, no preamble. An empty array is a valid answer."
)


# PASS 2 (per-project). Reads the canonical vocabulary plus ONE project's evidence bundle
# and decides which vocabulary skills THAT project demonstrates, with a per-project
# weight, an evidence sentence, and signals. It is deliberately BLIND to every other
# project: it cannot reference, and so cannot leak, a project it never sees — which is
# what restores the existence-hiding guarantee the per-project design had. It may select
# ONLY from the given vocabulary (a controlled-vocabulary pass), so naming stays
# consistent across projects and the relay's merge collapses duplicates correctly.
_SKILLS_ATTRIBUTE_SYSTEM_PROMPT = (
    "You are given a CANONICAL SKILL VOCABULARY (a fixed list of competency names and "
    "their categories) and the EVIDENCE BUNDLE for ONE of a developer's projects — its "
    "languages, recent commit subjects, and doc excerpts. Decide which of the "
    "vocabulary's skills THIS project's evidence actually demonstrates.\n"
    "You are OBSERVING. Include a skill ONLY if this project's own evidence shows it was "
    "actually built and working — not merely demonstrated, mocked, or simulated to show how "
    "it might work. A resume must not overstate.\n"
    "Rules:\n"
    "- SELECT ONLY FROM THE VOCABULARY. The `name` of each entry you return MUST match a "
    "vocabulary name exactly. Never invent a skill that is not in the vocabulary, even if "
    "the evidence suggests one.\n"
    "- `weight`: an integer 1-3 for how CENTRAL the skill is to THIS project, judged from "
    "the evidence: 1 incidental, 2 notable, 3 central. Not a self-rating of ability.\n"
    "- `evidence`: ONE sentence, grounded ONLY in THIS project's bundle, naming what "
    "demonstrates the skill (a language, a body of commits, a documented decision). Do "
    "NOT mention any other project, and do not invent specifics not in this bundle.\n"
    "- `signals`: an array naming which evidence supported it, each one of \"git\", "
    "\"tasks\", \"docs\". Include only the kinds you actually used.\n"
    "- Never reproduce code, file contents, secrets, keys, or tokens.\n"
    "- Use clean prose: no em-dashes, no semicolons. Avoid generic LLM filler.\n"
    "Return ONLY a JSON array of objects, each {\"name\": str, \"weight\": int, "
    "\"evidence\": str, \"signals\": [str]}. No prose, no code fence, no preamble. An "
    "empty array is valid (this project may demonstrate none of the vocabulary's skills)."
)


def _strip_fence(raw: str) -> str:
    """Strip a leading/trailing ```...``` code fence some models add despite the prompt.

    Args:
        raw: The model's reply text.

    Returns:
        The inner text with any surrounding fence removed, stripped of whitespace. An
        empty string when a fence opened but enclosed nothing.

    Why:
        All three skills parsers (single-pass, vocab, attribution) tolerate the same
        fence, so the tolerance lives in one place (DRY) rather than being copied per
        parser.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else ""
        if text.endswith("```"):
            text = text[: -len("```")]
        text = text.strip()
    return text


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
    text = _strip_fence(raw)

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


def _parse_vocab(raw: str) -> tuple[VocabSkill, ...]:
    """Parse a pass-1 response into the deduplicated canonical vocabulary.

    Args:
        raw: The model's reply — expected to be a JSON array of {name, category} objects.

    Returns:
        The validated VocabSkills in the model's order, deduplicated by casefolded name
        (first occurrence wins). Items missing a name or category are SKIPPED (fail-soft
        per item).

    Raises:
        ExtractError when the whole response is not a JSON array (a contract break).

    Why:
        Pass 1 is the controlled vocabulary that pass 2 attributes against, so it must be
        clean and duplicate-free. The casefold de-dup here is a safety net ON TOP of the
        prompt's global-dedup instruction — if the model still emits two spellings of one
        skill, only the first survives, so pass 2 cannot split a project across both.
    """
    text = _strip_fence(raw)
    try:
        items = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExtractError(f"Vocab extractor returned non-JSON output: {exc}") from exc
    if not isinstance(items, list):
        raise ExtractError("Vocab extractor output was not a JSON array.")

    vocab: list[VocabSkill] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        category = item.get("category")
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(category, str) or not category.strip():
            continue
        norm = name.strip().casefold()
        if norm in seen:
            continue
        seen.add(norm)
        vocab.append(VocabSkill(name=name.strip(), category=category.strip()))
    return tuple(vocab)


def _parse_attribution(raw: str, vocab: Sequence[VocabSkill]) -> tuple[Skill, ...]:
    """Parse a pass-2 response into per-project Skills bound to the canonical vocabulary.

    Args:
        raw: The model's reply — expected to be a JSON array of
            {name, weight, evidence, signals} objects.
        vocab: The canonical vocabulary pass 2 was told to select from. It supplies each
            skill's canonical name + category AND acts as the allow-list: a returned name
            that is not in the vocabulary is DROPPED (the controlled-vocabulary guard).

    Returns:
        The validated Skills, with `name` and `category` taken from the matched vocabulary
        entry so naming is identical across projects. Items naming a non-vocabulary skill,
        or missing a name, are skipped; `weight` clamps to [SKILL_WEIGHT_MIN,
        SKILL_WEIGHT_MAX] and `signals` filters to SKILL_SIGNALS, as in _parse_skills.

    Raises:
        ExtractError when the whole response is not a JSON array (a contract break).

    Why:
        Pass 2's job is attribution, not naming. Binding each returned card back to its
        vocabulary entry by casefolded name is exactly what guarantees the same competency
        carries the same name in every project, so the relay's casefold merge collapses
        them instead of leaving the per-project near-duplicates this rework exists to fix.
        Dropping out-of-vocab names stops pass 2 from quietly reintroducing a duplicate the
        global pass already canonicalized.
    """
    text = _strip_fence(raw)
    try:
        items = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExtractError(f"Attribution extractor returned non-JSON output: {exc}") from exc
    if not isinstance(items, list):
        raise ExtractError("Attribution extractor output was not a JSON array.")

    # Match returned names back to the vocabulary by casefolded name (the same
    # normalization the relay's merge uses), so the stored card carries the canonical
    # spelling regardless of how the model cased it.
    by_norm = {v.name.casefold(): v for v in vocab}
    skills: list[Skill] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        canonical = by_norm.get(name.strip().casefold())
        if canonical is None:
            # Not in the controlled vocabulary — drop it; pass 2 must not invent skills.
            continue
        evidence = item.get("evidence")
        evidence_text = evidence.strip() if isinstance(evidence, str) else ""

        raw_weight = item.get("weight")
        weight = raw_weight if isinstance(raw_weight, int) and not isinstance(raw_weight, bool) else SKILL_WEIGHT_MIN
        weight = max(SKILL_WEIGHT_MIN, min(SKILL_WEIGHT_MAX, weight))

        raw_signals = item.get("signals")
        chosen = set(raw_signals) if isinstance(raw_signals, list) else set()
        signals = tuple(s for s in SKILL_SIGNALS if s in chosen)

        skills.append(
            Skill(
                name=canonical.name,
                category=canonical.category,
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

        Why:
            The legacy SINGLE-PASS per-project path (the deprecated `skills-push`). The
            global `skills-sync` path uses extract_vocab + attribute instead; this stays
            for the existing command and its tests.
        """
        ...

    def extract_vocab(self, portfolio_text: str) -> tuple[VocabSkill, ...]:
        """Pass 1: produce the deduplicated portfolio-wide skill vocabulary.

        Args:
            portfolio_text: The ALREADY-REDACTED, project-labeled evidence for the WHOLE
                portfolio (every in-scope project's bundle, assembled by the collector).

        Returns:
            The canonical VocabSkills (possibly empty). Backends raise ExtractError on an
            API failure, a truncated response, or an unparseable reply.
        """
        ...

    def attribute(
        self, evidence_text: str, vocab: Sequence[VocabSkill]
    ) -> tuple[Skill, ...]:
        """Pass 2: attribute the canonical vocabulary to ONE project's evidence.

        Args:
            evidence_text: The ALREADY-REDACTED evidence bundle for ONE project — the only
                project this call ever sees, which is what keeps its output from
                referencing (and so leaking) any other project.
            vocab: The canonical vocabulary from extract_vocab; the controlled set this
                project's skills are selected from.

        Returns:
            The Skills this project evidences (possibly empty), each bound to a vocabulary
            entry. Backends raise ExtractError on an API failure, truncation, or an
            unparseable reply.
        """
        ...


class AnthropicSkillExtractor:
    """Skills extractor backed by the Anthropic Messages API (the default).

    Args:
        client: An anthropic.Anthropic instance, injected by the caller — same as the
            summarizer / disciplines extractor, so secret handling stays in the CLI and
            tests can substitute a fake client.
        model: The model id to request. The CLI passes SKILLS_EXTRACTION_MODEL (Sonnet)
            for the global two-pass path, since portfolio-wide dedup + resume-grading is a
            harder task than the per-project reframe; the legacy single-pass `extract`
            shares the same client/model.

    Why:
        Reuses the exact Anthropic plumbing the disciplines extractor uses (plain,
        non-streaming Messages request; APIError -> ExtractError). Only the system
        prompt and the JSON shape differ. The three calls share one _complete helper so
        the API plumbing and the truncation guard live in a single place (DRY).
    """

    def __init__(self, client: anthropic.Anthropic, model: str) -> None:
        self._client = client
        self._model = model

    def _complete(self, system: str, user_text: str, max_tokens: int) -> str:
        """Run one Messages request and return its concatenated text, guarding truncation.

        Args:
            system: The system prompt for this call.
            user_text: The (already-redacted) user message.
            max_tokens: The output budget for this call.

        Returns:
            The concatenated text of the reply's text blocks.

        Raises:
            ExtractError on an APIError, OR when the model stopped because it hit
            max_tokens (a TRUNCATED reply). Truncation is treated as a failure, not parsed
            as a partial array, so a sync run aborts rather than pushing a half-built skill
            set (a valid-but-partial JSON could otherwise silently drop a project's skills).

        Why:
            All three skills calls share this plumbing. The stop_reason check is the
            structural guard behind the plan's "never write a partial result": we would
            rather fail loudly and keep the relay's prior skills than store a truncated set.
        """
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_text}],
            )
        except anthropic.APIError as exc:
            raise ExtractError(f"Anthropic API call failed: {exc}") from exc

        # A reply that stopped on the token ceiling is incomplete; refuse to parse it.
        if getattr(response, "stop_reason", None) == "max_tokens":
            raise ExtractError(
                "Skills extraction hit the token ceiling (response truncated); aborting "
                "without pushing so the relay's prior skills are left intact."
            )

        return "".join(
            block.text
            for block in response.content
            if getattr(block, "type", None) == "text"
        )

    def extract(self, evidence_text: str) -> tuple[Skill, ...]:
        """Extract skills from one redacted evidence bundle via the Anthropic API (legacy).

        Args:
            evidence_text: The ALREADY-REDACTED rendered evidence bundle.

        Returns:
            The observed Skills (possibly empty).

        Why:
            The legacy single-pass per-project path (deprecated `skills-push`). Kept
            unchanged in behavior; the global path uses extract_vocab + attribute.
        """
        reply = self._complete(_SKILLS_SYSTEM_PROMPT, evidence_text, MAX_TOKENS)
        return _parse_skills(reply)

    def extract_vocab(self, portfolio_text: str) -> tuple[VocabSkill, ...]:
        """Pass 1: deduplicate + resume-grade the whole portfolio into one vocabulary.

        Args:
            portfolio_text: The ALREADY-REDACTED, project-labeled portfolio evidence.

        Returns:
            The canonical VocabSkills (possibly empty).

        Why:
            One global call over all projects is what lets the model collapse the
            cross-project near-duplicates a per-project pass cannot see. Its output is
            names + categories only, so seeing every project leaks nothing downstream.
        """
        reply = self._complete(
            _SKILLS_VOCAB_SYSTEM_PROMPT, portfolio_text, SKILLS_MAX_TOKENS
        )
        return _parse_vocab(reply)

    def attribute(
        self, evidence_text: str, vocab: Sequence[VocabSkill]
    ) -> tuple[Skill, ...]:
        """Pass 2: select which vocabulary skills ONE project's evidence demonstrates.

        Args:
            evidence_text: The ALREADY-REDACTED evidence bundle for ONE project.
            vocab: The canonical vocabulary to select from.

        Returns:
            The Skills this project evidences (possibly empty), bound to vocabulary entries.

        Why:
            Per-project and blind to other projects, so its evidence sentences cannot name
            (and so cannot leak) a project it never sees. The vocabulary is rendered into
            the user message as a fixed allow-list; _parse_attribution drops anything off
            it so pass 2 cannot reintroduce a duplicate.
        """
        vocab_lines = "\n".join(f"- {v.name} [{v.category}]" for v in vocab)
        user_text = (
            "## Canonical skill vocabulary (select only from these)\n"
            f"{vocab_lines}\n\n"
            "## This project's evidence\n"
            f"{evidence_text}"
        )
        reply = self._complete(
            _SKILLS_ATTRIBUTE_SYSTEM_PROMPT, user_text, SKILLS_MAX_TOKENS
        )
        return _parse_attribution(reply, vocab)


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

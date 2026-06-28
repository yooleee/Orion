# =============================================================================
# collectors/skills.py
# -----------------------------------------------------------------------------
# Responsible for: The SKILLS signal (E2 Inc 4 slice 4c — the "skills comb" /
#                  partial living resume). Gathers a project's OBSERVED evidence
#                  (languages from its tracked files, recent commit subjects, and
#                  short excerpts of its instruction/decision docs) and reframes it
#                  into competency cards via the opt-in LLM step (extract.py).
# Role in project: A dashboard-only signal (like the live checklist / disciplines),
#                  NOT a report lane — it has a snapshot() (full current state) and no
#                  collect()/delta. The CLI pushes the snapshot to the relay's /skills
#                  endpoint, full-state, exactly as disciplines-push works. The relay
#                  then MERGES each project's skills across the portfolio into the comb.
# Observe-not-originate: a resume is normally authored; this one is DERIVED. The model
#                  reframes only what the evidence shows — it must never list a skill the
#                  evidence does not support (the resume-puffery guard lives in the prompt).
# Privacy: the rendered evidence bundle is REDACTED before it reaches the model (the
#                  privacy invariant). The CLI redacts the extracted text again as a net.
# Cache: extraction is gated on a content hash of the redacted bundle (via the injected
#                  cache_get/cache_set), so the model runs ONLY when the evidence actually
#                  changes — cheap and idempotent. One bundle per project = one cache key.
# =============================================================================

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from pathlib import Path

from dataclasses import dataclass

from orion.collectors import git
from orion.extract import (
    SKILLS_CACHE_VERSION,
    ExtractError,
    Skill,
    SkillExtractor,
    VocabSkill,
)
from orion.redact import redact

# The cache_get/cache_set seam: the collector stays decoupled from the state store
# (and trivially testable with a dict) while the CLI wires these to state.get_cache /
# state.set_cache. cache_get returns (content_hash, extraction_json) or None. Mirrors
# the disciplines collector. (Used by the legacy single-project snapshot.)
CacheGet = Callable[[str], "tuple[str, str] | None"]
CacheSet = Callable[[str, str, str], None]

# The GLOBAL sync's cache seam carries an extra cache_id (which project, or the portfolio
# sentinel) because one sync caches BOTH the portfolio-wide vocabulary and each project's
# attribution. The CLI wires these to state.get_cache/set_cache with the "skills"
# namespace and cache_id as the per-row project key.
SyncCacheGet = Callable[[str, str], "tuple[str, str] | None"]
SyncCacheSet = Callable[[str, str, str, str], None]

# One cache key per project — unlike disciplines (one key per doc), a project's skills
# come from a SINGLE merged evidence bundle, so there is one extraction and one entry.
_BUNDLE_KEY = "bundle"

# The global sync's two cache slots. The vocabulary is stored under a synthetic
# portfolio "project" (it is cross-project, not owned by any one project); each project's
# attribution is stored under that project's own name, key "attribution" (distinct from
# the legacy "bundle" key so the two paths never collide).
_PORTFOLIO_CACHE_ID = "__portfolio__"
_VOCAB_KEY = "vocab"
_ATTRIBUTION_KEY = "attribution"

# How many recent commit subjects to feed the model. Enough to characterize the kind of
# work without ballooning cost or the redaction surface.
_COMMIT_LIMIT = 60

# How many leading characters of each doc to include as "focus" evidence. We want the
# doc's framing (its opening / headings), not its whole body — the disciplines collector
# already reads docs in full for principles; here we only need a topical hint.
_DOC_EXCERPT_CHARS = 1500

# Map a tracked file's extension to a human language/skill label. Only extensions that
# signal a real competency are listed; anything unmapped is ignored (so lockfiles,
# images, and data files do not masquerade as languages). Lowercased before lookup.
_EXTENSION_LANGUAGES: dict[str, str] = {
    ".py": "Python",
    ".ts": "TypeScript",
    ".tsx": "TypeScript / React",
    ".js": "JavaScript",
    ".jsx": "JavaScript / React",
    ".css": "CSS",
    ".html": "HTML",
    ".md": "Markdown / docs",
    ".toml": "TOML config",
    ".sql": "SQL",
    ".sh": "Shell",
    ".rs": "Rust",
    ".go": "Go",
    ".java": "Java",
    ".c": "C",
    ".h": "C",
    ".cpp": "C++",
    ".rb": "Ruby",
    ".yml": "YAML",
    ".yaml": "YAML",
}


def _language_counts(paths: Sequence[str]) -> dict[str, int]:
    """Count tracked files per recognized language.

    Args:
        paths: Repo-relative tracked file paths (from git.tracked_files).

    Returns:
        A {language: file_count} dict for the languages in _EXTENSION_LANGUAGES that
        appear, ordered most-frequent first (ties keep first-seen order). Empty when no
        tracked file has a recognized extension.

    Why:
        File-count per language is a cheap, honest proxy for "what this project is built
        in" — the headline evidence the model reframes into language/stack skills.
        Unmapped extensions are ignored so noise (data, images, lockfiles) cannot pose
        as a language. We sort by count so the rendered bundle leads with the dominant
        languages, which keeps the evidence's emphasis stable across runs (and the cache
        key stable when the file set is unchanged).
    """
    counts: dict[str, int] = {}
    for path in paths:
        ext = Path(path).suffix.lower()
        lang = _EXTENSION_LANGUAGES.get(ext)
        if lang is None:
            continue
        counts[lang] = counts.get(lang, 0) + 1
    # Sort by descending count, then by language name for a deterministic tie-break.
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _doc_excerpts(docs: Sequence[Path]) -> list[str]:
    """Read a short leading excerpt of each doc for topical "focus" evidence.

    Args:
        docs: Instruction/decision doc paths (the project's discipline_docs, reused).

    Returns:
        A list of "<repo-relative-ish name>: <excerpt>" strings, one per readable doc.
        An unreadable doc is skipped (fail-soft) — a missing doc must never abort the
        bundle.

    Why:
        The docs say what the project is ABOUT, which helps the model name domain skills
        (e.g. "local-first systems", "LLM pipelines") that languages alone do not reveal.
        We take only a leading excerpt (not the whole doc) because we need topical
        framing, not the full text, and a bounded excerpt keeps cost and the redaction
        surface small. The doc name is the bare filename — never an absolute path — so no
        home-directory/username leaks into the evidence.
    """
    excerpts: list[str] = []
    for doc in docs:
        try:
            text = doc.read_text(encoding="utf-8")
        except OSError:
            # Fail soft: a doc we cannot read simply contributes no focus evidence.
            continue
        excerpt = text.strip()[:_DOC_EXCERPT_CHARS]
        if excerpt:
            excerpts.append(f"{doc.name}: {excerpt}")
    return excerpts


def _render_bundle(
    languages: dict[str, int], commits: Sequence[str], docs: Sequence[str]
) -> str:
    """Render the gathered evidence into one deterministic text bundle for the model.

    Args:
        languages: {language: file_count}, already ordered.
        commits: Recent commit subject lines, newest first.
        docs: Per-doc "name: excerpt" focus strings.

    Returns:
        A single labeled text block (## Languages / ## Recent work / ## Project docs).
        Sections with no evidence are omitted.

    Why:
        A stable, labeled rendering does two jobs: it gives the model clearly-typed
        evidence to ground each skill in, and it is the EXACT string the content hash is
        taken over — so an unchanged project yields a byte-identical bundle and a cache
        hit (no model call). Determinism here is what makes the cache trustworthy.
    """
    sections: list[str] = []
    if languages:
        lang_lines = [f"- {lang} ({count} files)" for lang, count in languages.items()]
        sections.append("## Languages (by tracked-file count)\n" + "\n".join(lang_lines))
    if commits:
        commit_lines = [f"- {subject}" for subject in commits]
        sections.append("## Recent work (commit subjects)\n" + "\n".join(commit_lines))
    if docs:
        sections.append("## Project docs (focus excerpts)\n" + "\n\n".join(docs))
    return "\n\n".join(sections).strip()


def _safe_bundle(
    repo_path: Path, doc_paths: Sequence[Path], commit_limit: int
) -> str:
    """Gather a project's evidence and return it as ONE redacted bundle string.

    Args:
        repo_path: The project's repo root (language + commit evidence).
        doc_paths: Instruction/decision docs to read for topical focus (may be empty).
        commit_limit: How many recent commit subjects to include.

    Returns:
        The redacted, rendered evidence bundle, or "" when the project has no evidence at
        all (empty repo, no docs) — in which case there is nothing to extract.

    Why:
        Both the legacy per-project snapshot and the global sync need the SAME
        gather + render + redact step, so it lives in one place (DRY). Redaction happens
        HERE, before the text can reach the model, so every caller inherits the privacy
        invariant without restating it.
    """
    languages = _language_counts(git.tracked_files(repo_path))
    commits = git.recent_subjects(repo_path, commit_limit)
    docs = _doc_excerpts(doc_paths)
    bundle = _render_bundle(languages, commits, docs)
    if not bundle:
        return ""
    # Redact BEFORE the text can reach the model (the privacy invariant).
    return redact(bundle).text


def snapshot(
    repo_path: Path,
    doc_paths: Sequence[Path],
    extractor: SkillExtractor,
    *,
    cache_get: CacheGet,
    cache_set: CacheSet,
    commit_limit: int = _COMMIT_LIMIT,
) -> tuple[Skill, ...]:
    """Gather a project's evidence and return the skills it demonstrates.

    Args:
        repo_path: The project's repo root — the source of language + commit evidence.
        doc_paths: Instruction/decision docs to read for topical focus (the project's
            discipline_docs, reused; may be empty).
        extractor: The injected SkillExtractor (Anthropic via config, or a fake in
            tests). Called at most once per CHANGED bundle.
        cache_get: Looks up the bundle's cached (content_hash, extraction_json) or None.
        cache_set: Stores the bundle's (content_hash, extraction_json) after a
            successful extraction.
        commit_limit: How many recent commit subjects to include.

    Returns:
        The observed Skills for this project (full current state), possibly empty.

    Raises:
        ExtractError when extraction is attempted and fails (API error, unparseable
        reply). Unlike the disciplines collector — which fails soft PER DOC because it
        has many docs — skills is a SINGLE bundle, so a failure is propagated rather
        than swallowed: the CLI then aborts WITHOUT pushing, leaving the project's prior
        relay skills intact rather than clobbering them with an empty full-state push.
        Evidence GATHERING is still fail-soft (an unreadable doc / a repo with no commits
        simply contributes nothing).

    Why:
        This is the dashboard "live skills" read — current full state, like
        disciplines.snapshot — so the relay can full-state-replace it. We redact the
        rendered bundle BEFORE the model (privacy invariant), hash the redacted text
        (prefixed with SKILLS_CACHE_VERSION so a prompt tune busts the cache), and reuse
        the cached extraction when the hash is unchanged so the LLM runs only on a real
        change.
    """
    safe = _safe_bundle(repo_path, doc_paths, commit_limit)
    if not safe:
        # No evidence at all (empty repo, no docs) — nothing to extract. Return empty
        # without a model call; the caller decides whether an empty push is wanted.
        return ()

    # Hash the redacted bundle (prefixed with the prompt's CACHE_VERSION) so the cache key
    # reflects exactly what we would send AND busts when the prompt changes.
    content_hash = hashlib.sha256(
        f"{SKILLS_CACHE_VERSION}\n{safe}".encode("utf-8")
    ).hexdigest()

    cached = cache_get(_BUNDLE_KEY)
    if cached is not None and cached[0] == content_hash:
        # Unchanged evidence: reconstruct the Skills from the cached extraction — no
        # model call.
        return _skills_from_json(cached[1])

    # Cache miss / changed evidence: run the model. A failure propagates (see Raises) so
    # the CLI does not clobber the stored skills; we cache ONLY on success.
    extracted = extractor.extract(safe)
    cache_set(
        _BUNDLE_KEY,
        content_hash,
        json.dumps([s.as_dict() for s in extracted]),
    )
    return extracted


def _skills_from_json(payload: str) -> tuple[Skill, ...]:
    """Reconstruct cached Skills from their JSON form.

    Args:
        payload: The JSON array a cache slot stored ([{name, category, evidence, weight,
            signals}, ...]).

    Returns:
        The Skills, with `signals` rebuilt as a tuple (JSON has no tuples).

    Why:
        Both the legacy snapshot cache hit and the sync attribution cache hit decode the
        same stored shape, so the reconstruction lives in one place (DRY).
    """
    return tuple(
        Skill(
            name=item["name"],
            category=item["category"],
            evidence=item["evidence"],
            weight=item["weight"],
            signals=tuple(item["signals"]),
        )
        for item in json.loads(payload)
    )


@dataclass(frozen=True)
class SkillProject:
    """One project's inputs to the global sync (its identity + evidence sources).

    Args:
        name: The project name (the relay's per-project upsert key, and the label used to
            separate projects in the pass-1 portfolio evidence).
        repo_path: The project's repo root.
        doc_paths: Its instruction/decision docs (the project's discipline_docs, reused).

    Why:
        sync() works over MANY projects, so a small input record reads better than three
        parallel lists and keeps the collector decoupled from the CLI's ProjectConfig (it
        needs only these three fields). Frozen so a caller cannot mutate it mid-run.
    """

    name: str
    repo_path: Path
    doc_paths: tuple[Path, ...]


def _render_portfolio(bundles: dict[str, str]) -> str:
    """Assemble per-project redacted bundles into ONE labeled portfolio document.

    Args:
        bundles: {project_name: redacted_bundle_text} for every project with evidence.

    Returns:
        The bundles concatenated, each under a "### Project: <name>" header, in sorted
        name order (deterministic, so the pass-1 cache key and model input are stable).

    Why:
        Pass 1 reads every project at once to deduplicate globally, so it needs the
        bundles in one document with clear per-project boundaries. The names appear only
        in the INPUT — pass 1's output is names + categories with no project labels — so
        labeling here does not leak a project downstream.
    """
    blocks = [f"### Project: {name}\n\n{bundles[name]}" for name in sorted(bundles)]
    return "\n\n---\n\n".join(blocks)


def sync(
    projects: Sequence[SkillProject],
    extractor: SkillExtractor,
    *,
    cache_get: SyncCacheGet,
    cache_set: SyncCacheSet,
    commit_limit: int = _COMMIT_LIMIT,
) -> dict[str, tuple[Skill, ...]]:
    """Observe ALL projects together and return each one's skills against ONE vocabulary.

    Args:
        projects: Every project to include (typically all `skills = true` projects).
        extractor: The injected SkillExtractor (Anthropic via config, or a fake in tests).
        cache_get: Looks up a (cache_id, key) slot's cached (content_hash, json) or None.
        cache_set: Stores a (cache_id, key) slot's (content_hash, json).
        commit_limit: How many recent commit subjects to include per project.

    Returns:
        {project_name: skills} for EVERY requested project (a project with no evidence, or
        none of the vocabulary's skills, maps to an empty tuple — the caller decides how to
        treat an empty slice). Naming is consistent across projects because every skill is
        bound to the shared pass-1 vocabulary, so the relay's merge collapses duplicates.

    Raises:
        ExtractError if pass 1 or any pass-2 call fails or truncates. The failure
        propagates so the CLI aborts the WHOLE run WITHOUT pushing — a half-built skill set
        is never written, leaving the relay's prior skills intact (the same abort-on-failure
        guarantee snapshot gives, extended to the portfolio).

    Why:
        This is the two-pass global rework. Pass 1 (extract_vocab) sees every project's
        evidence at once and produces a deduplicated, resume-grade vocabulary — fixing the
        per-project near-duplicate and component-vs-competency problems a per-project pass
        cannot. Pass 2 (attribute) runs per project, BLIND to the others, so its evidence
        text cannot reference (and so cannot leak) another project — restoring the
        existence-hiding guarantee structurally. Two cache levels keep re-runs cheap: the
        vocabulary is cached on the whole portfolio's content, each attribution on its own
        bundle plus the vocabulary.
    """
    # 1) Build a redacted bundle per project. Projects with no evidence are simply absent
    #    from `bundles` (they contribute nothing to the vocabulary), but still appear in
    #    the result as empty so the caller sees the full requested set.
    bundles: dict[str, str] = {}
    bundle_hashes: dict[str, str] = {}
    for project in projects:
        safe = _safe_bundle(project.repo_path, project.doc_paths, commit_limit)
        if not safe:
            continue
        bundles[project.name] = safe
        bundle_hashes[project.name] = hashlib.sha256(
            f"{SKILLS_CACHE_VERSION}\n{safe}".encode("utf-8")
        ).hexdigest()

    result: dict[str, tuple[Skill, ...]] = {project.name: () for project in projects}
    if not bundles:
        # No project has any evidence — nothing to extract; every slice is empty.
        return result

    # 2) Pass 1 (vocabulary), cached on the sorted tuple of all bundle hashes so any one
    #    project's change re-runs it (the price of a global view) but an unchanged
    #    portfolio reuses it. Sorted → order-independent, so adding a project at the end
    #    does not spuriously bust the key.
    portfolio_hash = hashlib.sha256(
        ("\n".join([SKILLS_CACHE_VERSION, *sorted(bundle_hashes.values())])).encode("utf-8")
    ).hexdigest()
    cached_vocab = cache_get(_PORTFOLIO_CACHE_ID, _VOCAB_KEY)
    if cached_vocab is not None and cached_vocab[0] == portfolio_hash:
        vocab = tuple(
            VocabSkill(name=v["name"], category=v["category"])
            for v in json.loads(cached_vocab[1])
        )
    else:
        vocab = extractor.extract_vocab(_render_portfolio(bundles))
        cache_set(
            _PORTFOLIO_CACHE_ID,
            _VOCAB_KEY,
            portfolio_hash,
            json.dumps([{"name": v.name, "category": v.category} for v in vocab]),
        )

    if not vocab:
        # The portfolio demonstrates no nameable skills — all slices stay empty.
        return result

    # The vocabulary's identity, folded into each attribution cache key so a vocabulary
    # change re-runs pass 2 even when a project's own bundle is unchanged.
    vocab_hash = hashlib.sha256(
        json.dumps([[v.name, v.category] for v in vocab]).encode("utf-8")
    ).hexdigest()

    # 3) Pass 2 per project (blind to other projects), cached on (bundle hash, vocab hash).
    for name, safe in bundles.items():
        attribution_hash = hashlib.sha256(
            f"{SKILLS_CACHE_VERSION}\n{bundle_hashes[name]}\n{vocab_hash}".encode("utf-8")
        ).hexdigest()
        cached_attr = cache_get(name, _ATTRIBUTION_KEY)
        if cached_attr is not None and cached_attr[0] == attribution_hash:
            result[name] = _skills_from_json(cached_attr[1])
            continue
        attributed = extractor.attribute(safe, vocab)
        cache_set(
            name,
            _ATTRIBUTION_KEY,
            attribution_hash,
            json.dumps([s.as_dict() for s in attributed]),
        )
        result[name] = attributed

    return result

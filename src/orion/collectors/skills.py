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

from orion.collectors import git
from orion.extract import SKILLS_CACHE_VERSION, Skill, SkillExtractor, ExtractError
from orion.redact import redact

# The cache_get/cache_set seam: the collector stays decoupled from the state store
# (and trivially testable with a dict) while the CLI wires these to state.get_cache /
# state.set_cache. cache_get returns (content_hash, extraction_json) or None. Mirrors
# the disciplines collector.
CacheGet = Callable[[str], "tuple[str, str] | None"]
CacheSet = Callable[[str, str, str], None]

# One cache key per project — unlike disciplines (one key per doc), a project's skills
# come from a SINGLE merged evidence bundle, so there is one extraction and one entry.
_BUNDLE_KEY = "bundle"

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
    languages = _language_counts(git.tracked_files(repo_path))
    commits = git.recent_subjects(repo_path, commit_limit)
    docs = _doc_excerpts(doc_paths)
    bundle = _render_bundle(languages, commits, docs)

    if not bundle:
        # No evidence at all (empty repo, no docs) — nothing to extract. Return empty
        # without a model call; the caller decides whether an empty push is wanted.
        return ()

    # Redact BEFORE the text reaches the model (the privacy invariant). Hash the
    # redacted bundle (prefixed with the prompt's CACHE_VERSION) so the cache key
    # reflects exactly what we would send AND busts when the prompt changes.
    safe = redact(bundle).text
    content_hash = hashlib.sha256(
        f"{SKILLS_CACHE_VERSION}\n{safe}".encode("utf-8")
    ).hexdigest()

    cached = cache_get(_BUNDLE_KEY)
    if cached is not None and cached[0] == content_hash:
        # Unchanged evidence: reconstruct the Skills from the cached extraction — no
        # model call. signals is stored as a list; rebuild it as a tuple.
        return tuple(
            Skill(
                name=item["name"],
                category=item["category"],
                evidence=item["evidence"],
                weight=item["weight"],
                signals=tuple(item["signals"]),
            )
            for item in json.loads(cached[1])
        )

    # Cache miss / changed evidence: run the model. A failure propagates (see Raises) so
    # the CLI does not clobber the stored skills; we cache ONLY on success.
    extracted = extractor.extract(safe)
    cache_set(
        _BUNDLE_KEY,
        content_hash,
        json.dumps([s.as_dict() for s in extracted]),
    )
    return extracted

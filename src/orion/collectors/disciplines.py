# =============================================================================
# collectors/disciplines.py
# -----------------------------------------------------------------------------
# Responsible for: The DISCIPLINES signal (E2 Inc 4 slice 4b). Reads a project's
#                  own instruction/design/decision docs and reflects the working
#                  principles their author stated, as dashboard cards.
# Role in project: A dashboard-only signal (like the live checklist), NOT a report
#                  lane — it has a snapshot() (full current state) and no collect()/
#                  delta. The CLI pushes the snapshot to the relay's /disciplines
#                  endpoint, full-state, exactly as the checklist push works.
# Observe-not-originate: Orion reflects principles it OBSERVES in the user's docs;
#                  it never authors them. The user's docs are read UNMODIFIED — the
#                  reframing into cards is the optional, opt-in LLM step (extract.py).
# Privacy: each doc is REDACTED before it reaches the model (the privacy invariant —
#                  redact before any text reaches the LLM). The CLI redacts the
#                  extracted text again as a net before it leaves the machine.
# Cache: extraction is gated on a content hash (via the injected cache_get/cache_set,
#                  backed by the state store), so the model runs ONLY when a doc's
#                  (redacted) content actually changes — cheap and idempotent.
# =============================================================================

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from pathlib import Path

from orion.extract import CACHE_VERSION, Discipline, DisciplineExtractor, ExtractError
from orion.redact import redact

# The cache_get/cache_set seam: the collector stays decoupled from the state store
# (and trivially testable with a dict) while the CLI wires these to state.get_cache /
# state.set_cache. cache_get returns (content_hash, extraction_json) or None.
CacheGet = Callable[[str], "tuple[str, str] | None"]
CacheSet = Callable[[str, str, str], None]


def _repo_relative(doc: Path, repo_path: Path) -> str:
    """Return a doc's path relative to the repo, as a forward-slash string.

    Args:
        doc: The absolute doc path.
        repo_path: The project's repo root.

    Returns:
        The repo-relative path (e.g. "CLAUDE.md", "design/README.md") using POSIX
        separators. Falls back to the bare file name when the doc lives OUTSIDE the
        repo (e.g. an absolute global file), so an absolute home-dir path with the
        username is never emitted.

    Why:
        The dashboard footer reads "observed · <source>", and the design shows clean
        repo-relative paths. Beyond looks, this is a privacy fix: an absolute path
        leaks the home directory and username. Path.relative_to raises ValueError
        when `doc` is not under `repo_path`; we catch that and degrade to the
        basename rather than ever exposing the absolute path. as_posix keeps the
        separator stable across Windows/macOS/Linux.
    """
    try:
        return doc.relative_to(repo_path).as_posix()
    except ValueError:
        # The doc is not under the repo root (e.g. a shared global file). Use just
        # its name — informative for the footer, and leaks no directory structure.
        return doc.name


def snapshot(
    docs: Sequence[Path],
    repo_path: Path,
    extractor: DisciplineExtractor,
    *,
    cache_get: CacheGet,
    cache_set: CacheSet,
) -> tuple[Discipline, ...]:
    """Read the configured docs and return the disciplines observed across them.

    Args:
        docs: The project's discipline_docs (absolute paths), in config order.
        repo_path: The project's repo root, used to make each `source` repo-relative.
        extractor: The injected DisciplineExtractor (Anthropic via config, or a fake
            in tests). Called at most once per CHANGED doc.
        cache_get: Looks up a doc's cached (content_hash, extraction_json) or None.
        cache_set: Stores a doc's (content_hash, extraction_json) after a successful
            extraction.

    Returns:
        The observed Disciplines across all docs, in (doc order, then model order).
        Empty tuple when no docs yield principles.

    Why:
        This is the dashboard "live disciplines" read — current full state, like
        tasks.snapshot — so the relay can full-state-replace it. For each doc we
        redact BEFORE the model (privacy invariant), hash the redacted text, and
        reuse the cached extraction when the hash is unchanged so the LLM runs only
        on a real change. A missing/unreadable doc, or a doc whose extraction fails,
        is SKIPPED (fail-soft) rather than aborting the whole snapshot — disciplines
        are additive context, never a hard dependency, mirroring tasks.snapshot's
        tolerance. We cache ONLY on success, so a transient API failure retries next
        run instead of caching an empty result.
    """
    disciplines: list[Discipline] = []
    for doc in docs:
        try:
            text = doc.read_text(encoding="utf-8")
        except OSError:
            # Fail soft: a doc we cannot read simply contributes no disciplines; it
            # must never abort the snapshot of the docs we CAN read.
            continue

        # Redact BEFORE the text reaches the model (the privacy invariant). We hash
        # the redacted text (prefixed with the extractor's CACHE_VERSION) so the cache
        # key reflects exactly what we would send AND busts when the prompt changes —
        # not just when the doc content does.
        safe = redact(text).text
        content_hash = hashlib.sha256(
            f"{CACHE_VERSION}\n{safe}".encode("utf-8")
        ).hexdigest()
        source = _repo_relative(doc, repo_path)
        cache_key = str(doc)

        cached = cache_get(cache_key)
        if cached is not None and cached[0] == content_hash:
            # Unchanged doc: reconstruct the Disciplines from the cached extraction —
            # no model call. The cached dicts carry the source stamped at cache time.
            disciplines.extend(
                Discipline(**item) for item in json.loads(cached[1])
            )
            continue

        try:
            extracted = extractor.extract(safe, source=source)
        except ExtractError:
            # This one doc's extraction failed (API error, unparseable reply). Skip
            # it and do NOT cache, so the next run retries it. Other docs proceed.
            continue

        cache_set(
            cache_key,
            content_hash,
            json.dumps([d.as_dict() for d in extracted]),
        )
        disciplines.extend(extracted)
    return tuple(disciplines)

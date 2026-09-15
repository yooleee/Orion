# =============================================================================
# cli/_checklist.py
# -----------------------------------------------------------------------------
# Responsible for: The redacted checklist / About payloads a project contributes to a report AND
#                  to a standalone checklist push.
# Role in project: Shared by report.py (embeds the checklist in the blob) and push.py (pushes it
#                  alone); lives apart so the two never import each other.
# Assumptions: Redaction runs here on every lane, exactly as it does for the raw lane.
# =============================================================================
from __future__ import annotations


from pathlib import Path

from orion.collectors.about import read_about
from orion.collectors.tasks import ChecklistItem
from orion.collectors.tasks import snapshot as snapshot_tasks
from orion.collectors.tracker import snapshot as snapshot_tracker
from orion.config import (
    ProjectConfig,
)
from orion.redact import redact
from orion.report import (
    serialize_checklist_item,
)


def _checklist_source_files(project: ProjectConfig) -> list[Path]:
    """The local file(s) a project's live checklist is read from.

    Args:
        project: The project to inspect.

    Returns:
        The configured checklist-source paths in a stable order: tasks_file first (if
        set), then tracker_file (if set). Empty when the project has no checklist
        source at all.

    Why:
        The checklist push (guard) and the watch loop (the polled/printed file) both
        need to know "what feeds this project's checklist," and that became more than
        just tasks_file once the tracker collector landed. Centralizing the answer
        keeps the guard and the watch agreeing on the same set of sources.
    """
    files: list[Path] = []
    if project.tasks_file is not None:
        files.append(project.tasks_file)
    if project.tracker_file is not None:
        files.append(project.tracker_file)
    return files


def _redacted_checklist(project: ProjectConfig) -> tuple[list[ChecklistItem], int]:
    """Snapshot a project's current checklist and redact each item's text.

    Args:
        project: The project to read. Its checklist comes from a tasks_file (checkbox
            snapshot) and/or a tracker_file (status-aware snapshot) — both feed the
            same {text, done} surface.

    Returns:
        A (items, hits) pair: the redacted ChecklistItem list (an item whose text is
        ENTIRELY a secret — empty after redaction — is dropped to avoid a blank row),
        and the count of secrets scrubbed across all items. Returns ([], 0) when the
        project has neither a tasks_file nor a tracker_file.

    Why:
        BOTH the report push (_run_report) and the dedicated checklist push
        (cmd_checklist_push / its watch loop) must apply the SAME redaction to checklist
        item texts before they leave the machine — the non-negotiable privacy net.
        Factoring it here means that guarantee lives in ONE place and cannot drift
        between the two lanes, and means a new checklist source (the tracker) is
        redacted by construction without a second redaction site.
    """
    items: list[ChecklistItem] = []
    hits = 0
    # Gather raw items from every checklist source in a stable order (tasks first, then
    # tracker), then dedup by RAW text (KI-6 identity-by-text) so a title present in
    # both sources collapses to its first occurrence before redaction.
    raw_items: list[ChecklistItem] = []
    if project.tasks_file is not None:
        raw_items.extend(snapshot_tasks(project.tasks_file))
    if project.tracker_file is not None:
        raw_items.extend(snapshot_tracker(project.tracker_file))

    seen: set[str] = set()
    for item in raw_items:
        if item.text in seen:
            continue
        seen.add(item.text)
        scrub = redact(item.text)
        hits += scrub.hit_count
        # A secret inside an item name is replaced with a placeholder (not dropped), so
        # the item still shows with its done-state. We skip an item only if its text is
        # empty AFTER redaction (the whole label was a secret), to avoid a blank row.
        safe_text = scrub.text.strip()
        if safe_text:
            # due_date is already a normalized ISO date (or None), never raw user text, so
            # it rides through untouched — but the rebuild must preserve it (and key/group)
            # or they would be lost before reaching the wire. The `key` (a title) and
            # `group` (a heading) ARE user text, so both are redacted here too as a safety
            # net; their hits are NOT re-counted: a `key` is a substring of `text` (already
            # counted), and a `group` is shared across many items, so counting it per item
            # would multiply one secret into many. None (tasks/table items) stays None.
            safe_key = redact(item.key).text if item.key is not None else None
            safe_group = redact(item.group).text if item.group is not None else None
            items.append(
                ChecklistItem(
                    text=safe_text,
                    done=item.done,
                    due_date=item.due_date,
                    key=safe_key,
                    group=safe_group,
                    # `status` is a semantic enum value (not user free text), so it rides
                    # through untouched like done/due_date — but the rebuild must carry it
                    # or it would be lost before reaching the wire (E2 Inc 4, gap-8).
                    status=item.status,
                )
            )
    return items, hits


def _checklist_payload(project: ProjectConfig) -> list[dict]:
    """The project's redacted checklist as the wire payload (list of {text, done[, due_date]}).

    Why:
        The relay push and the watch loop both need the checklist in the exact JSON
        shape push_checklist sends. Deriving it here (over _redacted_checklist) keeps
        the redaction-then-serialize step in one place and gives the watch loop a value
        it can compare across ticks to detect changes.
    """
    items, _hits = _redacted_checklist(project)
    return [serialize_checklist_item(item) for item in items]


def _redacted_about(project: ProjectConfig) -> tuple[str | None, int]:
    """Read and redact a project's About line, or (None, 0) when there is none.

    Args:
        project: The project to read. Its About source is `about_file` (the band is off
            when that is None).

    Returns:
        A (about, hits) pair: the redacted About string (None when no about_file is set,
        the doc is missing/unreadable, or it has no prose), and the count of secrets
        scrubbed. None vs a string is the absent-vs-present distinction the caller uses to
        decide whether to send About at all.

    Why:
        About is user-authored content, so it MUST pass the same structured-lane redaction
        net as checklist item text before it leaves the machine (privacy rule). Factoring
        it here means both carriers — the report/ingest blob and the /checklist push —
        redact About in ONE place and cannot drift, exactly as _redacted_checklist does for
        item text. Reading fails soft to None (via read_about) so a mis-pointed doc never
        blocks a report or push.
    """
    if project.about_file is None:
        return None, 0
    raw = read_about(project.about_file)
    if raw is None:
        return None, 0
    scrub = redact(raw)
    # Unlike a checklist row we do NOT drop an all-secret About to None: the placeholder
    # left by redaction is a truthful "there was a secret here" signal, and About is a
    # single field, not a list where a blank row looks broken. read_about already ruled
    # out empty input, so scrub.text is non-empty.
    return scrub.text, scrub.hit_count

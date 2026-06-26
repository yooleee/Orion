# =============================================================================
# collectors/tracker.py
# -----------------------------------------------------------------------------
# Responsible for: The TRACKER structured-lane collector. Reads a "rich" project
#                  document (status-per-section applications + Markdown to-do
#                  tables) and turns it into the SAME checklist signal the tasks
#                  collector produces — without requiring the file to use
#                  GitHub-style "- [ ]" checkboxes.
# Role in project: The first real workload for the dashboard's live-checklist
#                  surface (E2 Inc 2 / 2.5). The motivating file is the user's
#                  applications tracker, where each application is a
#                  "## N. Title (type)" section carrying a "- **Status:**" field,
#                  and a "Non-Application To-Do" section is a Markdown table. It
#                  reuses the existing ChecklistItem(text, done) so the relay/
#                  dashboard wire format ({text, done}) is unchanged.
# Status mapping (the v1 contract): Submitted / Closed -> done; Not started /
#                  In progress -> open; an unknown/absent status on a numbered
#                  application is kept and treated as open (never silently dropped).
#                  Table rows have no status column, so they surface as open items.
# Identity model: an item is identified by its TEXT (KI-6), mirroring the tasks
#                  collector — re-ordering is safe; renaming a title makes the old
#                  text disappear and the new one appear; duplicates dedupe.
# Deadlines: deliberately NOT parsed in v1. The tracker carries real deadlines, but
#                  surfacing "due soon / overdue / at-risk" is the forward-looking
#                  layer (E2 Inc 3 / E1); v1 is status-only.
# Assumptions: a UTF-8 Markdown file. Structure is parsed by collectors/_markdown.py
#                  (sections + pipe tables); anything that does not match is ignored.
# =============================================================================

from __future__ import annotations

import json
import re
from pathlib import Path

from orion.collectors import LANE_STRUCTURED, CollectorResult
from orion.collectors._markdown import parse_sections, parse_tables

# Reuse the tasks collector's item record verbatim: the whole point of this collector
# is to feed the EXISTING {text, done} checklist surface, so it must emit the same
# type the dashboard push path already understands.
from orion.collectors.tasks import ChecklistItem


class TrackerError(Exception):
    """Raised when the configured tracker file cannot be read.

    Why:
        Mirrors TasksError so the CLI can catch a missing/unreadable tracker file
        specifically and print a clear, fixable message (the path to create or fix)
        instead of a traceback. A misconfigured path is a setup error, so it earns a
        kind message like the other collectors.
    """


# The canonical status vocabulary, in the file's own wording. Order does not matter
# for matching (the four are prefix-distinct), but is kept stable for readability.
_KNOWN_STATUSES = ("Not started", "In progress", "Submitted", "Closed")

# Which canonical statuses count as "done" for the binary checkbox. "Submitted" and
# "Closed" are terminal (the application is out the door / no longer actionable);
# "Not started" and "In progress" are still open work.
_DONE_STATUSES = frozenset({"Submitted", "Closed"})

# An application heading is a NUMBERED "## " section: "1. Claude Corps Fellow (job)".
# We use the leading "N. " both to identify application sections (vs. prose headings
# like "Non-Application To-Do") and to strip the number from the displayed title.
_NUMBERED_HEADING_RE = re.compile(r"^\d+\.\s*(.*)$")

# The displayed item text joins the title and its status with a plain " — "-free
# separator (the user avoids em dashes); " - " reads cleanly on the dashboard, e.g.
# "Claude Corps Fellow (job) - In progress".
_STATUS_SEP = " - "

# Table columns (case-insensitive) whose cell becomes a checklist item's text. The
# Non-Application table keys its task on "Task"; sub-goal breakdown tables use
# "Sub-goal". Any table lacking both is not a to-do table and is skipped.
_TASK_COLUMNS = ("task", "sub-goal")


def collect(tracker_file: Path, prior_marker: str | None) -> CollectorResult:
    """Collect applications newly moved to a done status since the last report.

    Args:
        tracker_file: Path to the tracker Markdown file (resolved absolute by config).
        prior_marker: The previous marker — a JSON list of the application item texts
            that were already "done" at the last report — or None on a first run.

    Returns:
        A CollectorResult on the STRUCTURED lane. raw_text is a Markdown bullet list
        of the applications newly moved to Submitted/Closed; has_activity is False
        (raw_text empty) when nothing new went done. new_marker is the full current
        done set serialized, so the next run measures its delta against the complete
        picture. Mirrors the tasks collector's delta semantics exactly.

    Why:
        The applications project is dashboard-only (it pushes via checklist-push, not
        report), but a collector still has to dispatch from _collect_for, so collect()
        exists and behaves like tasks.collect(): only DONE applications form the
        signal (table rows have no done-state, so they never enter the delta). Unlike
        snapshot(), a missing file here raises (mirroring tasks) — at report time a
        misconfigured path should fail loudly with a fixable message.
    """
    text = _read(tracker_file)

    # Done applications, in file order, deduped by text (the current done set).
    done_in_order: list[str] = []
    current: set[str] = set()
    for item in _application_items(text):
        if item.done and item.text not in current:
            current.add(item.text)
            done_in_order.append(item.text)

    stored: set[str] = set(json.loads(prior_marker)) if prior_marker else set()
    newly_done = [text_ for text_ in done_in_order if text_ not in stored]

    has_activity = bool(newly_done)
    raw_text = "\n".join(f"- {item}" for item in newly_done) if has_activity else ""
    new_marker = json.dumps(sorted(current))

    return CollectorResult(
        lane=LANE_STRUCTURED,
        raw_text=raw_text,
        new_marker=new_marker,
        has_activity=has_activity,
    )


def snapshot(tracker_file: Path) -> tuple[ChecklistItem, ...]:
    """Read the FULL current tracker (applications + table to-dos) as checklist items.

    Args:
        tracker_file: Path to the tracker Markdown file (resolved absolute by config).

    Returns:
        Checklist items, deduped by text (first wins). Application items come first
        (heading order), then table to-do rows (table order). Each application item's
        text is "<title> - <Status>" with done set per the status mapping; each table
        row is an open item. Empty tuple when the file is missing/unreadable or has no
        trackable content.

    Why:
        This is the dashboard's "live checklist" read — current state, not a delta —
        so it carries open and done together, exactly like tasks.snapshot(). It fails
        soft (returns () instead of raising) so attaching a tracker can never abort a
        push: a tracker we cannot read simply does not appear on the dashboard.
    """
    try:
        text = _read(tracker_file)
    except TrackerError:
        # Fail soft: a tracker we cannot read must never break the push, mirroring
        # tasks.snapshot()'s tolerance of a missing file.
        return ()

    items: list[ChecklistItem] = []
    seen: set[str] = set()
    # Applications first (in heading order), then table to-dos (in table order); both
    # are deduped against the same `seen` set so a title repeated across the two
    # sources collapses to its first occurrence (KI-6 identity-by-text).
    for item in (*_application_items(text), *_table_items(text)):
        if item.text and item.text not in seen:
            seen.add(item.text)
            items.append(item)
    return tuple(items)


def _application_items(text: str) -> list[ChecklistItem]:
    """Turn each numbered "## N. Title" application section into a ChecklistItem.

    Args:
        text: The tracker file's full text.

    Returns:
        One ChecklistItem per numbered application section, in heading order. The text
        is "<title> - <Status>" when the status is recognized, else just the title;
        done is True only for Submitted/Closed.

    Why:
        Numbered headings are how the file marks an application (prose headings like
        "Non-Application To-Do" are not numbered), so the leading "N. " both selects
        application sections and is stripped from the display title. We embed the
        status word because Not-started and In-progress both map to "open" — the
        checkbox alone would erase that distinction, which is exactly what family
        readers want to see.
    """
    items: list[ChecklistItem] = []
    for section in parse_sections(text):
        numbered = _NUMBERED_HEADING_RE.match(section.heading)
        if numbered is None:
            continue  # not an application section (e.g. a prose heading)
        title = numbered.group(1).strip()
        if not title:
            continue
        status = _canonical_status(section.fields.get("status", ""))
        if status is not None:
            text_ = f"{title}{_STATUS_SEP}{status}"
            done = status in _DONE_STATUSES
        else:
            # Unknown/absent status: keep the application (never drop it) as open, with
            # just its title so we never echo unrecognized free text onto the dashboard.
            text_ = title
            done = False
        items.append(ChecklistItem(text=text_, done=done))
    return items


def _table_items(text: str) -> list[ChecklistItem]:
    """Turn Markdown to-do table rows into open ChecklistItems.

    Args:
        text: The tracker file's full text.

    Returns:
        One open ChecklistItem per non-empty task row, across every table that has a
        "Task" or "Sub-goal" column, in document order.

    Why:
        The Non-Application To-Do list and sub-goal breakdowns are tables, not
        checkboxes, and have no status column — so every row surfaces as an open item
        (a deliberate v1 choice). Tables without a task-like column (should any exist)
        are not to-do lists and are skipped.
    """
    items: list[ChecklistItem] = []
    for table in parse_tables(text):
        column = _task_column(table.headers)
        if column is None:
            continue
        for row in table.rows:
            cell = row.get(column, "").strip()
            if cell:
                items.append(ChecklistItem(text=cell, done=False))
    return items


def _canonical_status(value: str) -> str | None:
    """Match a raw status value to its canonical status, tolerating trailing context.

    Args:
        value: The raw text after "- **Status:**" (e.g.
            "In progress (partial application already done) — low priority").

    Returns:
        The canonical status string (one of _KNOWN_STATUSES) when the value STARTS
        with a known status, else None.

    Why:
        Status lines carry free-text qualifiers after the status word, so an exact
        match would miss them. A case-insensitive prefix match pins the canonical
        status while ignoring the trailing note. The four statuses are prefix-distinct,
        so the first match is unambiguous.
    """
    low = value.strip().lower()
    for status in _KNOWN_STATUSES:
        if low.startswith(status.lower()):
            return status
    return None


def _task_column(headers: list[str]) -> str | None:
    """Return the header (verbatim) whose column carries a row's task text.

    Args:
        headers: A table's header cells.

    Returns:
        The actual header string matching "Task" or "Sub-goal" (case-insensitive), or
        None when the table has neither.

    Why:
        Rows are keyed by their real header text (parse_tables preserves it), so we
        return the matching header verbatim to index each row dict, while comparing
        case-insensitively so "Task"/"task"/"Sub-goal" all resolve.
    """
    for header in headers:
        if header.strip().lower() in _TASK_COLUMNS:
            return header
    return None


def _read(tracker_file: Path) -> str:
    """Read the tracker file as UTF-8, raising TrackerError on any failure.

    Args:
        tracker_file: Path to the tracker file.

    Returns:
        The file's full text.

    Why:
        Centralizes the read so there is one clear message for the common case (the
        file does not exist yet) and a consistent failure type the CLI can catch,
        mirroring tasks._read. No read cap: a tracker is small by nature.
    """
    try:
        return tracker_file.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise TrackerError(
            f"Tracker file not found: {tracker_file}. Create it or fix `tracker_file` "
            f"in orion.toml."
        ) from exc
    except OSError as exc:
        # Permission denied, is-a-directory, decode errors, etc. — surface the path
        # and the underlying reason rather than a raw traceback.
        raise TrackerError(f"Could not read tracker file {tracker_file}: {exc}") from exc

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
# Milestones: each item carries a `group` (E2 Inc 3, Unit 5) — "Applications" for the
#                  numbered application sections, the table's nearest heading for to-do
#                  rows — so the relay can roll items up into per-section milestones
#                  (progress, nearest deadline, at-risk). Ungrouped items have group=None.
# Deadlines: PARSED and carried on each item (due_date), but NOT yet surfaced here.
#                  Reading the deadline the tracker already holds is the on-ramp to the
#                  forward-looking layer (E2 Inc 3 / E1, Unit 1); deriving "due soon /
#                  overdue / at-risk" and rendering it come later in that ladder. Only
#                  EXPLICIT-YEAR dates are accepted (ISO, or "Month D, YYYY" with
#                  trailing context tolerated); a year-less form like "Sun, Jun 14"
#                  parses to None on purpose — see _parse_deadline for why.
# Assumptions: a UTF-8 Markdown file. Structure is parsed by collectors/_markdown.py
#                  (sections + pipe tables); anything that does not match is ignored.
# =============================================================================

from __future__ import annotations

import json
import re
from datetime import date
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

# Table columns (case-insensitive) whose cell carries a row's deadline. The tracker's
# tables label it "Deadline"; "Due"/"Target" are accepted aliases so other docs slot in.
_DEADLINE_COLUMNS = ("deadline", "due", "target")

# Month name -> number, full names plus 3-letter abbreviations, lower-cased. Built
# explicitly rather than relying on strptime's %B/%b so deadline parsing never depends
# on the process LOCALE (a cross-platform requirement); the tracker is written in English.
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
_MONTHS.update({name[:3]: num for name, num in list(_MONTHS.items())})

# A LEADING ISO date, e.g. "2026-06-30" (anything after the day is ignored).
_ISO_DATE_RE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})\b")
# A LEADING "Month D, YYYY" date, full or abbreviated month, with an EXPLICIT 4-digit
# year, e.g. "July 17, 2026" or "June 12, 2026, 23:59 AoE …" (trailing context ignored).
# The year-less table form "Sun, Jun 14" has no 4-digit year here and so will not match.
_MONTH_DATE_RE = re.compile(r"^\s*([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})\b")


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
        # A "- **Deadline:**" (or "- **Due:**") field, normalized to ISO or None. Read
        # independently of status, so even an open application carries its due date.
        deadline = _parse_deadline(
            section.fields.get("deadline") or section.fields.get("due")
        )
        status = _canonical_status(section.fields.get("status", ""))
        if status is not None:
            text_ = f"{title}{_STATUS_SEP}{status}"
            done = status in _DONE_STATUSES
        else:
            # Unknown/absent status: keep the application (never drop it) as open, with
            # just its title so we never echo unrecognized free text onto the dashboard.
            text_ = title
            done = False
        # The bare title is the STABLE forward-store identity (key): `text_` embeds the
        # status, so it changes when the application advances (Not started -> Submitted),
        # but the title does not. Carrying key=title lets the relay track the SAME item's
        # deadline/done over time across that change (E2 Inc 3, Unit 3).
        #
        # Every numbered section is an application, so they all share ONE milestone,
        # "Applications" (E2 Inc 3, Unit 5) — a single "N/M applications, next due ..."
        # roll-up reads better than four groups of one. The label is fixed (not the H1)
        # to keep it clean; revisit only if the tracker is reused for a non-apps doc.
        items.append(
            ChecklistItem(
                text=text_, done=done, due_date=deadline, key=title, group="Applications"
            )
        )
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
        # Resolve the deadline column once per table (None when the table has none);
        # the live tables use a year-less "Deadline" form, which parses to None — the
        # seam is wired so a future table with explicit-year dates carries them.
        deadline_column = _deadline_column(table.headers)
        for row in table.rows:
            cell = row.get(column, "").strip()
            if cell:
                deadline = (
                    _parse_deadline(row.get(deadline_column, ""))
                    if deadline_column is not None
                    else None
                )
                # The table's nearest heading is the row's milestone (E2 Inc 3, Unit 5):
                # the "# Non-Application To-Do" table and each "### Task N" breakdown table
                # become their own milestones. None when the table sits under no heading,
                # which leaves those rows ungrouped (no milestone) rather than guessing.
                items.append(
                    ChecklistItem(
                        text=cell, done=False, due_date=deadline, group=table.heading
                    )
                )
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


def _parse_deadline(value: str | None) -> str | None:
    """Normalize a raw deadline string to an ISO "YYYY-MM-DD" date, or None.

    Args:
        value: The raw text of a "- **Deadline:**"/"- **Due:**" field or a
            Deadline/Due/Target table cell (e.g. "July 17, 2026", "2026-06-30",
            "June 12, 2026, 23:59 AoE (UTC-12) — about 4:59 AM PT"), or None when absent.

    Returns:
        The date as an ISO "YYYY-MM-DD" string when `value` STARTS with a date we accept
        — ISO, or "Month D, YYYY" (full or abbreviated month, EXPLICIT 4-digit year) with
        any trailing time/timezone context ignored. None for an absent, empty,
        unrecognized, year-less, or calendar-invalid value.

    Why:
        This is the forward-looking layer's on-ramp (E2 Inc 3): the deadlines the tracker
        already carries, finally read instead of discarded. We accept only EXPLICIT-YEAR
        dates on purpose — inferring a missing year is ambiguous, and for a tracker that
        holds genuinely-past deadlines it would mislabel them as upcoming, so a year-less
        value is left unparsed rather than guessed (a documented, additive-to-revisit
        limitation). Parsing NEVER raises: an unreadable deadline yields None so a typo
        can never break the checklist. Mirrors _canonical_status's "match the leading
        token, tolerate trailing context" shape. A normalized return also means due_date
        is always sanitized digits-and-hyphens, never raw user free text on the wire.
    """
    if not value:
        return None
    text = value.strip()

    iso = _ISO_DATE_RE.match(text)
    if iso is not None:
        year, month, day = (int(group) for group in iso.groups())
    else:
        named = _MONTH_DATE_RE.match(text)
        if named is None:
            return None
        month = _MONTHS.get(named.group(1).lower())
        if month is None:
            return None  # a leading word in date position that is not a month name
        day, year = int(named.group(2)), int(named.group(3))

    # date() validates the calendar (rejects e.g. Feb 30 or month 13). An impossible
    # date is treated as unparseable, not an error — "never fail the parse" holds.
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _deadline_column(headers: list[str]) -> str | None:
    """Return the header (verbatim) whose column carries a row's deadline, if any.

    Args:
        headers: A table's header cells.

    Returns:
        The actual header string matching "Deadline"/"Due"/"Target" (case-insensitive),
        or None when the table has no deadline column.

    Why:
        Symmetric to _task_column: rows are keyed by their real header text, so we return
        the matching header verbatim to index each row dict, comparing case-insensitively.
        Returning None (rather than raising) lets a to-do table without a deadline column
        simply carry no due dates — deadlines are optional context, not required.
    """
    for header in headers:
        if header.strip().lower() in _DEADLINE_COLUMNS:
            return header
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

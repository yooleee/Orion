# =============================================================================
# collectors/incubator.py
# -----------------------------------------------------------------------------
# Responsible for: The INCUBATOR structured-lane collector. Reads a local
#                  "idea pipeline" Markdown table (an incubator's index.md) and
#                  reports ideas that are NEW or whose STATUS changed since the
#                  last report.
# Role in project: A structured signal — it returns lane="structured", so the
#                  orchestrator passes its text straight through (NO LLM). Only
#                  raw git activity is ever summarized by Claude. This is Orion's
#                  fifth collector and the first one whose source is a structured
#                  table rather than a checklist/free-text note (D4).
# How "changed" works: the marker is the full {idea -> status} map, serialized as
#                  sorted-keys JSON. The delta is computed by comparing the current
#                  map to the stored one: an idea absent from the stored map is
#                  NEW; an idea whose status differs is a TRANSITION. Re-running
#                  with no table edits reports nothing. The marker is opaque to the
#                  state store (state.py never interprets it).
# Identity model (mirrors tasks.py, see docs/known-issues.md KI-6): an idea is
#                  identified by its TITLE text (the Idea cell, link text if it is
#                  a Markdown link), not its row position. Re-ordering rows is safe;
#                  RENAMING an idea makes the old title disappear and the new one
#                  count as new; two rows with the same title collapse to one (the
#                  later row wins). A pitch-only edit is deliberately NOT activity:
#                  the marker tracks title+status only.
# Assumptions: the file is UTF-8 and contains a GitHub-style Markdown table whose
#                  header row has at least an "Idea" and a "Status" column (a
#                  "pitch" column is used for new-idea context when present). Extra
#                  columns and column re-ordering are tolerated (we index by header).
# =============================================================================

from __future__ import annotations

import json
import re
from pathlib import Path

from orion.collectors import LANE_STRUCTURED, CollectorResult


class IncubatorError(Exception):
    """Raised when the configured incubator file cannot be read.

    Why:
        A dedicated exception lets the CLI catch a missing/unreadable incubator
        file specifically and print a clear, fixable message (the path to create
        or fix) instead of dumping a traceback. A misconfigured path is a
        user/setup error, so it deserves a kind message, mirroring
        TasksError/NotesError/ConfigError.
    """


# A Markdown inline link: "[text](target)". We capture the link TEXT (group 1) so
# an Idea cell like "[VLM Photo Overlay](ideas/vlm.md)" identifies the idea by its
# human title, not the file path. A cell with no link is used verbatim.
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")

# A Markdown table separator cell: optional leading/trailing colon (alignment) and
# one-or-more dashes, e.g. "---", ":--", "--:". A row whose cells are ALL of this
# shape is the header/body divider and carries no data, so it is skipped.
_SEPARATOR_CELL_RE = re.compile(r"^:?-+:?$")


def collect(incubator_file: Path, prior_marker: str | None) -> CollectorResult:
    """Collect new ideas and status transitions since the last report.

    Args:
        incubator_file: Path to the incubator's Markdown index table (resolved
            absolute by config).
        prior_marker: The previous marker — sorted-keys JSON of the {idea -> status}
            map at the last report — or None on a first run.

    Returns:
        A CollectorResult on the STRUCTURED lane. raw_text is a report-ready block
        of transition lines (new ideas, with their one-line pitch for context, plus
        "old → new" status changes), already audience-ready so no LLM touches it.
        has_activity is False (and raw_text empty) when nothing is new or changed.
        new_marker is the FULL current {idea -> status} map serialized, so the next
        run measures its delta against the complete picture.

    Why:
        Reporting only what is NEW or CHANGED keeps each update a true delta — a
        mentor or family member hears "this idea graduated," not the whole table
        every run. We serialize the full current map (not just the delta) as the
        marker so an idea that moved two runs ago is never re-reported even if the
        file is edited in between — the same delta strategy tasks.py uses, applied
        to a {key -> value} map instead of a set.
    """
    text = _read(incubator_file)
    current, pitches = _parse_table(text)

    # The stored map of already-reported {idea -> status}. An empty/None marker
    # means "first run": every idea currently in the table is new.
    stored: dict[str, str] = json.loads(prior_marker) if prior_marker else {}

    raw_text = _format_changes(current, pitches, stored)
    has_activity = bool(raw_text)

    # Marker is the full current map with sorted keys, for a stable, order-
    # independent serialization (re-ordering the table never looks like a change).
    new_marker = json.dumps(current, sort_keys=True)

    return CollectorResult(
        lane=LANE_STRUCTURED,
        raw_text=raw_text,
        new_marker=new_marker,
        has_activity=has_activity,
    )


def _read(incubator_file: Path) -> str:
    """Read the incubator file as UTF-8, raising IncubatorError on any failure.

    Args:
        incubator_file: Path to the incubator index file.

    Returns:
        The file's full text.

    Why:
        Centralizing the read means one clear error message for the common case
        (the file does not exist yet) and a consistent failure type the CLI can
        catch — the same pattern as the tasks/notes readers, kept separate rather
        than shared because the collectors are independent units (a shared helper
        would couple them for almost no savings).
    """
    try:
        return incubator_file.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise IncubatorError(
            f"Incubator file not found: {incubator_file}. Create it or fix "
            f"`incubator_file` in orion.toml."
        ) from exc
    except OSError as exc:
        # Permission denied, is-a-directory, decode errors, etc. — surface the
        # path and the underlying reason rather than a raw traceback.
        raise IncubatorError(
            f"Could not read incubator file {incubator_file}: {exc}"
        ) from exc


def _parse_table(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """Parse the idea-pipeline Markdown table into {idea -> status} and {idea -> pitch}.

    Args:
        text: The full incubator file text.

    Returns:
        A pair of dicts, both keyed by idea title in FILE ORDER:
        - {idea -> status}: every idea row's status (the delta is computed from this).
        - {idea -> pitch}: the one-line pitch where a pitch column exists (used to
          give a brand-new idea some context in the report). May omit ideas with no
          pitch.

    Why:
        We locate columns by the HEADER ("Idea", "Status", and any column whose name
        contains "pitch") rather than by fixed position, so extra columns or a
        re-ordered table still parse — the file is hand-maintained and its shape may
        drift. An Idea cell that is a Markdown link is reduced to its link text so an
        idea is identified by its human title. A file with no table (or a table
        lacking the Idea/Status columns) yields empty maps rather than an error: an
        incubator with no ideas yet is a valid empty state, exactly like an empty
        notes file is "no activity," not a failure.
    """
    current: dict[str, str] = {}
    pitches: dict[str, str] = {}

    lines = text.splitlines()
    header_index, columns = _find_header(lines)
    if header_index is None:
        # No "Idea"/"Status" table found — treat as an empty pipeline (no error).
        return current, pitches
    idx_idea, idx_status, idx_pitch = columns

    # Rows after the header. The first data-shaped row is usually the "---"
    # separator, which _is_separator filters out. We stop at the first line that is
    # not a table row, so a later unrelated table or trailing prose is not consumed.
    for line in lines[header_index + 1 :]:
        if "|" not in line:
            break
        cells = _split_row(line)
        if _is_separator(cells):
            continue
        # Guard against a short/ragged row that lacks the columns we need.
        if len(cells) <= max(idx_idea, idx_status):
            continue
        title = _link_text(cells[idx_idea])
        status = cells[idx_status].strip()
        if not title or not status:
            continue
        current[title] = status  # a duplicate title keeps the later row (documented)
        if idx_pitch is not None and idx_pitch < len(cells):
            pitch = cells[idx_pitch].strip()
            if pitch:
                pitches[title] = pitch

    return current, pitches


def _find_header(lines: list[str]) -> tuple[int | None, tuple[int, int, int | None]]:
    """Find the table header row and the column indices we care about.

    Args:
        lines: The file split into lines.

    Returns:
        A pair (header_index, (idx_idea, idx_status, idx_pitch)). header_index is
        None when no header row containing both an "Idea" and a "Status" column is
        found, in which case the column indices are meaningless and ignored.

    Why:
        The header is the one row that names the columns, so it is where we learn
        which position holds the idea, the status, and (optionally) the pitch. We
        match case-insensitively and by substring for "pitch" so small wording
        differences ("One-line pitch") still resolve, keeping the parser tolerant of
        a hand-edited file.
    """
    for index, line in enumerate(lines):
        if "|" not in line:
            continue
        cells = [c.lower() for c in _split_row(line)]
        if "idea" in cells and "status" in cells:
            idx_pitch = next(
                (i for i, c in enumerate(cells) if "pitch" in c), None
            )
            return index, (cells.index("idea"), cells.index("status"), idx_pitch)
    return None, (0, 0, None)


def _split_row(line: str) -> list[str]:
    """Split one Markdown table row into trimmed cell strings.

    Args:
        line: A single table row, e.g. "| A | B |".

    Returns:
        The cell texts with surrounding whitespace and the optional leading/trailing
        pipes removed, e.g. ["A", "B"].

    Why:
        GitHub-style tables usually wrap rows in leading/trailing pipes; stripping
        them avoids the empty first/last cells that a naive split would produce, so
        column indices line up with the visible columns.
    """
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _is_separator(cells: list[str]) -> bool:
    """Whether a split row is the header/body divider (all "---"-style cells).

    Args:
        cells: The trimmed cells of one row.

    Returns:
        True if every cell is a Markdown separator cell (dashes with optional
        alignment colons), so the row carries no data.

    Why:
        The divider row sits between the header and the data and must not be read as
        an idea. Checking every cell (rather than just the first) avoids mistaking a
        real row whose first cell happens to be empty for the separator.
    """
    return bool(cells) and all(_SEPARATOR_CELL_RE.match(cell) for cell in cells)


def _link_text(cell: str) -> str:
    """Reduce an Idea cell to the idea's title, unwrapping a Markdown link.

    Args:
        cell: The raw Idea-column cell text.

    Returns:
        The link text when the cell is/contains a Markdown link, else the cell
        verbatim.

    Why:
        Ideas are usually written as "[Title](ideas/title.md)" so the index links to
        the idea's own file. We identify an idea by its human Title, so the path is
        dropped — and a plain-text cell (no link) still works as its own title.
    """
    match = _LINK_RE.search(cell)
    return match.group(1).strip() if match else cell


def _format_changes(
    current: dict[str, str], pitches: dict[str, str], stored: dict[str, str]
) -> str:
    """Render new ideas and status transitions as a report-ready block.

    Args:
        current: The current {idea -> status} map, in file order.
        pitches: The {idea -> pitch} map (for context on new ideas).
        stored: The previously-reported {idea -> status} map.

    Returns:
        A block of transition lines (empty string when nothing is new or changed):
        a new idea becomes "- New idea: Title (status)" with its pitch on an indented
        line below; a status change becomes "- Title: old → new". Ideas dropped from
        the table are NOT reported.

    Why:
        The structured lane is "already report-ready," so the collector produces the
        final section body itself (the merge step adds the "## Idea pipeline" header,
        so this returns just the lines). New ideas carry their pitch because a
        recipient seeing an idea for the first time needs to know what it is; a status
        change needs only the movement. We iterate `current` in file order so the
        report reads top-to-bottom like the table. Removals are silent — mirroring
        tasks.py not reporting an un-checked item — because "we stopped tracking this"
        is rarely the update a supervisor wants, and the marker advances past it
        regardless on the next change.
    """
    lines: list[str] = []
    for title, status in current.items():
        if title not in stored:
            lines.append(f"- New idea: {title} ({status})")
            pitch = pitches.get(title)
            if pitch:
                # Two-space indent visually nests the pitch under its idea line.
                lines.append(f"  {pitch}")
        elif stored[title] != status:
            lines.append(f"- {title}: {stored[title]} → {status}")
    return "\n".join(lines)

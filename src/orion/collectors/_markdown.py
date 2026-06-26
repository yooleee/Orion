# =============================================================================
# collectors/_markdown.py
# -----------------------------------------------------------------------------
# Responsible for: Small, generic Markdown-structure extractors shared by the
#                  collectors that read a "rich" document (not a checkbox list).
# Role in project: The DRY seam behind "turn a rich project document into checklist
#                  signal." The tracker collector (collectors/tracker.py) uses BOTH
#                  helpers — parse_sections() for "## N. Title" application sections
#                  with a "- **Status:**" field, and parse_tables() for the
#                  Non-Application To-Do / sub-goal tables. A later add-project
#                  bootstrap (Unit B) reuses parse_tables() to turn a roadmap table
#                  into checklist items. Keeping these here (not inside tracker.py)
#                  makes the reuse explicit and keeps tracker.py focused on mapping
#                  parsed structure -> ChecklistItem.
# Assumptions: input is UTF-8 Markdown text (already read from disk by the caller).
#                  These are deliberately small parsers for the constructs Orion
#                  actually consumes (ATX "##" headings, "- **Field:** value" lines,
#                  GitHub pipe tables) — NOT a general CommonMark parser. Anything
#                  that does not match is simply ignored, never an error.
# =============================================================================

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Section:
    """One "## " heading and the bold "- **Field:** value" fields directly under it.

    Args:
        heading: The heading text after the "## " marker, stripped (e.g.
            "1. Claude Corps Fellow (job)"). The leading "N. " number, if any, is
            left intact — stripping it is the caller's choice.
        fields: Field name (lower-cased) -> value, for each "- **Name:** value"
            bullet found between this heading and the next "#"/"##" heading. First
            occurrence of a name wins (a later duplicate does not overwrite it).

    Why:
        The applications tracker encodes each application as a "## N. Title" section
        whose state lives in a "- **Status:**" bullet. Returning a tiny named record
        (heading + a field map) lets the tracker read section.fields["status"]
        without re-implementing the heading/field scan, and leaves room for it to
        read other fields later (e.g. a deadline in Inc 3) without touching this
        parser.
    """

    heading: str
    fields: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Table:
    """One parsed GitHub pipe table: its column headers and its data rows.

    Args:
        headers: The header cells, in column order, stripped.
        rows: One dict per data row, mapping each header to that row's cell text.
            A row with fewer cells than headers leaves the missing columns absent;
            extra cells beyond the headers are dropped.
        heading: The text of the nearest heading (any level, "#".."######") that
            PRECEDES this table in the document, stripped, or None when no heading
            comes before it. Lets a caller group a table by the section it sits under
            (E2 Inc 3 Unit 5: the to-do table under "# Non-Application To-Do" and each
            "### Task N" breakdown table become their own milestones). Additive and
            defaulted last, so existing Table(headers=..., rows=...) construction and
            unpacking are unaffected.

    Why:
        Both the Non-Application To-Do table and the roadmap table (Unit B) are read
        by column NAME ("Task", "Sub-goal", "Scope", "Status"), not position, so a
        per-row dict keyed by header is the natural shape. Keeping headers too lets a
        caller decide which column carries the item text. `heading` is the table's
        document context — the parser already scans line-by-line, so remembering the
        last heading seen costs nothing and gives the tracker a milestone label without
        re-parsing the file.
    """

    headers: list[str]
    rows: list[dict[str, str]]
    heading: str | None = None


# An ATX heading at level 1 or 2: "# ..." or "## ...". We capture the level (the run
# of '#') and the text. A level-1/2 heading ends the previous "## " section; deeper
# headings ("### ") are treated as content within a section (not a new boundary), so a
# sub-heading inside an application does not split it.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")

# A "- **Field:** value" bullet: optional indent, a "-"/"*" bullet, then a bold run
# whose text ends in a colon ("**Status:**"), then the value (the rest of the line).
# Group 1 is the field name (without the trailing colon); group 2 is the value.
_FIELD_RE = re.compile(r"^\s*[-*]\s+\*\*(.+?):\*\*\s*(.*?)\s*$")

# A pipe-table SEPARATOR row, e.g. "|---|:--:|----|": after stripping outer pipes,
# every cell is dashes with optional leading/trailing colons (alignment markers).
_TABLE_SEP_RE = re.compile(r"^\s*\|?(?:\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?\s*$")


def parse_sections(text: str) -> list[Section]:
    """Extract "## "-level sections and their "- **Field:** value" fields.

    Args:
        text: The full Markdown document text.

    Returns:
        One Section per "#"/"##" heading, in document order. A document with no such
        headings yields an empty list. Fields are collected only from the bullet
        lines between a heading and the next level-1/2 heading.

    Why:
        The tracker needs each application's title and its Status without caring about
        the surrounding prose, links, or notes. Scanning line-by-line (rather than a
        full Markdown parse) keeps this dependency-free and predictable: only the two
        line shapes we care about (headings and bold-field bullets) are recognized,
        and everything else is ignored.
    """
    sections: list[Section] = []
    current: Section | None = None

    for line in text.splitlines():
        heading = _HEADING_RE.match(line)
        if heading is not None:
            level = len(heading.group(1))
            # Only level 1/2 headings start a new section boundary; "### " and deeper
            # are content inside the current section (so a sub-heading within an
            # application does not begin a new "application").
            if level <= 2:
                current = Section(heading=heading.group(2).strip(), fields={})
                sections.append(current)
            continue

        if current is None:
            # Field bullets that appear before the first heading have no section to
            # attach to; skip them (the tracker only reads fields under a heading).
            continue

        field_match = _FIELD_RE.match(line)
        if field_match is not None:
            name = field_match.group(1).strip().lower()
            value = field_match.group(2).strip()
            # First occurrence wins: a section's canonical Status is its first
            # "- **Status:**" line, mirroring the tasks collector's first-wins dedup.
            current.fields.setdefault(name, value)

    return sections


def parse_tables(text: str) -> list[Table]:
    """Extract every GitHub pipe table in the document.

    Args:
        text: The full Markdown document text.

    Returns:
        One Table per recognized pipe table, in document order. A table is recognized
        only when a header row is immediately followed by a separator row (the
        "|---|---|" line); a lone pipe line with no separator is not a table and is
        ignored. Empty list when the document has no tables. Each Table carries the
        text of the nearest heading preceding it (heading=None if none).

    Why:
        The Non-Application To-Do list and the sub-goal breakdowns are Markdown
        tables, and (Unit B) a roadmap is too. Requiring the separator row is what
        distinguishes a real table from an ordinary line that happens to contain a
        "|", so prose with a pipe is never misread as a one-row table. We also track
        the most recent heading while scanning (one extra match per line, no second
        pass) so each table knows the section it lives under — the milestone grouping
        the tracker needs (Unit 5).
    """
    tables: list[Table] = []
    lines = text.splitlines()
    i = 0
    n = len(lines)
    # The nearest heading seen so far while scanning top-to-bottom. Any heading line
    # (levels 1-6) updates it, so a table is grouped by whatever section most recently
    # opened above it. None until the first heading appears.
    current_heading: str | None = None

    while i < n:
        line = lines[i]
        # Track the section context: a heading line (any level) becomes the heading any
        # following table is attributed to. Checked before the table test because a
        # heading line never contains a leading pipe, so the two never collide.
        heading = _HEADING_RE.match(line)
        if heading is not None:
            current_heading = heading.group(2).strip()
            i += 1
            continue
        # A header row is any line containing a pipe whose NEXT line is a separator.
        # We check the separator first because that is the cheap, decisive signal.
        if "|" in line and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1]):
            headers = _split_row(line)
            rows: list[dict[str, str]] = []
            j = i + 2
            # Consume contiguous data rows: any line with a pipe that is not blank.
            # The first line without a pipe (or a blank line) ends the table.
            while j < n and "|" in lines[j] and lines[j].strip():
                cells = _split_row(lines[j])
                # Key cells by header; zip stops at the shorter side, so a short row
                # leaves trailing columns absent and an over-long row drops extras.
                rows.append(dict(zip(headers, cells)))
                j += 1
            tables.append(Table(headers=headers, rows=rows, heading=current_heading))
            i = j
            continue
        i += 1

    return tables


def _split_row(line: str) -> list[str]:
    """Split one pipe-table row into stripped cell strings.

    Args:
        line: A raw table row, e.g. "| 1 | Task | _(fill in)_ | Sun, Jun 14 |".

    Returns:
        The cell texts in column order, each stripped. Leading/trailing pipes (and
        the empty cells they imply) are removed first.

    Why:
        Centralizes the one cell-splitting rule so the header row and the data rows
        are tokenized identically. We strip the outer pipes before splitting so the
        common "| a | b |" form does not yield phantom empty first/last cells.
    """
    return [cell.strip() for cell in line.strip().strip("|").split("|")]

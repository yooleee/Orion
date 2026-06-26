# =============================================================================
# tests/test_markdown_extract.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the shared Markdown extractors in
#                  collectors/_markdown.py (parse_sections, parse_tables) at the
#                  parser level — the edge cases that are awkward to reach through
#                  a collector but matter for both the tracker and the future
#                  roadmap-bootstrap reuse.
# Role in project: These two helpers are the DRY seam for "rich document ->
#                  checklist signal." If their boundary rules drift (what starts a
#                  section, what counts as a table), every consumer drifts with them.
# =============================================================================

from orion.collectors._markdown import Section, Table, parse_sections, parse_tables


def test_parse_sections_collects_bold_fields_under_each_heading():
    """A "## " heading captures the "- **Field:** value" bullets beneath it.

    Why this matters: the tracker reads each application's Status out of these
    fields, so the heading->fields association is the core contract.
    """
    text = (
        "## 1. First (job)\n"
        "- **Type:** Job\n"
        "- **Status:** Not started\n"
        "## 2. Second (course)\n"
        "- **Status:** Submitted\n"
    )
    sections = parse_sections(text)

    assert sections == [
        Section(heading="1. First (job)", fields={"type": "Job", "status": "Not started"}),
        Section(heading="2. Second (course)", fields={"status": "Submitted"}),
    ]


def test_parse_sections_first_field_value_wins():
    """A repeated field name keeps its first value (mirrors first-wins dedup).

    Why this matters: a stray second "- **Status:**" line must not silently change
    an application's state; the first declaration is canonical.
    """
    text = "## 1. A (job)\n- **Status:** In progress\n- **Status:** Closed\n"
    assert parse_sections(text)[0].fields["status"] == "In progress"


def test_parse_sections_level_three_heading_does_not_split_a_section():
    """A "### " sub-heading is content, not a new section boundary.

    Why this matters: an application may contain a "### " sub-heading; it must not be
    mistaken for a new application, which would scramble the field association.
    """
    text = (
        "## 1. A (job)\n"
        "### A sub-heading\n"
        "- **Status:** In progress\n"
        "## 2. B (job)\n"
    )
    sections = parse_sections(text)
    # Only the two "## " headings are sections; the "### " line is absorbed into the
    # first, so its Status still attaches to application 1.
    assert [s.heading for s in sections] == ["1. A (job)", "2. B (job)"]
    assert sections[0].fields["status"] == "In progress"


def test_parse_sections_ignores_field_bullets_before_any_heading():
    """Bullets before the first heading have no section to attach to and are dropped.

    Why this matters: front-matter-style bullets at the top of a file must not crash
    or attach to a phantom section.
    """
    text = "- **Status:** Stray\nSome prose.\n## 1. Real (job)\n- **Status:** Closed\n"
    sections = parse_sections(text)
    assert len(sections) == 1
    assert sections[0].fields == {"status": "Closed"}


def test_parse_tables_reads_header_keyed_rows():
    """A pipe table yields one dict per data row, keyed by header.

    Why this matters: consumers read columns by NAME ("Task", "Deadline"), so the
    header->cell mapping is the table contract.
    """
    text = (
        "| # | Task | Deadline |\n"
        "|---|------|----------|\n"
        "| 1 | Do the thing | Sun, Jun 14 |\n"
        "| 2 | Do another | Tue, Jun 16 |\n"
    )
    tables = parse_tables(text)
    assert tables == [
        Table(
            headers=["#", "Task", "Deadline"],
            rows=[
                {"#": "1", "Task": "Do the thing", "Deadline": "Sun, Jun 14"},
                {"#": "2", "Task": "Do another", "Deadline": "Tue, Jun 16"},
            ],
        )
    ]


def test_parse_tables_requires_a_separator_row():
    """A pipe line with no "|---|" separator beneath it is NOT a table.

    Why this matters: ordinary prose containing a "|" must never be misread as a
    one-row table — the separator row is the decisive signal.
    """
    text = "Some prose with a | pipe in it.\nMore prose.\n"
    assert parse_tables(text) == []


def test_parse_tables_short_row_leaves_trailing_columns_absent():
    """A row with fewer cells than headers simply omits the missing columns.

    Why this matters: a ragged table row should degrade gracefully (no crash, no
    misaligned cells) rather than raising.
    """
    text = "| A | B | C |\n|---|---|---|\n| only-a | only-b |\n"
    rows = parse_tables(text)[0].rows
    assert rows == [{"A": "only-a", "B": "only-b"}]  # C absent, not blank


def test_parse_tables_attributes_each_table_to_its_nearest_heading():
    """Each table carries the nearest heading that precedes it (any level).

    Why this matters: Unit 5 groups table rows into milestones by the section they
    live under, so a table beneath "# Non-Application To-Do" and a later one beneath
    "### Task 2" must each report their OWN heading — not the document's first one.
    """
    text = (
        "# Non-Application To-Do\n"
        "\n"
        "| Task | Deadline |\n"
        "|------|----------|\n"
        "| Do the thing | Sun, Jun 14 |\n"
        "\n"
        "### Task 2 — Format repo\n"
        "\n"
        "| Sub-goal | Deadline |\n"
        "|----------|----------|\n"
        "| Sub one | Mon, Jun 15 |\n"
    )
    tables = parse_tables(text)
    assert [t.heading for t in tables] == [
        "Non-Application To-Do",
        "Task 2 — Format repo",
    ]


def test_parse_tables_table_before_any_heading_has_none_heading():
    """A table with no heading above it carries heading=None, not a crash.

    Why this matters: the heading is optional context — a document that opens with a
    table (no section above it) must still parse, leaving the milestone label empty.
    """
    text = "| A | B |\n|---|---|\n| 1 | 2 |\n"
    assert parse_tables(text)[0].heading is None

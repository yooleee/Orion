# =============================================================================
# tests/test_merge.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying merge_sections assembles one body correctly and
#                  skips empty sections.
# Role in project: Merge is what turns several signals into one report. If it
#                  emits bare headers or drops content, the delivered message is
#                  wrong. These tests pin the exact output shape.
# =============================================================================

from orion.merge import merge_sections


def test_two_sections_merged_with_headers():
    """Two non-empty sections render as ## headers separated by a blank line.

    Why this matters: this is the common multi-signal case (e.g. git + tasks);
    the supervisor should see one body with clearly labeled sections.
    """
    result = merge_sections(
        [
            ("Code activity", "Refactored the state store."),
            ("Completed tasks", "- Wire structured lane\n- Add intake"),
        ]
    )
    assert result == (
        "## Code activity\n"
        "Refactored the state store.\n"
        "\n"
        "## Completed tasks\n"
        "- Wire structured lane\n- Add intake"
    )


def test_empty_section_is_skipped():
    """A section with an empty/whitespace body is omitted (no bare header).

    Why this matters: a collector may run but contribute nothing after upstream
    processing; emitting "## Notes" with no body would look broken.
    """
    result = merge_sections(
        [
            ("Code activity", "Did the thing."),
            ("Notes", "   "),
        ]
    )
    assert result == "## Code activity\nDid the thing."


def test_all_empty_returns_empty_string():
    """When every section is empty, the merged body is "".

    Why this matters: the orchestrator treats "" as "nothing to report" (same as
    a collector with no activity), so this must collapse to empty, not to a string
    of blank headers.
    """
    assert merge_sections([("A", ""), ("B", "  \n ")]) == ""
    assert merge_sections([]) == ""


def test_single_section_keeps_its_header():
    """A lone section still gets its ## header (consistent, predictable output).

    Why this matters: we deliberately do NOT special-case the single-section run;
    a labeled section reads the same whether one or three signals fired.
    """
    assert merge_sections([("Code activity", "Just git today.")]) == (
        "## Code activity\nJust git today."
    )


def test_order_is_preserved():
    """Sections appear in the order given (the orchestrator passes config order).

    Why this matters: section order is a presentation decision made by the caller
    (collector order in config); merge must not reorder it.
    """
    result = merge_sections(
        [("First", "1"), ("Second", "2"), ("Third", "3")]
    )
    assert result.index("## First") < result.index("## Second") < result.index("## Third")

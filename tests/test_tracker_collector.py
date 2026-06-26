# =============================================================================
# tests/test_tracker_collector.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the tracker collector — its status->done/open mapping,
#                  its title/status display text, the Non-Application table rows, and
#                  the collect() "newly done since last report" delta + marker.
# Role in project: The tracker is the first status-aware checklist signal. If its
#                  mapping is wrong, the dashboard shows applications with the wrong
#                  state; if its delta is wrong, the report path (when used) misfires.
#                  Tests pin the exact semantics against the real to_do.md shapes.
# =============================================================================

import json

import pytest

from orion.collectors import LANE_STRUCTURED
from orion.collectors.tasks import ChecklistItem
from orion.collectors.tracker import TrackerError, collect, snapshot

# A fixture mirroring the real applications tracker's shape: numbered application
# sections each with a "- **Status:**" field (one carrying trailing free-text), a
# prose "## Non-Application To-Do" heading that must NOT become an application item,
# a main to-do table, and a sub-goal table. It exercises all four status values even
# though the live file currently uses only two.
_TRACKER = (
    "# Applications To-Do\n"
    "\n"
    "## 1. Claude Corps Fellow (job)\n"
    "- **Type:** Job (1-year fellowship)\n"
    "- **Link:** https://example.com/a\n"
    "- **Status:** Not started\n"
    "- **Notes:** Embed full-time for one year.\n"
    "\n"
    "## 2. Hack Your Summer (program)\n"
    "- **Status:** In progress (partial application already done) — low priority\n"
    "\n"
    "## 3. Some Course (course)\n"
    "- **Status:** Submitted\n"
    "\n"
    "## 4. Old Internship (internship)\n"
    "- **Status:** Closed\n"
    "\n"
    "## Non-Application To-Do\n"
    "\n"
    "| # | Task | Purpose / What it does | Deadline |\n"
    "|---|------|------------------------|----------|\n"
    "| 1 | Review GitHub repo | _(fill in)_ | Sun, Jun 14 |\n"
    "| 2 | Format repo — *see breakdown below* | _(fill in)_ | Tue, Jun 16 |\n"
    "\n"
    "### Task 2 breakdown\n"
    "\n"
    "| Sub-goal | Purpose / What it does | Deadline |\n"
    "|----------|------------------------|----------|\n"
    "| Format the barebones repo | _(fill in)_ | Sun, Jun 14 |\n"
    "| Switch the repo to public | _(fill in)_ | Tue, Jun 16 |\n"
)


def _write_tracker(tmp_path, text=_TRACKER):
    """Write a tracker file and return its path.

    Why: every test needs a throwaway to_do.md on disk; centralizing the write keeps
    each test focused on the content it cares about (DRY).
    """
    path = tmp_path / "to_do.md"
    path.write_text(text, encoding="utf-8")
    return path


# --- snapshot(): the live dashboard read --------------------------------------------


def test_snapshot_maps_status_to_done_open_with_status_in_text(tmp_path):
    """Each application becomes "<title> - <Status>", done only for Submitted/Closed.

    Why this matters: this is the core contract. Not started / In progress are open
    (and the status word is preserved in the text, since the checkbox alone can't tell
    them apart); Submitted / Closed are done. Trailing free-text after the status word
    is tolerated and dropped from the canonical status.
    """
    items = snapshot(_write_tracker(tmp_path))

    # Application items come first, in heading order, with status embedded.
    assert items[0] == ChecklistItem(text="Claude Corps Fellow (job) - Not started", done=False)
    assert items[1] == ChecklistItem(text="Hack Your Summer (program) - In progress", done=False)
    assert items[2] == ChecklistItem(text="Some Course (course) - Submitted", done=True)
    assert items[3] == ChecklistItem(text="Old Internship (internship) - Closed", done=True)


def test_snapshot_excludes_non_numbered_prose_heading(tmp_path):
    """The "## Non-Application To-Do" prose heading is not an application item.

    Why this matters: only NUMBERED "## N." sections are applications. A prose section
    heading must not leak in as a checklist item — only its table rows should.
    """
    texts = [item.text for item in snapshot(_write_tracker(tmp_path))]
    assert "Non-Application To-Do" not in texts


def test_snapshot_surfaces_table_rows_as_open_items(tmp_path):
    """Main-table "Task" rows and sub-goal-table "Sub-goal" rows surface as open items.

    Why this matters: the user chose to include the Non-Application table. Those rows
    have no status column, so every one is open (done=False), keyed off the Task /
    Sub-goal column.
    """
    items = snapshot(_write_tracker(tmp_path))
    table_items = [i for i in items if i.text in {
        "Review GitHub repo",
        "Format repo — *see breakdown below*",
        "Format the barebones repo",
        "Switch the repo to public",
    }]
    # All four table rows present, all open.
    assert len(table_items) == 4
    assert all(item.done is False for item in table_items)


def test_snapshot_missing_status_keeps_application_as_open_title_only(tmp_path):
    """A numbered application with no Status line is kept (open) with just its title.

    Why this matters: the contract is "never silently drop a numbered application."
    Absent status -> open, and only the title shows (we don't echo unknown free text).
    """
    text = "## 5. No Status App (job)\n- **Type:** Job\n"
    items = snapshot(_write_tracker(tmp_path, text))
    assert items == (ChecklistItem(text="No Status App (job)", done=False),)


def test_snapshot_unknown_status_value_keeps_application_as_open_title_only(tmp_path):
    """An unrecognized Status value is treated as open, title-only (no echo).

    Why this matters: a typo'd or novel status ("Pending review") must not crash or be
    pasted verbatim onto the family dashboard; the application stays visible as open.
    """
    text = "## 5. Weird App (job)\n- **Status:** Pending review\n"
    items = snapshot(_write_tracker(tmp_path, text))
    assert items == (ChecklistItem(text="Weird App (job)", done=False),)


def test_snapshot_dedupes_by_text_first_wins(tmp_path):
    """A title+status repeated across sources collapses to its first occurrence.

    Why this matters: identity is the item TEXT (KI-6), mirroring the tasks collector;
    a duplicate must render once.
    """
    text = (
        "## 1. Dup (job)\n- **Status:** Submitted\n"
        "## 2. Dup (job)\n- **Status:** Submitted\n"
    )
    items = snapshot(_write_tracker(tmp_path, text))
    assert items == (ChecklistItem(text="Dup (job) - Submitted", done=True),)


def test_snapshot_missing_file_returns_empty_not_error(tmp_path):
    """A missing tracker file yields () — it must never break a push.

    Why this matters: the tracker is additive context that rides on a checklist push;
    a misconfigured path must fail soft (like tasks.snapshot), not abort the push.
    """
    assert snapshot(tmp_path / "nope.md") == ()


# --- collect(): the retrospective "newly done" delta --------------------------------


def test_collect_first_run_reports_done_applications_only(tmp_path):
    """With no prior marker, only Submitted/Closed applications are the signal.

    Why this matters: collect() mirrors tasks.collect() — open work (Not started /
    In progress) and table rows (always open) are NOT progress; only applications that
    reached a done status count.
    """
    result = collect(_write_tracker(tmp_path), prior_marker=None)

    assert result.lane == LANE_STRUCTURED
    assert result.has_activity is True
    # Only the two done applications, in file order, as prose bullets.
    assert result.raw_text == (
        "- Some Course (course) - Submitted\n- Old Internship (internship) - Closed"
    )


def test_collect_reports_only_newly_done_since_marker(tmp_path):
    """A second run reports only applications done since the prior marker.

    Why this matters: the delta guarantee — an already-reported done application must
    not re-appear just because it's still in the file.
    """
    prior = json.dumps(["Some Course (course) - Submitted"])
    result = collect(_write_tracker(tmp_path), prior_marker=prior)

    assert result.has_activity is True
    assert result.raw_text == "- Old Internship (internship) - Closed"


def test_collect_marker_round_trips_full_done_set_sorted(tmp_path):
    """new_marker serializes the FULL current done set, sorted; round-trip = no activity.

    Why this matters: feeding new_marker back as prior_marker must yield nothing new,
    and sorting makes the marker order-independent.
    """
    first = collect(_write_tracker(tmp_path), prior_marker=None)
    assert json.loads(first.new_marker) == [
        "Old Internship (internship) - Closed",
        "Some Course (course) - Submitted",
    ]
    second = collect(_write_tracker(tmp_path), prior_marker=first.new_marker)
    assert second.has_activity is False
    assert second.raw_text == ""


def test_collect_table_rows_never_enter_the_done_delta(tmp_path):
    """Table to-do rows are open, so they never count as "newly done".

    Why this matters: tables have no status column; treating any table row as progress
    would be a false positive in the report path.
    """
    text = (
        "| # | Task | Deadline |\n"
        "|---|------|----------|\n"
        "| 1 | A table task | Sun, Jun 14 |\n"
    )
    result = collect(_write_tracker(tmp_path, text), prior_marker=None)
    assert result.has_activity is False
    assert result.raw_text == ""


def test_collect_missing_file_raises_tracker_error(tmp_path):
    """A nonexistent tracker file raises TrackerError with the path.

    Why this matters: at report time a misconfigured path should fail loudly with a
    fixable message (config intentionally does not check existence), mirroring tasks.
    """
    with pytest.raises(TrackerError, match="not found"):
        collect(tmp_path / "nope.md", prior_marker=None)

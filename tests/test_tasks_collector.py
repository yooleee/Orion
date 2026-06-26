# =============================================================================
# tests/test_tasks_collector.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the tasks collector's "newly completed since last
#                  report" delta logic and its marker round-trip.
# Role in project: This is the first structured-lane signal. If its delta math is
#                  wrong, a checklist either re-reports finished items forever or
#                  silently drops completions. Tests pin the exact semantics
#                  (including the documented identity-by-text limitations).
# =============================================================================

import json

import pytest

from orion.collectors import LANE_STRUCTURED
from orion.collectors.tasks import ChecklistItem, TasksError, collect, snapshot


def _write_tasks(tmp_path, text):
    """Write a checklist file and return its path.

    Why: every test needs a throwaway TODO.md on disk; this keeps each test to the
    one checklist it cares about instead of repeating file I/O (DRY).
    """
    path = tmp_path / "TODO.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_first_run_reports_all_checked(tmp_path):
    """With no prior marker, every currently-checked item is reported.

    Why this matters: a first report must surface the whole completed state, the
    same way the git collector reports full history on its first run.
    """
    path = _write_tasks(
        tmp_path,
        "# TODO\n"
        "- [x] Build collector contract\n"
        "- [ ] Slack delivery\n"
        "- [x] Add intake command\n",
    )
    result = collect(path, prior_marker=None)

    assert result.lane == LANE_STRUCTURED
    assert result.has_activity is True
    # File order is preserved in the body; the unchecked item is absent.
    assert result.raw_text == "- Build collector contract\n- Add intake command"
    assert "Slack delivery" not in result.raw_text


def test_incremental_reports_only_newly_checked(tmp_path):
    """A second run reports only items checked since the prior marker.

    Why this matters: this is the core delta guarantee — an already-reported
    completion must not appear again just because the file still contains it.
    """
    # Prior marker: "Build collector contract" was already reported complete.
    prior = json.dumps(["Build collector contract"])
    path = _write_tasks(
        tmp_path,
        "- [x] Build collector contract\n- [x] Add intake command\n",
    )
    result = collect(path, prior_marker=prior)

    assert result.has_activity is True
    assert result.raw_text == "- Add intake command"


def test_no_new_completions_means_no_activity(tmp_path):
    """When nothing new is checked, has_activity is False and body is empty.

    Why this matters: a run over an unchanged checklist must report nothing and
    (downstream) not advance state — otherwise every run would re-send.
    """
    prior = json.dumps(["Build collector contract"])
    path = _write_tasks(tmp_path, "- [x] Build collector contract\n- [ ] Later\n")
    result = collect(path, prior_marker=prior)

    assert result.has_activity is False
    assert result.raw_text == ""


def test_unchecking_an_item_is_not_reported(tmp_path):
    """An item moved from [x] back to [ ] is not reported as progress.

    Why this matters: un-completing is not progress. Set subtraction handles this
    naturally — the item drops out of the current set and never enters the delta.
    """
    prior = json.dumps(["Build collector contract"])
    # The previously-complete item is now unchecked.
    path = _write_tasks(tmp_path, "- [ ] Build collector contract\n")
    result = collect(path, prior_marker=prior)

    assert result.has_activity is False
    assert result.raw_text == ""


def test_duplicate_item_text_is_deduped(tmp_path):
    """Two identical completed lines are treated as one item.

    Why this matters: identity is the item TEXT (a documented limitation); a
    checklist with a repeated line should report once, not twice.
    """
    path = _write_tasks(
        tmp_path,
        "- [x] Ship it\n- [x] Ship it\n",
    )
    result = collect(path, prior_marker=None)

    assert result.raw_text == "- Ship it"
    # The marker also carries the deduped set (one entry).
    assert json.loads(result.new_marker) == ["Ship it"]


def test_marker_round_trips_full_completed_set(tmp_path):
    """The new_marker serializes the FULL current completed set, sorted.

    Why this matters: feeding the new_marker back in as prior_marker must yield no
    activity (everything currently checked is now "already reported"). Sorting
    makes the marker order-independent so re-ordering the file is not a "change".
    """
    path = _write_tasks(
        tmp_path,
        "- [x] Beta\n- [x] Alpha\n- [ ] Gamma\n",
    )
    first = collect(path, prior_marker=None)
    # Sorted regardless of file order.
    assert json.loads(first.new_marker) == ["Alpha", "Beta"]

    # Round-trip: using that marker as the prior produces no new activity.
    second = collect(path, prior_marker=first.new_marker)
    assert second.has_activity is False


def test_star_bullets_and_mixed_case_x(tmp_path):
    """`*` bullets and an uppercase [X] are recognized as completed items.

    Why this matters: GitHub-style checklists allow both bullet characters and
    don't care about the case of the x; the parser must match real-world files.
    """
    path = _write_tasks(tmp_path, "* [X] Star item\n")
    result = collect(path, prior_marker=None)
    assert result.raw_text == "- Star item"


def test_missing_file_raises_tasks_error(tmp_path):
    """A nonexistent tasks file raises TasksError with the path.

    Why this matters: a misconfigured/uncreated path should fail with a clear,
    fixable message at run time (config intentionally does not check existence).
    """
    missing = tmp_path / "nope.md"
    with pytest.raises(TasksError, match="not found"):
        collect(missing, prior_marker=None)


# --- snapshot(): the LIVE checklist read (E2 Inc 2) -------------------------------
# snapshot() is a SEPARATE read from collect(): it reports the FULL current checklist
# (open + done) for the dashboard's live view, not a delta. These tests pin that it
# captures both states in file order AND that it does not disturb collect()'s
# retrospective behavior (the kickoff's hard invariant).


def test_snapshot_captures_open_and_done_in_file_order(tmp_path):
    """snapshot() returns every item — open and done — preserving file order.

    Why this matters: the dashboard's "live checklist" must show what is done AND
    what is still open/planned. Unlike collect(), which deliberately drops "[ ]"
    items, snapshot() carries both with their done-state, in the order they appear so
    the rendered list reads like the user's file.
    """
    path = _write_tasks(
        tmp_path,
        "# TODO\n"
        "- [x] Build collector contract\n"
        "- [ ] Slack delivery\n"
        "- [X] Add intake command\n"
        "* [ ] Wire the dashboard\n",
    )
    items = snapshot(path)

    assert items == (
        ChecklistItem(text="Build collector contract", done=True),
        ChecklistItem(text="Slack delivery", done=False),
        ChecklistItem(text="Add intake command", done=True),  # uppercase [X] is done
        ChecklistItem(text="Wire the dashboard", done=False),  # `*` bullet recognized
    )


def test_snapshot_dedupes_by_text_first_wins(tmp_path):
    """A repeated item text collapses to its first occurrence (identity-by-text).

    Why this matters: snapshot() mirrors collect()'s documented identity model
    (KI-6) — an item is identified by its text — so two identical lines must not
    render twice. First occurrence (and its done-state) wins.
    """
    path = _write_tasks(
        tmp_path,
        "- [ ] Duplicate\n- [x] Duplicate\n",
    )
    items = snapshot(path)
    assert items == (ChecklistItem(text="Duplicate", done=False),)


def test_snapshot_ignores_non_checklist_lines(tmp_path):
    """Prose, headings, and plain bullets are not checklist items.

    Why this matters: a tasks file is real Markdown with prose around the boxes;
    only "- [ ]"/"- [x]" lines are checklist items, everything else is skipped.
    """
    path = _write_tasks(
        tmp_path,
        "# Heading\nSome prose.\n- A plain bullet\n- [ ] Real item\n",
    )
    assert snapshot(path) == (ChecklistItem(text="Real item", done=False),)


def test_snapshot_missing_file_returns_empty_not_error(tmp_path):
    """A missing/unreadable tasks file yields () — it must never break a report.

    Why this matters: the checklist is additive context that rides on a report. If
    it could raise (the way collect() does), a misconfigured path would abort the
    report the user actually asked for. snapshot() fails soft instead.
    """
    missing = tmp_path / "nope.md"
    assert snapshot(missing) == ()


def test_snapshot_does_not_disturb_collect_delta(tmp_path):
    """collect() and snapshot() read the same file independently and agree.

    Why this matters: this is the kickoff's hard invariant — adding the live
    checklist must NOT regress the retrospective "newly completed" path. Here both
    run on one file: collect() still reports only the done item as new, while
    snapshot() reports the full open+done list. They neither share nor mutate state.
    """
    path = _write_tasks(
        tmp_path,
        "- [x] Done thing\n- [ ] Open thing\n",
    )
    delta = collect(path, prior_marker=None)
    live = snapshot(path)

    # Retrospective path is unchanged: only the completed item, as prose.
    assert delta.raw_text == "- Done thing"
    assert json.loads(delta.new_marker) == ["Done thing"]
    # Live path: both items, with state.
    assert live == (
        ChecklistItem(text="Done thing", done=True),
        ChecklistItem(text="Open thing", done=False),
    )


# --- ChecklistItem.due_date: the additive field (E2 Inc 3, Unit 1) -------------------


def test_checklist_item_due_date_defaults_to_none():
    """A two-arg ChecklistItem still constructs, with due_date defaulting to None.

    Why this matters: due_date was added additively. Every existing construction site
    (and every existing test) passes only text+done, so the default must keep that valid
    and the wire shape unchanged when no deadline is present.
    """
    assert ChecklistItem(text="x", done=False).due_date is None


def test_checklist_item_carries_due_date_when_given():
    """When provided, due_date is carried verbatim on the item.

    Why this matters: the tracker collector sets due_date to a normalized ISO date; the
    record must hold it so it can ride the wire to the dashboard.
    """
    item = ChecklistItem(text="x", done=True, due_date="2026-07-17")
    assert item.due_date == "2026-07-17"


def test_tasks_snapshot_leaves_due_date_none(tmp_path):
    """The tasks collector (checkbox lists) never sets a deadline — due_date stays None.

    Why this matters: GitHub-style "[ ]/[x]" lists carry no deadline syntax, so the tasks
    collector must leave due_date None; deadlines come only from the tracker collector.
    """
    live = snapshot(_write_tasks(tmp_path, "- [ ] Open thing\n"))
    assert all(item.due_date is None for item in live)

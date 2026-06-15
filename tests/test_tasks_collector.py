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
from orion.collectors.tasks import TasksError, collect


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

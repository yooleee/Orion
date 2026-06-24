# =============================================================================
# tests/test_incubator_collector.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the incubator collector's table parsing and its
#                  "new idea / status transition since last report" delta logic,
#                  including the {idea -> status} marker round-trip.
# Role in project: This is Orion's fifth signal (D4) and the first whose source is
#                  a structured Markdown table. If its delta math or parsing is
#                  wrong, the idea pipeline either re-reports every idea forever or
#                  silently drops transitions. These tests pin the exact semantics
#                  (link-text identity, header-indexed columns, removals-are-silent)
#                  against the real index.md shape.
# =============================================================================

import json

import pytest

from orion.collectors import LANE_STRUCTURED
from orion.collectors.incubator import IncubatorError, collect

# A canonical table in the real index.md shape: an H1, prose, a "Status values"
# line, then a 5-column table whose Idea cells are Markdown links. Tests build
# variants of this so each states only the difference it cares about (DRY).
_CANONICAL = """\
# Incubator Index

A registry of every idea, for comparison and triage.

Status values: raw, refining, validated, parked, graduated.

| Idea | Status | One-line pitch | Relation to existing projects | Next step |
|------|--------|----------------|-------------------------------|-----------|
| [VLM Photo Overlay](ideas/vlm-photo-overlay.md) | refining | Annotate a photo with real places | Standalone CV | Manual projection |
| [Recipe Sorter](ideas/recipe-sorter.md) | raw | Sort recipes by effort | None | Validate demand |
"""


def _write_incubator(tmp_path, text):
    """Write an incubator index file and return its path.

    Why: every test needs a throwaway index.md on disk; this keeps each test to the
    one table it cares about instead of repeating file I/O (DRY).
    """
    path = tmp_path / "index.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_first_run_reports_all_ideas_as_new(tmp_path):
    """With no prior marker, every idea in the table is reported as new, with pitch.

    Why this matters: a first report must surface the whole pipeline (each idea + a
    one-line pitch for context), in file order, the same way the other collectors
    report their full current state on a first run.
    """
    path = _write_incubator(tmp_path, _CANONICAL)
    result = collect(path, prior_marker=None)

    assert result.lane == LANE_STRUCTURED
    assert result.has_activity is True
    # New ideas in file order, each followed by its indented pitch line.
    assert result.raw_text == (
        "- New idea: VLM Photo Overlay (refining)\n"
        "  Annotate a photo with real places\n"
        "- New idea: Recipe Sorter (raw)\n"
        "  Sort recipes by effort"
    )


def test_status_change_reported_as_transition(tmp_path):
    """An idea whose status moved is reported as an "old → new" transition.

    Why this matters: the core update a supervisor wants is movement — "this idea
    graduated" — not the idea re-announced. Only the changed idea should appear.
    """
    # Prior: both ideas were already known at their original statuses.
    prior = json.dumps({"VLM Photo Overlay": "refining", "Recipe Sorter": "raw"})
    # Same table, but VLM has advanced refining -> validated.
    moved = _CANONICAL.replace(
        "vlm-photo-overlay.md) | refining |", "vlm-photo-overlay.md) | validated |"
    )
    path = _write_incubator(tmp_path, moved)
    result = collect(path, prior_marker=prior)

    assert result.has_activity is True
    assert result.raw_text == "- VLM Photo Overlay: refining → validated"


def test_new_idea_added_to_known_pipeline(tmp_path):
    """A brand-new row is reported as new while unchanged ideas stay silent.

    Why this matters: adding an idea to an established pipeline should announce just
    that idea (with its pitch), not re-announce the ideas already reported.
    """
    # Prior: only VLM was known (Recipe Sorter is brand new this run).
    prior = json.dumps({"VLM Photo Overlay": "refining"})
    path = _write_incubator(tmp_path, _CANONICAL)
    result = collect(path, prior_marker=prior)

    assert result.has_activity is True
    assert result.raw_text == (
        "- New idea: Recipe Sorter (raw)\n  Sort recipes by effort"
    )
    assert "VLM Photo Overlay" not in result.raw_text


def test_no_change_means_no_activity(tmp_path):
    """An unchanged table reports nothing and produces an empty body.

    Why this matters: a run over an unchanged pipeline must report nothing and
    (downstream) not advance state — otherwise every run would re-send.
    """
    prior = json.dumps({"VLM Photo Overlay": "refining", "Recipe Sorter": "raw"})
    path = _write_incubator(tmp_path, _CANONICAL)
    result = collect(path, prior_marker=prior)

    assert result.has_activity is False
    assert result.raw_text == ""


def test_marker_round_trips_full_status_map(tmp_path):
    """new_marker serializes the FULL current {idea -> status} map, sorted by key.

    Why this matters: feeding a run's marker back as the prior for an unchanged file
    must yield zero activity — the round-trip property that makes the delta correct.
    """
    path = _write_incubator(tmp_path, _CANONICAL)
    first = collect(path, prior_marker=None)
    # The marker is the complete map (not just the delta), keyed by idea title.
    assert json.loads(first.new_marker) == {
        "VLM Photo Overlay": "refining",
        "Recipe Sorter": "raw",
    }

    second = collect(path, prior_marker=first.new_marker)
    assert second.has_activity is False  # full round-trip: nothing new or changed


def test_idea_identified_by_link_text_not_path(tmp_path):
    """A Markdown-link Idea cell is identified by its title, not the link target.

    Why this matters: ideas are written as "[Title](ideas/title.md)"; the idea's
    identity (and the marker key) must be the human title so the path can change
    without the idea looking new.
    """
    path = _write_incubator(tmp_path, _CANONICAL)
    marker = json.loads(collect(path, prior_marker=None).new_marker)
    assert "VLM Photo Overlay" in marker
    # The file path must NOT leak into the identity.
    assert not any("ideas/" in key for key in marker)


def test_reordered_columns_and_no_pitch_column(tmp_path):
    """Columns are located by header, so re-ordering works; a missing pitch is fine.

    Why this matters: the file is hand-maintained, so the parser must tolerate
    column re-ordering and a table without a pitch column — in which case a new idea
    is reported without a pitch line rather than failing.
    """
    table = (
        "| Status | Idea | Next step |\n"
        "|--------|------|-----------|\n"
        "| validated | [Recipe Sorter](ideas/recipe-sorter.md) | Build v1 |\n"
    )
    path = _write_incubator(tmp_path, table)
    result = collect(path, prior_marker=None)

    assert result.has_activity is True
    # No pitch column -> just the new-idea line, no indented pitch beneath it.
    assert result.raw_text == "- New idea: Recipe Sorter (validated)"


def test_removed_idea_is_not_reported(tmp_path):
    """Dropping an idea from the table is not an update and is reported silently.

    Why this matters: "we stopped tracking this" is rarely the update a supervisor
    wants; mirroring tasks.py (un-checking is not activity), a removal alone yields
    no report — but the marker drops the removed idea so it can't resurface.
    """
    prior = json.dumps({"VLM Photo Overlay": "refining", "Recipe Sorter": "raw"})
    # New table keeps only VLM, unchanged. Recipe Sorter is gone.
    table = (
        "| Idea | Status | One-line pitch |\n"
        "|------|--------|----------------|\n"
        "| [VLM Photo Overlay](ideas/vlm.md) | refining | Annotate a photo |\n"
    )
    path = _write_incubator(tmp_path, table)
    result = collect(path, prior_marker=prior)

    assert result.has_activity is False
    assert result.raw_text == ""
    # The marker reflects the current table only — the removed idea is gone.
    assert json.loads(result.new_marker) == {"VLM Photo Overlay": "refining"}


def test_missing_file_raises_incubator_error(tmp_path):
    """A missing incubator file raises IncubatorError (a clean, catchable failure).

    Why this matters: a misconfigured path must surface as a kind, fixable message
    the CLI catches, not a raw traceback — the same contract as tasks/notes.
    """
    missing = tmp_path / "does-not-exist.md"
    with pytest.raises(IncubatorError):
        collect(missing, prior_marker=None)


def test_no_table_is_empty_not_an_error(tmp_path):
    """A file with no idea table is a valid empty pipeline, not a failure.

    Why this matters: an incubator with no ideas yet (just a heading/prose) should
    report nothing and serialize an empty map — like an empty notes file is "no
    activity," not an error.
    """
    path = _write_incubator(tmp_path, "# Incubator Index\n\nNothing here yet.\n")
    result = collect(path, prior_marker=None)

    assert result.has_activity is False
    assert result.raw_text == ""
    assert json.loads(result.new_marker) == {}

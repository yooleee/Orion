# =============================================================================
# tests/test_notes_collector.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the notes collector's "changed since last report"
#                  (content-hash) delta logic.
# Role in project: Notes is the second structured signal. If its change detection
#                  is wrong, an unchanged note re-sends every run, or a real edit
#                  is missed. Tests pin the replace-model semantics (KI-7).
# =============================================================================

import pytest

from orion.collectors import LANE_STRUCTURED
from orion.collectors.notes import NotesError, collect


def _write_notes(tmp_path, text):
    """Write a notes file and return its path.

    Why: each test needs a throwaway note on disk; this keeps tests to the one
    note they care about instead of repeating file I/O (DRY).
    """
    path = tmp_path / "NOTES.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_first_run_with_content_reports_it(tmp_path):
    """With no prior marker, a non-empty note is reported verbatim.

    Why this matters: a hand-written update should go out on its first run, as the
    note body (structured lane, no LLM rewrite).
    """
    path = _write_notes(tmp_path, "Finished the structured lane today.")
    result = collect(path, prior_marker=None)

    assert result.lane == LANE_STRUCTURED
    assert result.has_activity is True
    assert result.raw_text == "Finished the structured lane today."


def test_unchanged_content_reports_nothing(tmp_path):
    """Re-running with the prior hash and unchanged content yields no activity.

    Why this matters: the replace model must not re-send an unchanged note every
    run — the hash match is what makes it a delta.
    """
    path = _write_notes(tmp_path, "Same note.")
    first = collect(path, prior_marker=None)
    second = collect(path, prior_marker=first.new_marker)

    assert second.has_activity is False
    assert second.raw_text == ""


def test_changed_content_reports_again(tmp_path):
    """Editing the note changes the hash and reports the new content.

    Why this matters: a genuine update must be detected and sent; this is the
    whole point of the collector.
    """
    path = _write_notes(tmp_path, "First version.")
    first = collect(path, prior_marker=None)

    path.write_text("Second version.", encoding="utf-8")
    second = collect(path, prior_marker=first.new_marker)

    assert second.has_activity is True
    assert second.raw_text == "Second version."
    assert second.new_marker != first.new_marker


def test_empty_file_reports_nothing(tmp_path):
    """An empty (or whitespace-only) notes file produces no activity.

    Why this matters: there is no point sending a blank note; an empty file must
    not trigger a report even on a first run.
    """
    path = _write_notes(tmp_path, "   \n  \n")
    result = collect(path, prior_marker=None)

    assert result.has_activity is False
    assert result.raw_text == ""


def test_trailing_whitespace_is_not_a_change(tmp_path):
    """Adding only trailing whitespace does not count as a changed note.

    Why this matters: the collector strips before hashing, so incidental editor
    whitespace doesn't spuriously re-send an otherwise-identical note.
    """
    path = _write_notes(tmp_path, "Stable content.")
    first = collect(path, prior_marker=None)

    path.write_text("Stable content.\n\n  ", encoding="utf-8")
    second = collect(path, prior_marker=first.new_marker)

    assert second.has_activity is False


def test_marker_is_a_stable_hash(tmp_path):
    """The same content always yields the same marker (deterministic hash).

    Why this matters: stability is what lets an unchanged note be recognized
    across runs; a non-deterministic marker would re-send every time.
    """
    path = _write_notes(tmp_path, "Deterministic.")
    a = collect(path, prior_marker=None)
    b = collect(path, prior_marker=None)
    assert a.new_marker == b.new_marker


def test_missing_file_raises_notes_error(tmp_path):
    """A nonexistent notes file raises NotesError with the path.

    Why this matters: a misconfigured/uncreated path should fail clearly at run
    time (config intentionally does not check existence).
    """
    missing = tmp_path / "nope.md"
    with pytest.raises(NotesError, match="not found"):
        collect(missing, prior_marker=None)

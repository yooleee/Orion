# =============================================================================
# collectors/notes.py
# -----------------------------------------------------------------------------
# Responsible for: The NOTES structured-lane collector. Reads a local
#                  hand-written "current note" file and reports it when it has
#                  changed since the last report.
# Role in project: A structured signal — it returns lane="structured", so the
#                  orchestrator passes the note straight through (NO LLM). A
#                  hand-written update is already audience-ready by definition.
# Replace model (see docs/known-issues.md KI-7): the notes file is treated as
#                  THE current note, not an append log. The marker is a hash of
#                  the content, so ANY change re-sends the WHOLE file. This suits
#                  "here is my latest update" usage; it is intentionally not an
#                  append-only log (which would need a byte-offset marker instead).
# Assumptions: the notes file is UTF-8 text. Its content is the report body
#                  verbatim (already redacted as a safety net downstream).
# =============================================================================

from __future__ import annotations

import hashlib
from pathlib import Path

from orion.collectors import LANE_STRUCTURED, CollectorResult


class NotesError(Exception):
    """Raised when the configured notes file cannot be read.

    Why:
        A dedicated exception lets the CLI catch a missing/unreadable notes file
        specifically and print a clear, fixable message instead of a traceback,
        mirroring TasksError/GitError/ConfigError.
    """


def collect(notes_file: Path, prior_marker: str | None) -> CollectorResult:
    """Collect the manual note when it has changed since the last report.

    Args:
        notes_file: Path to the notes file (resolved absolute by config).
        prior_marker: The previous marker — a content hash of the last-reported
            note — or None on a first run.

    Returns:
        A CollectorResult on the STRUCTURED lane. raw_text is the note content
        verbatim (report-ready, hence no LLM). has_activity is False (and raw_text
        empty) when the file is empty or unchanged since prior_marker. new_marker
        is the hash of the current content.

    Why:
        Hashing the content gives a cheap, fixed-size "did this change?" marker
        without storing the note text in the state DB. An empty file reports
        nothing (no point sending a blank note); an unchanged file reports nothing
        (the hash matches), which is what keeps each run a true delta.
    """
    # Strip so that incidental trailing whitespace/newline edits don't count as a
    # change. The stripped text is both what we hash and what we send.
    content = _read(notes_file).strip()

    # Hash the content for the marker. An empty note still gets a hash, but it is
    # never reported (see has_activity below), so that hash is simply unused.
    current_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    # Activity requires BOTH that there is something to send AND that it differs
    # from what was last reported. On a first run prior_marker is None, so any
    # non-empty content is new.
    has_activity = bool(content) and current_hash != prior_marker
    raw_text = content if has_activity else ""

    return CollectorResult(
        lane=LANE_STRUCTURED,
        raw_text=raw_text,
        new_marker=current_hash,
        has_activity=has_activity,
    )


def _read(notes_file: Path) -> str:
    """Read the notes file as UTF-8, raising NotesError on any failure.

    Args:
        notes_file: Path to the notes file.

    Returns:
        The file's full text.

    Why:
        One clear error for the common "file not created yet" case and a
        consistent failure type the CLI can catch — the same pattern as the tasks
        collector's reader, kept separate rather than shared because the two
        collectors are independent units (a shared helper would couple them for
        almost no savings).
    """
    try:
        return notes_file.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise NotesError(
            f"Notes file not found: {notes_file}. Create it or fix `notes_file` "
            f"in orion.toml."
        ) from exc
    except OSError as exc:
        raise NotesError(f"Could not read notes file {notes_file}: {exc}") from exc

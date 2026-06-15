# =============================================================================
# collectors/tasks.py
# -----------------------------------------------------------------------------
# Responsible for: The TASKS structured-lane collector. Reads a local Markdown
#                  checklist and reports items that were newly checked off since
#                  the last report.
# Role in project: A structured signal — it returns lane="structured", so the
#                  orchestrator passes its text straight through (NO LLM). Only
#                  raw git activity is ever summarized by Claude.
# How "newly completed" works: the collector's marker is the full set of
#                  currently-completed item texts, serialized as a JSON list. The
#                  delta is (current completed set − stored set), so re-running
#                  with no new checkmarks reports nothing. The marker is opaque to
#                  the state store (state.py never interprets it).
# Identity model (documented limitations, see docs/known-issues.md KI-6):
#                  an item is identified by its TEXT, not its line position. So
#                  re-ordering lines is safe, but RENAMING a completed item makes
#                  the old text "disappear" and the new text count as newly done,
#                  and two identical lines are treated as one (deduped).
# Assumptions: the checklist is a UTF-8 text file using GitHub-style task list
#                  syntax: a line like "- [x] text" (or "* [X] text").
# =============================================================================

from __future__ import annotations

import json
import re
from pathlib import Path

from orion.collectors import LANE_STRUCTURED, CollectorResult


class TasksError(Exception):
    """Raised when the configured tasks file cannot be read.

    Why:
        A dedicated exception lets the CLI catch a missing/unreadable tasks file
        specifically and print a clear, fixable message (the path to create or
        fix) instead of dumping a traceback. A misconfigured path is a user/setup
        error, so it deserves a kind message, mirroring ConfigError/GitError.
    """


# A completed checklist line: optional indent, a "-" or "*" bullet, a "[x]" box
# (case-insensitive), then the item text. We capture only the text (group 1).
# Unchecked items ("[ ]") deliberately do NOT match — only completions are signal.
_COMPLETED_RE = re.compile(r"^\s*[-*]\s+\[[xX]\]\s+(.+?)\s*$")


def collect(tasks_file: Path, prior_marker: str | None) -> CollectorResult:
    """Collect newly completed checklist items since the last report.

    Args:
        tasks_file: Path to the Markdown checklist (resolved absolute by config).
        prior_marker: The previous marker — a JSON list of the item texts that
            were already complete at the last report — or None on a first run.

    Returns:
        A CollectorResult on the STRUCTURED lane. raw_text is a Markdown bullet
        list of the newly-completed items (already report-ready, hence structured
        and no LLM). has_activity is False (and raw_text empty) when nothing new
        was checked off. new_marker is the full CURRENT completed set serialized,
        so the next run measures its delta against the complete picture.

    Why:
        Returning only what is NEW keeps each report a true delta and stops a
        long-standing checklist from re-reporting the same finished items every
        run. We serialize the full current set (not just the delta) as the marker
        so an item completed two runs ago is never re-reported even if the file is
        edited in between.
    """
    text = _read(tasks_file)

    # Collect completed item texts in FILE ORDER, de-duplicated. File order is
    # used for the human-facing bullet list (reads like the checklist); the set
    # is used for delta math. `seen` doubles as the current completed set.
    completed_in_order: list[str] = []
    current: set[str] = set()
    for line in text.splitlines():
        match = _COMPLETED_RE.match(line)
        if match is None:
            continue
        item = match.group(1).strip()
        if item and item not in current:
            current.add(item)
            completed_in_order.append(item)

    # The stored set of already-reported completions. An empty/None marker means
    # "first run": everything currently checked is new.
    stored: set[str] = set(json.loads(prior_marker)) if prior_marker else set()

    # The delta: items checked now that were not at the last report. Kept in file
    # order for a readable report.
    newly_completed = [item for item in completed_in_order if item not in stored]

    has_activity = bool(newly_completed)
    raw_text = _format_items(newly_completed) if has_activity else ""

    # Marker is the full current set, sorted for a stable, order-independent
    # serialization (so an unrelated re-ordering of the file never looks like a change).
    new_marker = json.dumps(sorted(current))

    return CollectorResult(
        lane=LANE_STRUCTURED,
        raw_text=raw_text,
        new_marker=new_marker,
        has_activity=has_activity,
    )


def _read(tasks_file: Path) -> str:
    """Read the tasks file as UTF-8, raising TasksError on any failure.

    Args:
        tasks_file: Path to the checklist file.

    Returns:
        The file's full text.

    Why:
        Centralizing the read means one clear error message for the common case
        (the file does not exist yet) and a consistent failure type the CLI can
        catch. We do not cap the read: a checklist is small by nature, and only
        lines matching the task syntax are ever processed.
    """
    try:
        return tasks_file.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise TasksError(
            f"Tasks file not found: {tasks_file}. Create it or fix `tasks_file` "
            f"in orion.toml."
        ) from exc
    except OSError as exc:
        # Permission denied, is-a-directory, decode errors, etc. — surface the
        # path and the underlying reason rather than a raw traceback.
        raise TasksError(f"Could not read tasks file {tasks_file}: {exc}") from exc


def _format_items(items: list[str]) -> str:
    """Render newly-completed item texts as a Markdown bullet list.

    Args:
        items: The newly-completed item texts, in file order.

    Returns:
        A Markdown bullet list (one "- item" per line).

    Why:
        The structured lane is "already report-ready," so the collector produces
        the final section body itself. The merge step adds the "## Completed
        tasks" header, so this returns just the bullets — no heading — to keep the
        two responsibilities cleanly separated.
    """
    return "\n".join(f"- {item}" for item in items)

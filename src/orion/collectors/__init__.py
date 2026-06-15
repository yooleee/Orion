# =============================================================================
# collectors/__init__.py
# -----------------------------------------------------------------------------
# Responsible for: Defining the shared CollectorResult contract that EVERY
#                  collector returns.
# Role in project: This is the seam that keeps the pipeline open to new signals.
#                  Phase 1 has only the git (raw-lane) collector; Phase 2's
#                  task/notes collectors return this same type with lane=
#                  "structured", and the orchestrator handles them identically
#                  downstream. Defining the type here (not inside git.py) makes it
#                  the obvious shared home without inventing a base.py.
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass

# The two lanes from the design. "raw" activity needs redaction + LLM
# summarization; "structured" activity is already report-ready and skips the LLM.
# Phase 1 only ever produces "raw"; the constant exists so the seam is explicit.
LANE_RAW = "raw"
LANE_STRUCTURED = "structured"


@dataclass(frozen=True)
class CollectorResult:
    """What a collector found since the last report.

    Args:
        lane: LANE_RAW or LANE_STRUCTURED — decides whether the orchestrator
            sends this through the LLM (raw) or passes it through (structured).
        raw_text: The collected text (for git: commit messages + diffstat +
            optionally a capped diff). Empty when there is no activity.
        new_marker: The marker to advance state to if this report is sent (for
            git: the current HEAD sha). Empty when there is nothing to advance to.
        has_activity: True if there is something new to report. When False, the
            orchestrator prints "no new activity," sends nothing, and does NOT
            advance state.

    Why:
        A single, frozen result type means the orchestrator never has to know
        which collector produced the data — it reads the same four fields every
        time. That uniformity is exactly what lets a new collector slot in later
        without touching the orchestrator's core logic.
    """

    lane: str
    raw_text: str
    new_marker: str
    has_activity: bool

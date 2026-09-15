# =============================================================================
# cli/_status.py
# -----------------------------------------------------------------------------
# Responsible for: The per-project outcome labels the report and push sweeps print and count.
# Role in project: Shared vocabulary between report.py (--all summaries) and push.py (checklist
#                  sweeps); kept in one tiny module so neither imports the other for a string.
# Assumptions: Labels are stable strings (a scheduler may grep a log for them).
# =============================================================================
from __future__ import annotations


# Per-project run outcomes. cmd_report maps these to an exit code; `report --all`
# (CP3) tallies them into a summary. Plain string constants mirroring the
# LANE_RAW / LANE_STRUCTURED idiom in collectors — explicit, greppable, and
# directly printable. Deliberately NOT an Enum: the set is tiny and only ever
# compared and counted, so strings keep the call sites readable with no import.
STATUS_SENT = "SENT"                          # delivered to >=1 recipient
STATUS_NO_ACTIVITY = "NO_ACTIVITY"            # nothing new since last report
STATUS_SKIPPED_NOT_OPTED = "SKIPPED_NOT_OPTED"  # --yes but auto_send not enabled
STATUS_ABORTED = "ABORTED"                     # human declined at the preview
STATUS_FAILED = "FAILED"                       # a real failure (alert-worthy)
STATUS_NOT_DUE = "NOT_DUE"                      # --due: reported within its cadence

# Per-project outcomes for `checklist-push --all` (E1.3). A separate small set from the
# report STATUS_* above because the checklist lane's categories differ: there is no
# LLM/preview gate (so no ABORTED / SKIPPED_NOT_OPTED), but there IS a content change-gate
# (NO_CHANGE) and a "project has no checklist to push" skip (NO_CHECKLIST). NOT_DUE and
# FAILED are shared in spirit; reused directly. Kept as strings for the same reason.
STATUS_PUSHED = "PUSHED"                        # checklist pushed to the relay
STATUS_NO_CHANGE = "NO_CHANGE"                  # --due: content identical to last push
STATUS_NO_CHECKLIST = "NO_CHECKLIST"            # project has no checklist/source to push

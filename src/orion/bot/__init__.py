# =============================================================================
# bot/__init__.py
# -----------------------------------------------------------------------------
# Responsible for: Marking `bot` as a package and re-exporting the pure-core
#                  symbols so callers can `from orion.bot import decide_forward`
#                  without reaching into a submodule.
# Role in project: The native-bot slice (Horizon C, C2's two-way-in-chat add-on).
#                  PARKED (KI-28 Stage 2): the relay's comment write retired, so the
#                  bot has no honest write target until per-user keys land (the next
#                  slice repoints it at the discussion write). The pure decision core
#                  and the Slack shell are kept as the seam to repoint.
#                  The package is split deliberately:
#                    - core.py        — pure decision logic (no I/O, no slack-bolt)
#                    - slack_bot.py    — the always-on Bolt shell (the ONLY file that
#                                        imports slack-bolt, lazily); delivery parked
# Assumptions: This __init__ imports ONLY the pure core. It must NOT import
#              slack_bot (which imports the optional slack-bolt dependency), so
#              that `import orion.bot` / `import orion.bot.core` works on a stock
#              install with no extra installed. The CLI lazy-imports slack_bot only
#              when `orion bot` actually runs.
# =============================================================================

from __future__ import annotations

from orion.bot.core import ForwardDecision, IncomingMessage, decide_forward

__all__ = ["ForwardDecision", "IncomingMessage", "decide_forward"]

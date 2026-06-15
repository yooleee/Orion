# =============================================================================
# delivery/__init__.py
# -----------------------------------------------------------------------------
# Responsible for: Marking `delivery` as a package and defining the shared
#                  DeliveryError used by every channel.
# Role in project: Like collectors/, this directory holds one module per channel
#                  (discord.py now; slack.py in Phase 3). Each exposes the same
#                  send(message, webhook_url) -> None shape and raises this shared
#                  error on failure, so the orchestrator handles all channels the
#                  same way.
# =============================================================================

from __future__ import annotations


class DeliveryError(Exception):
    """Raised when sending a message to a channel fails.

    Why:
        A single error type across channels lets the CLI report a failed
        recipient uniformly and decide whether to advance state, without knowing
        which channel was involved.
    """

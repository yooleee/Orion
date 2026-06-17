# =============================================================================
# delivery/slack.py
# -----------------------------------------------------------------------------
# Responsible for: Sending a composed message to a Slack incoming webhook.
# Role in project: The Phase-3 delivery channel, a sibling of discord.py. Same
#                  send(message, webhook_url) -> None shape and shared
#                  DeliveryError, so the orchestrator routes to it identically.
# Why urllib (stdlib) and not requests / slack_sdk: a single JSON POST to an
#                  incoming webhook needs no third-party client — keeping Orion's
#                  runtime deps at two (anthropic, python-dotenv). A full Slack SDK
#                  would only earn its keep if we needed the Web API (bots, reads),
#                  which one-way reporting does not.
# Slack vs Discord differences handled here:
#   - Payload field is `text` (Slack), not `content` (Discord).
#   - Success is HTTP 200 with body "ok" (Discord returns 204).
#   - Slack's incoming webhooks are NOT behind the Cloudflare check that 403s the
#     default urllib User-Agent, so the UA is not strictly required — but we send a
#     descriptive one anyway, for parity and good citizenship.
# =============================================================================

from __future__ import annotations

import json
import urllib.error
import urllib.request

from orion import __version__
from orion.delivery import DeliveryError

# Sent for parity with the Discord sender. Slack does not require it, but a
# descriptive User-Agent identifies Orion in any webhook-side logs.
_USER_AGENT = f"Orion/{__version__} (progress-report webhook delivery)"


def send(payload: dict, webhook_url: str, *, timeout: float = 10.0) -> None:
    """POST a pre-built JSON payload to a Slack incoming webhook.

    Args:
        payload: The exact JSON body to send, built by compose (today {"text": …};
            later {"blocks": …, "text": fallback}). Delivery POSTs it as-is — it
            does not reshape or size it.
        webhook_url: The full Slack incoming-webhook URL (from .env via the
            recipient's webhook_env_var).
        timeout: Seconds to wait for the request before failing.

    Returns:
        None. Raises DeliveryError on any non-2xx response or network failure.

    Why:
        Delivery is pure transport: compose owns the payload's shape and sizing, so
        this function just serializes and POSTs. Slack returns 200 ("ok") on
        success; we translate every failure mode (HTTP error, connection error,
        timeout) into DeliveryError — the same uniform contract as discord.py, so
        the CLI handles a failed Slack recipient exactly like a failed Discord one.
    """
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": _USER_AGENT},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            # Any 2xx is success; Slack specifically returns 200 with body "ok".
            if not (200 <= status < 300):
                raise DeliveryError(f"Slack webhook returned HTTP {status}.")
    except urllib.error.HTTPError as exc:
        # 4xx/5xx responses arrive as HTTPError (a subclass of URLError). Slack
        # puts a short reason in the body (e.g. "invalid_payload", "no_service");
        # exc.reason carries enough to point the user at the problem.
        raise DeliveryError(
            f"Slack webhook returned HTTP {exc.code}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        # Connection refused, DNS failure, timeout, etc.
        raise DeliveryError(f"Could not reach Slack webhook: {exc.reason}") from exc

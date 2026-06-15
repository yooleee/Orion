# =============================================================================
# delivery/discord.py
# -----------------------------------------------------------------------------
# Responsible for: Sending a composed message to a Discord incoming webhook.
# Role in project: The Phase-1 delivery channel. One HTTPS POST, no bot, no
#                  gateway connection — the simplest outbound delivery, per the
#                  open-source-simplicity constraint.
# Why urllib (stdlib) and not requests: a single JSON POST needs no third-party
#                  HTTP client; using urllib keeps Orion's runtime deps at two.
# =============================================================================

from __future__ import annotations

import json
import urllib.error
import urllib.request

from orion import __version__
from orion.delivery import DeliveryError

# Discord rejects message `content` longer than 2000 characters. We truncate with
# a visible marker rather than letting the API 400 — the body is already redacted,
# so truncation is safe, just lossy. (Splitting into multiple messages is a
# possible future improvement; truncation is the simplest correct behavior now.)
_DISCORD_CONTENT_LIMIT = 2000
_TRUNCATION_MARKER = "\n… [truncated]"

# Discord's edge (Cloudflare) returns 403 Forbidden to requests sent with the
# default `Python-urllib/x.y` User-Agent, and the Discord API explicitly asks
# clients to send a descriptive User-Agent. Without this header, delivery fails
# for every real webhook even though the URL is valid — so it is required, not
# cosmetic. We identify Orion and its version.
_USER_AGENT = f"Orion/{__version__} (progress-report webhook delivery)"


def send(message: str, webhook_url: str, *, timeout: float = 10.0) -> None:
    """POST a message to a Discord incoming webhook.

    Args:
        message: The composed message text to send.
        webhook_url: The full Discord webhook URL (from .env via the recipient's
            webhook_env_var).
        timeout: Seconds to wait for the request before failing.

    Returns:
        None. Raises DeliveryError on any non-2xx response or network failure.

    Why:
        Discord's incoming-webhook API takes a JSON body with a `content` field
        and returns 204 No Content on success. We build the request with stdlib
        urllib, enforce Discord's length limit up front, and translate every
        failure mode (HTTP error, connection error, timeout) into DeliveryError
        so the caller can report the failed recipient and decide on state.
    """
    content = _truncate(message)
    # Discord expects a JSON body; "content" is the message text field.
    data = json.dumps({"content": content}).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": _USER_AGENT},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            # Any 2xx is success; Discord specifically returns 204.
            if not (200 <= status < 300):
                raise DeliveryError(f"Discord webhook returned HTTP {status}.")
    except urllib.error.HTTPError as exc:
        # 4xx/5xx responses arrive as HTTPError (a subclass of URLError).
        raise DeliveryError(
            f"Discord webhook returned HTTP {exc.code}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        # Connection refused, DNS failure, timeout, etc.
        raise DeliveryError(f"Could not reach Discord webhook: {exc.reason}") from exc


def _truncate(message: str) -> str:
    """Shorten a message to fit Discord's content limit, with a marker.

    Args:
        message: The full message text.

    Returns:
        The message unchanged if within the limit, otherwise truncated with a
        visible marker appended.

    Why:
        Keeping the truncation in one place (and reserving room for the marker)
        means the limit logic is testable and the caller never has to think about
        Discord's quirks.
    """
    if len(message) <= _DISCORD_CONTENT_LIMIT:
        return message
    keep = _DISCORD_CONTENT_LIMIT - len(_TRUNCATION_MARKER)
    return message[:keep] + _TRUNCATION_MARKER

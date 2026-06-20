# =============================================================================
# bot/relay_client.py
# -----------------------------------------------------------------------------
# Responsible for: Posting one supervisor comment to the relay's machine endpoint
#                  (POST /api/comments) over a single authenticated HTTPS request.
# Role in project: The bot's outbound seam to the relay — the inbound counterpart
#                  of delivery/relay.py's push()/pull_comments(). When the pure core
#                  (core.py) decides to forward a chat reply, the Bolt shell calls
#                  this to land it in the relay's report_comments store (the same
#                  store the dashboard and `orion comments` read). It is a sync
#                  stdlib function so it stays unit-testable with the project's
#                  standard monkeypatch-urlopen pattern; the async shell offloads it
#                  to a thread so a slow POST never blocks the event loop.
# Why urllib (stdlib) and not requests/aiohttp: one JSON POST needs no third-party
#                  HTTP client — keeping the bot from adding a SECOND dependency
#                  beyond slack-bolt, holding the open-source-simplicity line. This
#                  mirrors delivery/relay.py exactly.
# =============================================================================

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from orion import __version__
from orion.delivery import DeliveryError

# A descriptive, non-default User-Agent — same reasoning as delivery/relay.py: some
# ingress proxies (and Cloudflare, a likely relay front) reject the default
# `Python-urllib/x.y` UA, so we identify Orion and its version.
_USER_AGENT = f"Orion/{__version__} (chat-bot comment relay)"


def post_comment(
    relay_url: str,
    token: str,
    project: str,
    author: str,
    body: str,
    *,
    timeout: float = 10.0,
) -> None:
    """POST one supervisor comment to the relay's machine comment endpoint.

    Args:
        relay_url: The configured relay URL — the SAME value delivery/relay.py uses
            (the [relay] table's `url`, which points at the ingest endpoint, e.g.
            ".../ingest"). The comment URL is DERIVED from it (see Why), so the caller
            passes one configured URL, not two.
        token: The Bearer token authenticating this write (read from .env via the
            relay's `token_env_var`) — the same shared secret push/pull use. Sent as
            `Authorization: Bearer <token>`.
        project: The project whose latest report the comment attaches to. The relay
            resolves "latest" itself (this client does not send a report_id in the
            smallest slice).
        author: The supervisor's display label, or "" when unknown. Already capped by
            the pure core; the relay re-caps as a safety net.
        body: The comment text. Already stripped and length-checked by the core.
        timeout: Seconds to wait for the request before failing.

    Returns:
        None. Raises DeliveryError on any non-2xx response or network failure.

    Why:
        Mirrors delivery/relay.py's push() as closely as possible (stdlib urllib, the
        same User-Agent, the same DeliveryError mapping) so both directions of the
        relay seam read alike and the caller handles a failure with one uniform,
        fail-soft path (a failed comment relay logs and is dropped, never crashing the
        bot). The comment URL is derived with urljoin against a ROOT-RELATIVE
        "/api/comments": urljoin(".../ingest", "/api/comments") -> ".../api/comments",
        which replaces the whole path and matches the relay's root-level route — so the
        single configured `url` serves push, pull, AND this write with no extra config.
    """
    # Derive the comment URL from the configured (ingest) URL. A root-relative path
    # makes urljoin replace the entire path, so ".../ingest" -> ".../api/comments".
    url = urllib.parse.urljoin(relay_url, "/api/comments")
    # The relay's POST /api/comments validates this exact shape: project + body
    # required, author optional. We omit report_id (the relay attaches to the latest).
    data = json.dumps({"project": project, "author": author, "body": body}).encode(
        "utf-8"
    )
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
            # Same Bearer scheme as push/pull: the relay checks it constant-time and
            # 401s a mismatch before storing anything.
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            # The endpoint returns 201 Created on success; accept any 2xx.
            if not (200 <= status < 300):
                raise DeliveryError(f"Relay comment POST returned HTTP {status}.")
    except urllib.error.HTTPError as exc:
        # 401 = token mismatch; 400 = bad payload; 404 = no report to attach to yet.
        # All surface as a reported, non-fatal failure the caller logs.
        raise DeliveryError(
            f"Relay comment POST returned HTTP {exc.code}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        # Connection refused, DNS failure, timeout, etc. — e.g. the relay is down.
        raise DeliveryError(f"Could not reach relay: {exc.reason}") from exc

# =============================================================================
# delivery/relay.py
# -----------------------------------------------------------------------------
# Responsible for: Pushing the serialized portable report blob to a hosted relay's
#                  ingest endpoint over one authenticated HTTPS POST.
# Role in project: The C1 outbound seam. Unlike a chat channel (Discord/Slack),
#                  the relay does NOT receive a channel-rendered payload — it
#                  receives the raw serialized blob (serialize_blob's output) once
#                  per run, so the hosted dashboard can render its own presentation
#                  from the structured data. This is the single thing local Orion
#                  learns about hosting: "serialize the blob + a token -> POST to a
#                  URL," which keeps the hosting choice (Path A vs B) decoupled.
# Why urllib (stdlib) and not requests: same as the other senders — one JSON POST
#                  needs no third-party HTTP client, keeping Orion's runtime deps
#                  at two.
# =============================================================================

from __future__ import annotations

import urllib.error
import urllib.request

from orion import __version__
from orion.delivery import DeliveryError

# A descriptive, non-default User-Agent. The same reasoning as the chat senders:
# some ingress proxies (and Cloudflare, a likely future relay front) reject the
# default `Python-urllib/x.y` UA, so we identify Orion and its version. Harmless
# against a plain local server and keeps the request shape consistent across
# delivery modules.
_USER_AGENT = f"Orion/{__version__} (progress-report relay push)"


def push(blob_json: str, url: str, token: str, *, timeout: float = 10.0) -> None:
    """POST an already-serialized report blob to a relay's ingest endpoint.

    Args:
        blob_json: The serialized portable blob — exactly the string produced by
            report.serialize_blob. It is sent VERBATIM as the request body; it must
            NOT be re-serialized here (re-dumping would reorder keys and break the
            byte-stable contract the receiver validates).
        url: The relay's ingest URL (from the [relay] table's `url`).
        token: The Bearer token authenticating this push (read from .env via the
            relay's `token_env_var`). Sent as `Authorization: Bearer <token>`.
        timeout: Seconds to wait for the request before failing.

    Returns:
        None. Raises DeliveryError on any non-2xx response or network failure.

    Why:
        This is pure transport, like the chat senders: report.py owns what the blob
        looks like (and that it is twice-redacted), so this function only encodes
        and POSTs the contract bytes with the auth header. The relay's ingest
        returns 201 Created on success; we accept any 2xx and translate every
        failure mode (HTTP error, connection error, timeout) into DeliveryError.
        Reusing DeliveryError — rather than a relay-specific exception — lets the
        caller (cli._relay_push) treat a relay failure the same uniform way and
        apply the fail-soft policy: a relay error is reported but never fails the
        run or blocks state advancement. The decision that it is non-fatal lives in
        the caller; this function just raises like any other sender.
    """
    # Encode the already-serialized JSON string as-is. We deliberately do not call
    # json.dumps here: blob_json IS the wire contract, byte-for-byte.
    data = blob_json.encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
            # Bearer auth: the relay checks this against its configured token with a
            # constant-time compare (CP6) and 401s a mismatch.
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            # Any 2xx is success; the relay's ingest specifically returns 201.
            if not (200 <= status < 300):
                raise DeliveryError(f"Relay ingest returned HTTP {status}.")
    except urllib.error.HTTPError as exc:
        # 4xx/5xx responses arrive as HTTPError (a subclass of URLError). A 401 here
        # means a token mismatch; a 400 means the payload failed shape/version
        # validation — both surface as a reported, non-fatal relay failure.
        raise DeliveryError(
            f"Relay ingest returned HTTP {exc.code}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        # Connection refused, DNS failure, timeout, etc. — e.g. the relay server is
        # not running.
        raise DeliveryError(f"Could not reach relay: {exc.reason}") from exc

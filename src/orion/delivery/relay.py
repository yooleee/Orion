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

import json
import urllib.error
import urllib.parse
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


def push_checklist(
    relay_url: str,
    project: str,
    checklist: list,
    token: str,
    *,
    kind: str = "project",
    timeout: float = 10.0,
) -> None:
    """POST a project's current checklist to a relay's /checklist endpoint.

    Args:
        relay_url: The relay's configured URL (the [relay] table's `url`, e.g. an
            ".../ingest" URL); the /checklist endpoint is derived from it.
        project: The project the checklist belongs to (the upsert key on the relay).
        checklist: The current checklist as a list of {"text": str, "done": bool}
            dicts — ALREADY redacted by the caller (the privacy net runs before this).
        token: The Bearer token authenticating the push (the SAME ingest credential),
            sent as `Authorization: Bearer <token>`.
        kind: The project's kind ("project" | "tracker"), from config. E2 Inc 4: rides
            the checklist push so the relay can split the home into projects vs. trackers.
            The checklist push is the natural carrier — a tracker is checklist-only, so it
            always pushes here, while report-only projects default to "project" relay-side.
        timeout: Seconds to wait for the request before failing.

    Returns:
        None. Raises DeliveryError on any non-2xx response or network failure.

    Why:
        The transport for the dedicated checklist-only push — it sets the project's
        live checklist on the dashboard WITHOUT a report, enabling near-real-time
        updates as the local tasks_file is edited. Like push(), it is pure transport:
        the caller owns that the item texts are redacted; this only encodes the small
        {project, checklist} payload and POSTs it with the auth header. We derive the
        endpoint with urljoin (exactly as pull_comments derives /api/comments), so a
        relay URL configured as ".../ingest" still resolves to ".../checklist".
        DeliveryError unifies every failure mode so the CLI command / watch loop can
        report it and carry on.
    """
    endpoint = urllib.parse.urljoin(relay_url, "/checklist")
    data = json.dumps(
        {"project": project, "checklist": checklist, "kind": kind}
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            # The checklist endpoint returns 200 (an upsert); accept any 2xx.
            if not (200 <= status < 300):
                raise DeliveryError(f"Relay checklist push returned HTTP {status}.")
    except urllib.error.HTTPError as exc:
        raise DeliveryError(
            f"Relay checklist push returned HTTP {exc.code}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise DeliveryError(f"Could not reach relay: {exc.reason}") from exc


def push_disciplines(
    relay_url: str,
    project: str,
    disciplines: list,
    token: str,
    *,
    timeout: float = 10.0,
) -> None:
    """POST a project's observed disciplines to a relay's /disciplines endpoint.

    Args:
        relay_url: The relay's configured URL (the [relay] table's `url`, e.g. an
            ".../ingest" URL); the /disciplines endpoint is derived from it.
        project: The project the disciplines belong to (the upsert key on the relay).
        disciplines: The current disciplines as a list of
            {"title", "why", "scope", "source"} dicts — ALREADY redacted by the caller
            (the privacy net runs before this).
        token: The Bearer token authenticating the push (the SAME ingest credential),
            sent as `Authorization: Bearer <token>`.
        timeout: Seconds to wait for the request before failing.

    Returns:
        None. Raises DeliveryError on any non-2xx response or network failure.

    Why:
        The transport for the dedicated disciplines push (E2 Inc 4 slice 4b) — it sets
        the project's observed principles on the dashboard WITHOUT a report, exactly as
        push_checklist sets the live checklist. Disciplines are current full state
        (full-state upsert on the relay), so a dedicated push mirroring the checklist
        one is the natural carrier — not the append-only report blob. Like push(), it
        is pure transport: the caller owns that the texts are redacted; this only
        encodes the {project, disciplines} payload and POSTs it with the auth header.
        The endpoint is derived with urljoin (as push_checklist derives /checklist), so
        a relay URL configured as ".../ingest" still resolves to ".../disciplines".
    """
    endpoint = urllib.parse.urljoin(relay_url, "/disciplines")
    data = json.dumps(
        {"project": project, "disciplines": disciplines}
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            # The disciplines endpoint returns 200 (an upsert); accept any 2xx.
            if not (200 <= status < 300):
                raise DeliveryError(
                    f"Relay disciplines push returned HTTP {status}."
                )
    except urllib.error.HTTPError as exc:
        raise DeliveryError(
            f"Relay disciplines push returned HTTP {exc.code}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise DeliveryError(f"Could not reach relay: {exc.reason}") from exc


def push_skills(
    relay_url: str,
    project: str,
    skills: list,
    token: str,
    *,
    timeout: float = 10.0,
) -> None:
    """POST a project's observed skills to a relay's /skills endpoint.

    Args:
        relay_url: The relay's configured URL (the [relay] table's `url`, e.g. an
            ".../ingest" URL); the /skills endpoint is derived from it.
        project: The project the skills belong to (the upsert key on the relay).
        skills: The current skills as a list of
            {"name", "category", "evidence", "weight", "signals"} dicts — ALREADY
            redacted by the caller (the privacy net runs before this).
        token: The Bearer token authenticating the push (the SAME ingest credential),
            sent as `Authorization: Bearer <token>`.
        timeout: Seconds to wait for the request before failing.

    Returns:
        None. Raises DeliveryError on any non-2xx response or network failure.

    Why:
        The transport for the dedicated skills push (E2 Inc 4 slice 4c) — it sets the
        project's observed competencies on the dashboard WITHOUT a report, exactly as
        push_disciplines sets its principles. Skills are current full state (a
        full-state upsert on the relay), so a dedicated push mirroring the disciplines
        one is the natural carrier. Like push(), it is pure transport: the caller owns
        that the texts are redacted; this only encodes the {project, skills} payload and
        POSTs it with the auth header. The endpoint is derived with urljoin (as
        push_disciplines derives /disciplines), so a relay URL configured as ".../ingest"
        still resolves to ".../skills".
    """
    endpoint = urllib.parse.urljoin(relay_url, "/skills")
    data = json.dumps({"project": project, "skills": skills}).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            # The skills endpoint returns 200 (an upsert); accept any 2xx.
            if not (200 <= status < 300):
                raise DeliveryError(f"Relay skills push returned HTTP {status}.")
    except urllib.error.HTTPError as exc:
        raise DeliveryError(
            f"Relay skills push returned HTTP {exc.code}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise DeliveryError(f"Could not reach relay: {exc.reason}") from exc


def push_skills_batch(
    relay_url: str,
    slices: dict,
    token: str,
    *,
    allow_empty: bool = False,
    timeout: float = 10.0,
) -> None:
    """POST every project's skills in ONE atomic batch to a relay's /skills-batch endpoint.

    Args:
        relay_url: The relay's configured URL (the [relay] table's `url`); the
            /skills-batch endpoint is derived from it with urljoin.
        slices: {project_name: skills_list} — the FULL desired state for every synced
            project, each list the {name, category, evidence, weight, signals} card shape,
            ALREADY redacted by the caller (the privacy net runs before this).
        token: The Bearer token authenticating the push (the SAME ingest credential).
        allow_empty: When True, set the request's `allow_empty` flag so the relay accepts a
            batch that clears every project's skills (the relay otherwise refuses to wipe a
            populated comb to empty). Default False — the safe case.
        timeout: Seconds to wait for the request before failing.

    Returns:
        None. Raises DeliveryError on any non-2xx response (including the relay's 409
        empty-clobber refusal, whose message tells the caller to pass allow_empty) or a
        network failure.

    Why:
        The transport for the GLOBAL skills-sync (the two-pass rework). One request carries
        every project's slice so the relay can replace them in a single transaction, which
        is what makes the canonical re-naming atomic (no transient mix of old and new
        names). Pure transport, like push_skills: the caller owns redaction; this only
        encodes the {projects, allow_empty} body and POSTs it with the auth header.
    """
    endpoint = urllib.parse.urljoin(relay_url, "/skills-batch")
    data = json.dumps({"projects": slices, "allow_empty": allow_empty}).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            # The batch endpoint returns 200 (an atomic replace); accept any 2xx.
            if not (200 <= status < 300):
                raise DeliveryError(f"Relay skills-batch push returned HTTP {status}.")
    except urllib.error.HTTPError as exc:
        # Surface the relay's body message (e.g. the 409 empty-clobber refusal) so the
        # caller learns it must pass allow_empty, not just the bare status.
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:
            detail = ""
        raise DeliveryError(
            f"Relay skills-batch push returned HTTP {exc.code}: {exc.reason}"
            + (f" ({detail})" if detail else "")
        ) from exc
    except urllib.error.URLError as exc:
        raise DeliveryError(f"Could not reach relay: {exc.reason}") from exc


def pull_comments(
    relay_url: str,
    token: str,
    project: str,
    since_id: int,
    *,
    timeout: float = 10.0,
) -> dict:
    """GET a project's supervisor comments newer than since_id from the relay (C2).

    Args:
        relay_url: The configured relay URL — the SAME value push() uses (the
            [relay] table's `url`, which points at the ingest endpoint, e.g.
            ".../ingest"). The read URL is DERIVED from it (see Why), so the caller
            passes one configured URL, not two.
        token: The Bearer token authenticating this pull (read from .env via the
            relay's `token_env_var`) — the same shared secret the push sends. Sent as
            `Authorization: Bearer <token>`.
        project: The project whose comments to fetch. Sent as a query parameter.
        since_id: Return only comments with id strictly greater than this — the
            client's unread watermark. Pass 0 to fetch all of the project's comments.
        timeout: Seconds to wait for the request before failing.

    Returns:
        The parsed JSON response: {"comments": [ {id, report_id, author, body,
        created_at}, ... ], "latest_id": <int>}. `latest_id` is the highest comment
        id seen (or `since_id` when nothing is newer), which the caller advances its
        local watermark to. Raises DeliveryError on any non-2xx response, network
        failure, or unparseable body.

    Why:
        This is the inbound half of the relay seam, mirroring push() as closely as
        possible (stdlib urllib, the same User-Agent, the same DeliveryError mapping)
        so the two directions read alike and the caller handles a pull failure with
        the same fail-soft uniformity. The read URL is derived with urljoin against a
        ROOT-RELATIVE "/api/comments": urljoin(".../ingest", "/api/comments") ->
        ".../api/comments", which replaces the whole path and matches the relay's
        root-level API route — so a single configured `url` serves both push (its own
        path) and pull (the derived path) without a second config field. Unlike push,
        we PARSE the response: the body is the data the caller acts on, so a 200 with
        an unparseable body is itself a DeliveryError rather than a later crash.
    """
    # Derive the read URL from the configured (ingest) URL. A root-relative path makes
    # urljoin replace the entire path, so ".../ingest" -> ".../api/comments" regardless
    # of the configured path segment.
    base = urllib.parse.urljoin(relay_url, "/api/comments")
    # urlencode handles escaping a project name with spaces/special chars; since_id is
    # an int, str-encoded by urlencode.
    query = urllib.parse.urlencode({"project": project, "since_id": since_id})
    request = urllib.request.Request(
        f"{base}?{query}",
        headers={
            "User-Agent": _USER_AGENT,
            # Same Bearer scheme as push: the relay checks it constant-time and 401s
            # a mismatch (the /api/comments endpoint authenticates before querying).
            "Authorization": f"Bearer {token}",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            # urlopen only returns (no raise) for a 2xx, so any body here is the
            # success payload; 4xx/5xx arrive as HTTPError below.
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # 401 = token mismatch; 400 = bad project/since_id. Both surface as a reported
        # DeliveryError, like the push path.
        raise DeliveryError(
            f"Relay comments returned HTTP {exc.code}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        # Connection refused, DNS failure, timeout, etc. — e.g. the relay is down.
        raise DeliveryError(f"Could not reach relay: {exc.reason}") from exc

    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        # A 2xx with a body we can't parse means the relay and client disagree on the
        # contract — treat it as a transport failure, not a silent empty result.
        raise DeliveryError("Relay returned an unparseable comments response.") from exc


def pull_discussions(
    relay_url: str,
    token: str,
    project: str,
    since_id: int,
    *,
    timeout: float = 10.0,
) -> dict:
    """GET a project's discussion items newer than since_id from the relay (E2 Inc 5).

    Args:
        relay_url: The configured relay URL (the [relay] `url`, e.g. ".../ingest"); the
            read URL is DERIVED from it, so the caller passes one configured URL.
        token: The Bearer token authenticating this pull (the SAME ingest credential).
        project: The project whose discussion thread to fetch. Sent as a query parameter.
        since_id: Return only items with id strictly greater than this — the developer's
            unread watermark. Pass 0 to fetch the whole thread.
        timeout: Seconds to wait for the request before failing.

    Returns:
        The parsed JSON response: {"discussions": [ {id, project, author_id, author_name,
        role, body, created_at}, ... ], "latest_id": <int>}. `latest_id` is the highest
        item id seen (or `since_id` when nothing is newer). Raises DeliveryError on any
        non-2xx, network failure, or unparseable body.

    Why:
        The developer's read half of the supervisor-interaction loop — a near-verbatim
        twin of pull_comments against the /api/discussions route, so the two pulls read
        alike and fail-soft identically. The read URL is derived with urljoin against a
        root-relative "/api/discussions" exactly as pull_comments derives "/api/comments".
    """
    base = urllib.parse.urljoin(relay_url, "/api/discussions")
    query = urllib.parse.urlencode({"project": project, "since_id": since_id})
    request = urllib.request.Request(
        f"{base}?{query}",
        headers={
            "User-Agent": _USER_AGENT,
            "Authorization": f"Bearer {token}",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise DeliveryError(
            f"Relay discussions returned HTTP {exc.code}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise DeliveryError(f"Could not reach relay: {exc.reason}") from exc

    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise DeliveryError("Relay returned an unparseable discussions response.") from exc


def post_discussion(
    relay_url: str,
    token: str,
    project: str,
    body: str,
    author: str,
    *,
    timeout: float = 10.0,
) -> dict:
    """POST the developer's reply to a project's discussion thread (E2 Inc 5).

    Args:
        relay_url: The configured relay URL (the [relay] `url`); the write URL is derived.
        token: The Bearer token authenticating the write (the SAME ingest credential).
        project: The project whose thread to append to.
        body: The reply text. The relay validates non-empty + length-caps it.
        author: The developer's display name (the CLI's `--as`), or "" to let the relay
            stamp its default "developer" label. A free-text label — NOT a role: the relay
            always fixes role to "developer" on this Bearer path, so a name cannot escalate.
        timeout: Seconds to wait for the request before failing.

    Returns:
        The parsed JSON response {"id": <int>} — the new item's id. Raises DeliveryError
        on any non-2xx, network failure, or unparseable body.

    Why:
        The developer's write half of the loop — modelled on push_checklist's POST plumbing
        but, like pull_comments, it PARSES the response (the caller echoes the new id). The
        write URL is derived with urljoin against root-relative "/api/discussions" (the
        Bearer machine route, distinct from the cookie ".../items" route the SPA uses).
    """
    endpoint = urllib.parse.urljoin(relay_url, "/api/discussions")
    data = json.dumps({"project": project, "body": body, "author": author}).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # 401 token mismatch; 404 unknown project; 400 bad body — all reported.
        raise DeliveryError(
            f"Relay discussion reply returned HTTP {exc.code}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise DeliveryError(f"Could not reach relay: {exc.reason}") from exc

    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise DeliveryError("Relay returned an unparseable reply response.") from exc


# --- C3 admin API client: provisioning over HTTP (backs the relay-user CLI) -------
# These call the relay's admin endpoints with the SEPARATE admin token (never the
# ingest token). Unlike push/pull (fail-soft background calls), these back explicit
# `relay-user` commands, so a failure should surface clearly — we still raise
# DeliveryError, but the CLI treats it as a hard error (print + exit 1), and we lift the
# relay's own JSON {"error": ...} message into the exception so the operator sees the
# real reason (e.g. "a user named 'alice' already exists") rather than a bare status.


def _admin_error_message(exc: urllib.error.HTTPError) -> str:
    """Build a human-readable message from a failed admin-API response.

    Args:
        exc: The HTTPError raised for a non-2xx admin response.

    Returns:
        A message like "Relay returned HTTP 409: a user named 'alice' already exists",
        lifting the relay's JSON {"error": ...} detail when present, else just the status.

    Why:
        The admin endpoints answer errors as JSON {"error": "..."}; surfacing that detail
        (rather than a bare "HTTP 409") makes a CLI failure actionable. Reading the body
        can itself fail (already consumed, non-JSON) — we degrade to the status line
        rather than masking the original error with a parsing exception.
    """
    detail = ""
    try:
        body = json.loads(exc.read().decode("utf-8"))
        if isinstance(body, dict) and isinstance(body.get("error"), str):
            detail = f": {body['error']}"
    except (ValueError, UnicodeDecodeError, OSError):
        pass
    return f"Relay returned HTTP {exc.code}{detail}"


def _admin_request(
    method: str,
    url: str,
    admin_token: str,
    payload: dict | None,
    timeout: float,
) -> dict:
    """Make one authenticated admin-API request and return the parsed JSON response.

    Args:
        method: "GET" or "POST".
        url: The fully-resolved admin endpoint URL.
        admin_token: The admin Bearer token (independent of the ingest token).
        payload: The JSON request body for a POST, or None for a GET (no body).
        timeout: Seconds to wait before failing.

    Returns:
        The parsed JSON response dict.

    Raises:
        DeliveryError on any non-2xx response (with the relay's error detail), a network
        failure, or an unparseable body.

    Why:
        The three admin client calls share identical plumbing (Bearer admin auth, the
        User-Agent, the same error mapping), so it lives in one helper (DRY). It mirrors
        push/pull's urllib + DeliveryError shape so the whole relay client reads alike.
    """
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"User-Agent": _USER_AGENT, "Authorization": f"Bearer {admin_token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise DeliveryError(_admin_error_message(exc)) from exc
    except urllib.error.URLError as exc:
        raise DeliveryError(f"Could not reach relay: {exc.reason}") from exc
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise DeliveryError("Relay returned an unparseable response.") from exc


def create_user(
    relay_url: str,
    admin_token: str,
    name: str,
    role: str,
    projects: list[str],
    *,
    timeout: float = 10.0,
) -> dict:
    """Provision a relay user and return the response including the one-time key (C3).

    Args:
        relay_url: The configured relay URL (the [relay] `url`, e.g. ".../ingest"); the
            admin path is derived from it, same as pull_comments derives /api/comments.
        admin_token: The admin Bearer token (from .env via `admin_token_env_var`).
        name: The new user's unique handle.
        role: "viewer" or "admin".
        projects: The viewer's allowed project names (ignored server-side for an admin).
        timeout: Seconds to wait before failing.

    Returns:
        The parsed 201 response: {id, name, role, projects, key} — `key` is the raw login
        key, shown this once.

    Why:
        Mirrors push()'s transport but POSTs to the derived /api/users admin route. The
        raw key comes straight back to the caller (the CLI prints it once); it is never
        stored or logged here.
    """
    url = urllib.parse.urljoin(relay_url, "/api/users")
    return _admin_request(
        "POST",
        url,
        admin_token,
        {"name": name, "role": role, "projects": list(projects)},
        timeout,
    )


def list_users(relay_url: str, admin_token: str, *, timeout: float = 10.0) -> dict:
    """Fetch the relay's user roster (no credential material) for `relay-user list` (C3).

    Args:
        relay_url: The configured relay URL; the admin path is derived from it.
        admin_token: The admin Bearer token.
        timeout: Seconds to wait before failing.

    Returns:
        The parsed 200 response: {"users": [ {id, name, role, active, ...}, ... ]}. The
        relay omits key_verifier by construction, so no credential material is returned.

    Why:
        The read half of the admin client, mirroring create_user's transport with a GET.
    """
    url = urllib.parse.urljoin(relay_url, "/api/users")
    return _admin_request("GET", url, admin_token, None, timeout)


def revoke_user(
    relay_url: str, admin_token: str, name: str, *, timeout: float = 10.0
) -> dict:
    """Revoke a relay user (deactivate + force-logout) for `relay-user revoke` (C3).

    Args:
        relay_url: The configured relay URL; the admin path is derived from it.
        admin_token: The admin Bearer token.
        name: The user to revoke.
        timeout: Seconds to wait before failing.

    Returns:
        The parsed 200 response: {"name": <name>, "revoked": true}.

    Why:
        The third admin client call. The relay does the deactivate + session-version bump
        atomically, so a successful return means the user is force-logged-out everywhere.
    """
    url = urllib.parse.urljoin(relay_url, "/api/users/revoke")
    return _admin_request("POST", url, admin_token, {"name": name}, timeout)

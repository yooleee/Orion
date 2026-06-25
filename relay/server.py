# =============================================================================
# relay/server.py
# -----------------------------------------------------------------------------
# Responsible for: The relay's HTTP surface. In CP6 that is the single inbound
#                  endpoint: POST /ingest, which authenticates the pusher, validates
#                  the portable blob, and stores it. (CP7 adds the read-only GET
#                  dashboard routes to this same handler.)
# Role in project: This is Orion's FIRST inbound surface. Per the security
#                  must-holds, an inbound surface must authenticate (a shared Bearer
#                  token, constant-time compared) and validate (shape + version)
#                  before it touches storage. It speaks only the portable blob
#                  contract — the same JSON serialize_blob produces — so it stays
#                  decoupled from local Orion's internals.
# Why stdlib http.server: a tiny single-endpoint (soon: few-route) service needs no
#                  web framework. http.server's ThreadingHTTPServer gives concurrent
#                  request handling with zero dependencies, keeping the relay as
#                  light to run as the core — the open-source-simplicity bar.
# Concurrency note: ThreadingHTTPServer serves each request on its own thread, and a
#                  sqlite connection cannot be shared across threads, so the handler
#                  opens a fresh store connection per request (cheap for sqlite; the
#                  store's busy-timeout covers concurrent writes).
# =============================================================================

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import sqlite3
import sys
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from .render import (
    _DISPLAY_TZ,
    MAX_AUTHOR_CHARS,
    MAX_COMMENT_BODY_CHARS,
    PAGE_CSS_HASH,
    PAGE_JS_HASH,
    render_login,
    render_not_found,
    render_portfolio,
    render_project,
    render_report,
)
from .store import (
    add_comment,
    add_user,
    comments_for,
    comments_for_project,
    get,
    get_user_by_id,
    get_user_by_name,
    get_user_by_verifier,
    history,
    ingest,
    latest_report_per_project,
    list_users,
    open_relay_store,
    projects_for_user,
    record_admin_audit,
    revoke_user,
    update_last_login,
)

# Reject a Content-Length larger than this outright. A report blob is a few KB; 1 MB
# is far above any real payload but well below "read gigabytes into memory". Cheap
# defensive hygiene on an inbound surface — we do not trust a client-sent length.
_MAX_BLOB_BYTES = 1_000_000

# Per-field caps for a supervisor comment (C2) are defined in render.py (the
# dependency-free module) and imported above, so the form's maxlength hint and this
# server-side enforcement share ONE definition. The 1 MB raw-body cap above remains
# the outer memory guard; MAX_COMMENT_BODY_CHARS / MAX_AUTHOR_CHARS are the semantic
# limits on the DECODED form fields.

# The blob fields the relay consumes, each required to be a string. NOTE: the
# vestigial `source_marker` (always "", KI-8, unused by the store) is intentionally
# NOT required — coupling the wire check to a field slated for removal would be
# brittle. orion_version is here too, and additionally checked non-empty below: it
# is the contract's version handle, so it must be present, but we do NOT yet reject
# on a version *mismatch* (there is one version today; a compatibility policy is a
# deferred decision — the seam is simply that we capture the producing version).
_REQUIRED_STR_FIELDS = (
    "project",
    "share_level",
    "lane",
    "body",
    "generated_at",
    "orion_version",
)

# --- Multi-party auth: session-cookie + key-verifier crypto (Increment 1) ------
# Pure, module-level helpers so the cryptographic core is testable without a server.
# The session cookie is STATELESS and SIGNED: it carries only a tiny claims payload
# (format version, user id, issued/expiry times, the user's session_version) and an
# HMAC over it. It deliberately carries NO role/name/scope — those are re-read from
# the DB on every request, so a privilege never rides in a forgeable/stale cookie and
# revocation (active=0 / session_version bump) takes effect immediately. The signing
# key, the user-key pepper, and the admin token are all INDEPENDENT secrets (never
# derived from or shared with each other or the view/ingest tokens) per the Codex
# /second-opinion: sharing a secret widens blast radius and couples unrelated rotations.
_SESSION_COOKIE_NAME = "orion_session"
_SESSION_FORMAT_VERSION = 1  # bump to invalidate every outstanding cookie at once

# Roles an admin may PROVISION today. relay_users.role is an open enum
# (contributor/guest are reserved for later increments), but only these two are
# creatable now, so the provisioning endpoint allowlists them and 400s anything else —
# an unvalidated role string must never become a stored value.
_PROVISIONABLE_ROLES = ("admin", "viewer")

# Actor label written to the admin audit trail for token-driven provisioning. The admin
# API authenticates with a shared admin token (not a named identity), so the trail
# records the token role rather than a person — enough to attribute the action.
_ADMIN_ACTOR = "admin-token"

# --- Security response headers (hardening, defense-in-depth) --------------------
# The Content-Security-Policy sent on every HTML response. It is HASH-BASED: the two
# inline blocks the dashboard renders (_PAGE_CSS, _PAGE_JS) are allowlisted by their
# SHA-256, computed in render.py from the SAME constants the markup uses (so the policy
# and the page can never drift). That avoids 'unsafe-inline' while letting the page's
# own style/script run; everything else is locked down:
#   default-src 'self'      — same-origin only is the baseline for anything unlisted
#   style-src / script-src  — 'self' plus the one exact inline hash each
#   img-src 'self'          — the dashboard loads no external images
#   base-uri 'none'         — block a <base> tag that could rewrite relative URLs
#   form-action 'self'      — the login/comment forms may only POST back to us
#   frame-ancestors 'none'  — the dashboard may not be framed (clickjacking)
#   object-src 'none'       — no plugins/embeds
# The inline-hash allowances are simply inert on the _simple_html error pages (which
# carry no inline blocks), so this ONE policy safely covers every HTML response.
_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    f"style-src 'self' '{PAGE_CSS_HASH}'; "
    f"script-src 'self' '{PAGE_JS_HASH}'; "
    "img-src 'self'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'"
)


def _b64url_encode(raw: bytes) -> str:
    """URL-safe base64 WITHOUT padding (cookie-value safe: no '=', '/', '+').

    Why:
        Cookie values must avoid characters that need quoting (';', '=', space).
        URL-safe base64 already avoids '/'+'+'; stripping the '=' padding removes the
        last problematic char, and _b64url_decode re-adds it, so the round-trip is exact.
    """
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    """Inverse of _b64url_encode: re-pad then URL-safe base64 decode.

    Why:
        base64 needs the input length to be a multiple of 4; we stripped the padding
        on encode, so we restore it here. `validate=True` rejects stray characters so a
        tampered cookie segment fails to decode rather than decoding to garbage.
    """
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded)


def _sign(session_key: bytes, message: str) -> str:
    """Return the base64url HMAC-SHA256 of `message` under `session_key`.

    Why:
        One place computes the signature, so make_session_value and verify_session_value
        can never disagree on the algorithm. HMAC-SHA256 with a high-entropy key is the
        standard, stdlib-only way to make a stateless token unforgeable.
    """
    digest = hmac.new(session_key, message.encode("ascii"), hashlib.sha256).digest()
    return _b64url_encode(digest)


def make_session_value(
    session_key: bytes,
    user_id: int,
    session_version: int,
    issued_at: int,
    expires_at: int,
) -> str:
    """Build a signed session-cookie value `payload_b64.signature_b64`.

    Args:
        session_key: The independent HMAC signing key (bytes).
        user_id: The authenticated user's id (the cookie's `sub`); 0 is the reserved
            sentinel for the legacy bootstrap admin (no relay_users row).
        session_version: The user's current session_version at mint time; the request
            path rejects the cookie once this no longer matches the DB (revocation).
        issued_at: Unix seconds the token was minted.
        expires_at: Unix seconds the token expires (also enforced server-side).

    Returns:
        The cookie value string.

    Why:
        Claims are compact, sorted JSON so the signed bytes are deterministic. Only the
        five claims are included — never role/name/projects — so authority always comes
        from the live DB, not the cookie.
    """
    payload = {
        "v": _SESSION_FORMAT_VERSION,
        "sub": user_id,
        "iat": issued_at,
        "exp": expires_at,
        "sv": session_version,
    }
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    payload_b64 = _b64url_encode(payload_json.encode("utf-8"))
    return f"{payload_b64}.{_sign(session_key, payload_b64)}"


def verify_session_value(session_key: bytes, value: str, now: int) -> dict | None:
    """Verify a session-cookie value and return its claims, or None if invalid.

    Args:
        session_key: The HMAC signing key (bytes).
        value: The cookie value (`payload_b64.signature_b64`).
        now: Current Unix seconds, for the expiry check.

    Returns:
        The decoded claims dict when the signature verifies, the format version is
        known, and the token has not expired; otherwise None.

    Why:
        Order matters: verify the HMAC FIRST (constant-time, on the raw b64 segment)
        before decoding the payload, so a tampered/forged token is rejected without
        trusting any of its bytes. We reject an unknown `v` (format rotation) and an
        elapsed `exp` (server-side expiry, independent of the cookie's Max-Age, so a
        browser keeping the cookie past expiry still fails). This returns only the
        claims; checking `sv` against the DB and `active` is the request path's job.
    """
    try:
        payload_b64, signature = value.split(".", 1)
    except (ValueError, AttributeError):
        return None
    if not hmac.compare_digest(signature, _sign(session_key, payload_b64)):
        return None
    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != _SESSION_FORMAT_VERSION:
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < now:
        return None
    return payload


def key_verifier(user_pepper: bytes, raw_key: str) -> str:
    """Compute the stored verifier for a login key: HMAC-SHA256(pepper, key), hex.

    Args:
        user_pepper: The independent server-side pepper (bytes).
        raw_key: The user's raw login key.

    Returns:
        The hex verifier stored in relay_users.key_verifier.

    Why:
        Peppering (HMAC with a server secret) means a database leak ALONE cannot test
        candidate keys — an attacker also needs the pepper, which lives only in the
        relay's env, not the DB. A slow KDF (bcrypt/argon2) is unnecessary here because
        the keys are server-minted, ≥256-bit random (not human passwords), so there is
        nothing to brute-force; this was confirmed by the Codex /second-opinion.
    """
    return hmac.new(user_pepper, raw_key.encode("utf-8"), hashlib.sha256).hexdigest()


def mint_key() -> str:
    """Mint a fresh high-entropy login key (URL-safe, ~256 bits).

    Returns:
        A new random key string, shown to the operator ONCE at provisioning and never
        stored (only its verifier is).

    Why:
        secrets.token_urlsafe(32) draws 32 cryptographically-random bytes (~256 bits),
        far beyond brute-force, and the URL-safe alphabet makes it easy to copy/paste
        into a login form. Centralized so provisioning and any test mint the same shape.
    """
    return secrets.token_urlsafe(32)


def _validate_blob(payload: object) -> str | None:
    """Check a parsed payload against the portable blob contract.

    Args:
        payload: The JSON-parsed request body (any type — it is untrusted input).

    Returns:
        None when the payload is a valid blob; otherwise a short human-readable
        reason string (used as the 400 response message).

    Why:
        The ingest endpoint is an untrusted inbound surface, so it validates shape
        and types BEFORE storing — a malformed payload becomes a clear 400, not a
        later crash in the store or the renderer. We check each section is a
        [title, body] pair of strings specifically to protect CP7's renderer, which
        unpacks exactly that. Returning a reason string (rather than raising) keeps
        the handler's flow a simple linear sequence of guard checks.
    """
    if not isinstance(payload, dict):
        return "payload must be a JSON object"

    for key in _REQUIRED_STR_FIELDS:
        if not isinstance(payload.get(key), str):
            return f"field {key!r} must be a string"
    if not payload["orion_version"].strip():
        return "field 'orion_version' must be non-empty"

    participants = payload.get("participants")
    if not isinstance(participants, list) or not all(
        isinstance(p, str) for p in participants
    ):
        return "field 'participants' must be a list of strings"

    sections = payload.get("sections")
    if not isinstance(sections, list):
        return "field 'sections' must be a list"
    for section in sections:
        if not (
            isinstance(section, list)
            and len(section) == 2
            and all(isinstance(part, str) for part in section)
        ):
            return "each section must be a [title, body] pair of strings"

    return None


def _utc_now_iso() -> str:
    """Return the current time as an ISO-8601 UTC string (seconds precision).

    Returns:
        An ISO-8601 UTC timestamp like "2026-06-18T12:00:00+00:00".

    Why:
        The receiver stamps when it RECEIVED a push (ingested_at), distinct from the
        blob's own generated_at (when local Orion built it). Same format and
        precision local Orion uses, so timestamps across the two stores read
        consistently.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_comment_path(path: str) -> int | None:
    """Extract the report id from a "/report/<id>/comment" path, or None.

    Args:
        path: The request path, already stripped of any query string.

    Returns:
        The integer report id when `path` is exactly "/report/<digits>/comment";
        otherwise None (the route did not match).

    Why:
        do_POST routes by path, so it needs a single yes/no-with-id matcher for the
        comment route. We accept only an all-digit id segment (mirroring do_GET's
        id_str.isdigit() guard), so a non-numeric or malformed id simply fails to
        match and falls through to the 404 — no exception, no store touch.
    """
    prefix, suffix = "/report/", "/comment"
    if not (path.startswith(prefix) and path.endswith(suffix)):
        return None
    middle = path[len(prefix):-len(suffix)]
    return int(middle) if middle.isdigit() else None


def _simple_html(heading: str, detail: str) -> str:
    """Build a minimal self-contained HTML page for a browser-facing error.

    Args:
        heading: The <h1> / <title> text. MUST be a static, trusted literal.
        detail: A one-line explanation. Same static-only contract.

    Returns:
        A tiny complete HTML document string.

    Why:
        The comment POST is driven by a browser form, so its rejections (400/401/403)
        read better as HTML than the JSON /ingest returns. These pages carry NO
        dynamic input — every caller passes a fixed message — so they need no escaping.
        Keeping them here leaves render.py (the dashboard view layer) untouched.
    """
    return (
        "<!doctype html><html lang='en'><meta charset='utf-8'>"
        f"<title>Orion — {heading}</title>"
        f"<h1>{heading}</h1><p>{detail}</p></html>"
    )


class _RelayHandler(BaseHTTPRequestHandler):
    """Handles relay HTTP requests; reads its config from self.server (RelayServer).

    Why:
        BaseHTTPRequestHandler is instantiated per request by the server, so its
        configuration (the auth token and the db path) is carried on the server
        object and read here via self.server — the idiomatic stdlib way to inject
        per-server state without globals.
    """

    # Silence the default one-line-per-request stderr logging (noisy, and it
    # pollutes test output). We emit our own concise line on a successful ingest.
    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass

    def do_GET(self) -> None:
        """Route a GET to one of the read-only dashboard views.

        Routes: "/" (project index), "/project/<name>" (a project's history),
        "/report/<id>" (one report). An unknown report id or any other path is a
        404; an unknown PROJECT renders a friendly empty-state (200), since "no
        reports" and "never existed" look the same to a viewer.

        Read auth (multi-party): when the dashboard is access-gated — a view secret is
        set OR any user has been provisioned — every route requires a valid SESSION
        COOKIE; an absent/invalid/expired/revoked one redirects to /login (a persistent
        login, not the old per-session Basic dialog). On a bare loopback dev relay with
        neither, reads stay open. The fail-closed guard in create_server() still
        guarantees a view secret whenever the bind is non-loopback, so a world-reachable
        dashboard is never unauthenticated. Ingest stays Bearer-authed independently.

        /login and /logout are reachable WITHOUT a session (you must reach /login to
        authenticate). The "/api/..." namespace is Bearer-authed (a machine surface),
        routed FIRST — a browser never reaches it, the CLI never trips the session gate.

        AuthZ scoping layers on top of the gate: an admin (and the open/ungated relay)
        sees everything; a viewer is restricted to projects_for_user, and any project or
        report outside that scope returns 404 (existence-hiding, decision A), resolved
        from current DB state on every route so a regrant/revoke applies immediately.
        """
        # Strip any query string; routing is by path only (the /api/ handler re-reads
        # the query itself, since it — unlike the dashboard — takes parameters).
        path = urllib.parse.urlparse(self.path).path

        # Machine-JSON API routes are Bearer-authed and bypass the browser session gate.
        if path == "/api/comments":
            self._handle_api_comments()
            return
        # The admin API (provisioning) is authed with the SEPARATE admin token, not the
        # ingest token or a browser session — routed here so it never trips the cookie gate.
        if path == "/api/users":
            self._handle_list_users()
            return

        # The login surface is reachable WITHOUT a session — you must get here to log in.
        if path == "/login":
            self._send_html(200, render_login())
            return
        if path == "/logout":
            self._handle_logout()
            return

        # A fresh connection per request keeps each sqlite handle on its own thread.
        conn = open_relay_store(self.server.db_path)
        try:
            # Authentication gate (cookie session). Required whenever the dashboard is
            # access-gated; an absent/invalid/expired/revoked session redirects to
            # /login instead of the old Basic 401, so a logged-in browser STAYS logged
            # in across restarts (the point of the session).
            gated = self._auth_required(conn)
            principal = self._authenticate(conn) if gated else None
            if gated and principal is None:
                self._send_redirect(303, "/login")
                return

            # AuthZ scoping: which projects this principal may see. None = unrestricted
            # (admin / legacy admin / open relay); a set = a viewer's allowed projects.
            # Out-of-scope resources return 404 (NOT 403) so a viewer cannot even learn
            # that a project/report they aren't granted EXISTS — decision A (the audience
            # may include guests). The 404 is byte-identical to a genuinely-missing one.
            allowed = self._allowed_projects(conn, principal)

            if path == "/":
                # The portfolio home: each project plus its latest report (id + body),
                # so the cards can show a one-line headline and link straight in. Scope
                # filtering is unchanged — the same per-project `in allowed` predicate the
                # index used, applied before render, so a viewer sees only granted cards.
                projects = latest_report_per_project(conn)
                if allowed is not None:
                    projects = [p for p in projects if p["project"] in allowed]
                self._send_html(
                    200, render_portfolio(projects, self.server.display_tz)
                )
                return

            if path.startswith("/project/"):
                # Decode the percent-encoded name back to the stored project string.
                name = urllib.parse.unquote(path[len("/project/"):])
                # A viewer outside this project's scope is told it does not exist.
                if allowed is not None and name not in allowed:
                    self._send_html(404, render_not_found(f"No project {name!r}."))
                    return
                self._send_html(
                    200,
                    render_project(name, history(conn, name), self.server.display_tz),
                )
                return

            if path.startswith("/report/"):
                id_str = path[len("/report/"):]
                # Only a numeric id can match a row; anything else is a 404 without
                # touching the store.
                report = get(conn, int(id_str)) if id_str.isdigit() else None
                # Resolve the report's project FIRST, then scope-check it: an out-of-scope
                # report is reported as missing with the SAME message as a nonexistent
                # one, so existence stays hidden from a viewer who lacks the grant.
                if report is None or (
                    allowed is not None and report["project"] not in allowed
                ):
                    self._send_html(404, render_not_found(f"No report {id_str!r}."))
                    return
                # report is not None implies id_str was numeric, so int() is safe.
                comments = comments_for(conn, int(id_str))
                # Pass the authenticated identity (when logged in) so the comment form
                # shows "commenting as <name>" and drops the free-text name field — the
                # comment will be attributed to this identity, not a typed value.
                author_name = principal["name"] if principal is not None else None
                self._send_html(
                    200,
                    render_report(
                        report, comments, self.server.display_tz, author_name=author_name
                    ),
                )
                return

            self._send_html(404, render_not_found(f"Unknown path {path!r}."))
        finally:
            conn.close()

    def do_POST(self) -> None:
        """Route a POST to one of the relay's write surfaces.

        Routes: "/ingest" (the Bearer-authed report push), "/api/comments" (a
        Bearer-authed MACHINE comment write — the native bot's path, C2-bots), and
        "/report/<id>/comment" (a view-authed BROWSER comment, C2). Anything else is a
        404. Routing is by path only, so any query string is stripped first — mirroring
        do_GET.

        Why a router: do_POST used to be the single ingest handler. C2 added a second,
        very differently-authed write path (HTTP Basic + a CSRF check, not Bearer), and
        the bots slice adds a third — a machine sibling of GET /api/comments that takes
        Bearer (like /ingest), not Basic. Each path gets its own handler, keeping this
        method a short, readable table of routes rather than one branching blob.

        Note "/api/comments" is shared between do_GET (the pull-back read) and do_POST
        (the bot's write): the HTTP method already disambiguates them, so the same
        Bearer-gated machine path serves both directions — symmetric with how the
        browser uses /report/<id> (GET) and /report/<id>/comment (POST).
        """
        path = urllib.parse.urlparse(self.path).path

        if path == "/ingest":
            self._handle_ingest()
            return

        if path == "/api/comments":
            self._handle_api_comment_post()
            return

        # Admin API (admin-token authed, NOT the ingest token): provision + revoke users.
        if path == "/api/users":
            self._handle_create_user()
            return
        if path == "/api/users/revoke":
            self._handle_revoke_user()
            return

        if path == "/login":
            self._handle_login()
            return

        comment_report_id = _parse_comment_path(path)
        if comment_report_id is not None:
            self._handle_comment(comment_report_id)
            return

        self._send_json(404, {"error": "not found"})

    def _handle_ingest(self) -> None:
        """Authenticate, validate, and store a pushed report blob (POST /ingest).

        Why:
            This is the original ingest path, unchanged — moved verbatim out of
            do_POST when the second write route (comments) was added. It speaks JSON
            (a machine-to-machine push from local Orion), in contrast to the comment
            route, which speaks HTTP form data from a browser.
        """
        # 1) Authenticate FIRST: validate nothing about the payload until the caller
        # is authorized. We DO distinguish "no/!malformed header" from "wrong token"
        # in the message: with a single shared token there is no identity to
        # enumerate, so telling the two apart leaks nothing about the secret token —
        # while it meaningfully helps a self-hoster debug their push. The actual
        # secrecy guarantees live elsewhere (constant-time compare in _token_matches;
        # the token value is never echoed). Both remain 401.
        auth_error = self._auth_error()
        if auth_error is not None:
            self._send_json(
                401, {"error": auth_error}, extra_headers={"WWW-Authenticate": "Bearer"}
            )
            return

        # 2) Read and JSON-parse the body.
        raw = self._read_raw_body()
        if raw is None:
            self._send_json(400, {"error": "missing, oversized, or unreadable body"})
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "body is not valid JSON"})
            return

        # 3) Validate the blob shape/version.
        error = _validate_blob(payload)
        if error is not None:
            self._send_json(400, {"error": error})
            return

        # 4) Store and report 201 with the new id. A fresh connection per request
        # keeps each sqlite handle on its own thread.
        conn = open_relay_store(self.server.db_path)
        try:
            new_id = ingest(conn, payload, _utc_now_iso())
        finally:
            conn.close()
        print(
            f"[relay] ingested report {new_id} for project {payload['project']!r}",
            file=sys.stderr,
        )
        self._send_json(201, {"id": new_id})

    def _handle_comment(self, report_id: int) -> None:
        """Store one supervisor comment on a report (POST /report/<id>/comment, C2).

        Args:
            report_id: The report the comment attaches to (parsed from the path).

        The inbound-security checklist, enforced IN ORDER:
          1. Auth — a valid SESSION COOKIE, exactly as the GET dashboard now requires:
             gated whenever a view secret is set or users exist (401 otherwise); open
             on a bare loopback dev relay — consistent with reads.
          2. CSRF — a session cookie is auto-sent by the browser, so a forged cross-site
             POST is the threat. Require an Origin matching our canonical public origin
             (or, absent one configured, the request Host); reject a mismatch with 403.
             SameSite=Lax on the cookie is a second layer (it suppresses the cookie on
             most cross-site POSTs), but the Origin check stays the guarantee.
          3. Validate — parse the urlencoded form; require a non-empty body within the
             length caps (and a capped author); reject with 400.
          4. Report exists — 404 if the id has no report (a stale or forged link).
          5. Store, then 303 redirect back to the report (POST-redirect-GET, so a
             browser refresh does not resubmit the comment).

        Why:
            This is Orion's first write-from-a-browser surface, so it is gated more
            tightly than the feature itself. The comment text is deliberately NOT
            redaction-scanned: redaction is an OUTBOUND control for the developer's own
            secrets, whereas an inbound supervisor comment shown only on the
            access-gated dashboard is a different threat. The relevant control here is
            XSS-escaping on render (pinned in render.py), not redaction.
        """
        # One fresh connection per request (own thread), used for both the auth check
        # and the report lookup/insert below.
        conn = open_relay_store(self.server.db_path)
        try:
            # 1) Auth: a valid session is required whenever the dashboard is access-gated
            # (the same rule as do_GET). The credential is the session cookie, not Basic.
            # Capture the principal so step 5 can scope the write to the report's project.
            gated = self._auth_required(conn)
            principal = self._authenticate(conn) if gated else None
            if gated and principal is None:
                self._send_html(401, _simple_html("unauthorized", "Log in to comment."))
                return

            # 2) CSRF: require a same-origin POST (canonical Origin check).
            if self._origin_error() is not None:
                self._send_html(
                    403, _simple_html("forbidden", "Request blocked by an origin (CSRF) check.")
                )
                return

            # 3) Read and parse the urlencoded form body.
            raw = self._read_raw_body()
            if raw is None:
                self._send_html(
                    400, _simple_html("bad request", "Missing, oversized, or unreadable body.")
                )
                return
            try:
                fields = urllib.parse.parse_qs(raw.decode("utf-8"))
            except UnicodeDecodeError:
                self._send_html(400, _simple_html("bad request", "Body is not valid UTF-8."))
                return

            # parse_qs maps each key to a LIST of values; take the first (or "" if
            # absent), then strip — a name/body of only whitespace counts as empty.
            form_author = fields.get("author", [""])[0].strip()
            body = fields.get("body", [""])[0].strip()

            # 4) Validate: a non-empty body within caps, and a capped author. We validate
            # the FORM-submitted name even when logged in (defense), though it is then
            # ignored in favor of the authenticated identity below.
            if not body:
                self._send_html(400, _simple_html("bad request", "A comment body is required."))
                return
            if len(body) > MAX_COMMENT_BODY_CHARS or len(form_author) > MAX_AUTHOR_CHARS:
                self._send_html(400, _simple_html("bad request", "Comment or name is too long."))
                return

            # When the commenter is LOGGED IN, attribute the comment to their authenticated
            # identity (re-read from the session principal), NOT the self-asserted form
            # field — so a logged-in user cannot post under someone else's name. In open /
            # loopback mode there is no session, so the typed name stands. The principal
            # name comes from the DB (provisioned, or the legacy-admin sentinel), so it is
            # trusted and not re-length-checked here. (The bot's POST /api/comments path is
            # unchanged — a machine surface with its own free-text author.)
            author = principal["name"] if principal is not None else form_author

            # 5) Confirm the report exists AND is in this principal's scope, then store. An
            # out-of-scope report is reported as missing (identical to a nonexistent one),
            # so a viewer can neither write to nor confirm the existence of a report they
            # were not granted — the same existence-hiding rule do_GET applies to reads.
            allowed = self._allowed_projects(conn, principal)
            report = get(conn, report_id)
            if report is None or (
                allowed is not None and report["project"] not in allowed
            ):
                self._send_html(404, render_not_found(f"No report {report_id!r}."))
                return
            add_comment(conn, report_id, author, body, _utc_now_iso())
        finally:
            conn.close()
        # 303 + Location → the browser re-GETs the report (POST-redirect-GET), so a
        # refresh of the resulting page does not resubmit the comment.
        self._send_redirect(303, f"/report/{report_id}")

    def _handle_api_comments(self) -> None:
        """Return a project's comments as JSON for the local pull-back (GET /api/comments).

        Query parameters:
          - project  (required): the project whose comments to return.
          - since_id (optional): return only comments with id > this. A non-negative
            integer; defaults to 0 (all of the project's comments).

        The inbound-security checklist, enforced IN ORDER:
          1. Auth — Bearer, the SAME token /ingest uses (whoever can push a project's
             reports can read its replies). 401 + WWW-Authenticate: Bearer otherwise.
             Checked BEFORE any query parsing or store touch.
          2. Validate — `project` required non-empty (400 if missing/blank); `since_id`
             must be a non-negative integer (400 on anything else). "Never trust client
             input," mirroring _read_raw_body and the comment POST.
          3. Query + respond — JSON {"comments": [...], "latest_id": <n>}.

        Why:
            This is the machine-readable counterpart to the browser dashboard: the CLI
            pull-back consumes JSON over Bearer, leaving the HTML routes' Basic scheme
            untouched. latest_id lets the client advance its local unread watermark even
            when the rendered list is empty (it echoes since_id then), so the relay stays
            a dumb append-only store and "unread" is purely a per-developer local notion.
            Comments are NOT redaction-scanned (same reasoning as the comment POST: they
            are inbound supervisor text on an access-gated relay, not the developer's own
            outbound secrets), but the endpoint is still Bearer-gated.
        """
        # 1) Authenticate FIRST, exactly like /ingest — validate nothing until authorized.
        auth_error = self._auth_error()
        if auth_error is not None:
            self._send_json(
                401, {"error": auth_error}, extra_headers={"WWW-Authenticate": "Bearer"}
            )
            return

        # 2) Parse and validate the query string. do_GET strips the query for routing,
        # so we re-read it from self.path here (this is the one GET route with params).
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        # parse_qs maps each key to a LIST of values; take the first (or "" if absent).
        project = query.get("project", [""])[0].strip()
        if not project:
            self._send_json(400, {"error": "query parameter 'project' is required"})
            return
        # since_id is optional; default "0". isdigit() accepts only a non-negative
        # integer (no sign, no whitespace) — same guard do_GET uses for a report id —
        # so a negative or non-numeric value is a clean 400, never a store touch.
        since_id_raw = query.get("since_id", ["0"])[0]
        if not since_id_raw.isdigit():
            self._send_json(
                400, {"error": "query parameter 'since_id' must be a non-negative integer"}
            )
            return
        since_id = int(since_id_raw)

        # 3) Query and respond. A fresh connection per request keeps each sqlite handle
        # on its own thread (the threading-server pattern used throughout this handler).
        conn = open_relay_store(self.server.db_path)
        try:
            comments = comments_for_project(conn, project, since_id)
        finally:
            conn.close()
        # Comments are ordered by ascending id, so the last one carries the highest id;
        # with none returned the watermark stays where the client asked (since_id), so a
        # caller can always advance to latest_id unconditionally.
        latest_id = comments[-1]["id"] if comments else since_id
        self._send_json(200, {"comments": comments, "latest_id": latest_id})

    def _handle_api_comment_post(self) -> None:
        """Store a supervisor comment pushed by a machine client (POST /api/comments).

        This is the native-bot write path: a Slack/Discord bot, on a supervisor's reply,
        POSTs JSON {project, body, author?, report_id?} here and the relay appends it to
        the project's report_comments — the EXACT same store the browser comment form
        (`_handle_comment`) and the dashboard render from. So a chat reply and a dashboard
        comment are indistinguishable downstream, and `orion comments` keeps working
        unchanged.

        Body fields:
          - project   (required): the project to attach the comment to.
          - body      (required): the comment text (non-empty after strip, length-capped).
          - author    (optional): self-entered display name; "" when omitted. NOT an
            authenticated identity (that is C3) — a free-text label, length-capped.
          - report_id (optional): attach to THIS specific report instead of the latest.
            Unused by the smallest-slice bot (which always omits it → latest report), but
            accepted now so a later reply-targeting feature is a bot-only change with no
            further relay edit. When present it must name an existing report IN `project`.

        The inbound-security checklist, enforced IN ORDER (mirrors _handle_ingest):
          1. Auth — Bearer, the SAME token /ingest and GET /api/comments use (whoever can
             push a project's reports can comment on them). 401 + WWW-Authenticate: Bearer.
             Checked BEFORE the body is read. NO CSRF/Origin check is needed (unlike the
             browser `_handle_comment`): a Bearer token is never auto-attached by a browser,
             so there is no cross-site-forgery vector — the same reasoning GET /api/comments
             uses to skip it.
          2. Read + parse — the 1 MB raw-body cap (`_read_raw_body`), then JSON; a missing/
             oversized body or non-object/invalid JSON is a 400.
          3. Validate — `project`/`body` required non-empty strings; `author` optional str;
             body within MAX_COMMENT_BODY_CHARS and author within MAX_AUTHOR_CHARS. 400 otherwise.
          4. Resolve the target report — by `report_id` if given (404 if it does not exist,
             400 if it belongs to another project), else the project's LATEST report (404 if
             the project has no reports yet — an expected state, e.g. a channel mapped before
             the first report).
          5. Store + 201 — `add_comment`, then {"id", "report_id"}.

        Why:
            Comments are deliberately NOT redaction-scanned — same rationale as
            `_handle_comment` and `_handle_api_comments`: redaction is an OUTBOUND control
            for the developer's own secrets, whereas an inbound supervisor comment shown
            only on the access-gated dashboard is a different threat (the control there is
            XSS-escaping on render, in render.py). This handler stays Bearer-gated all the same.
        """
        # 1) Authenticate FIRST, exactly like /ingest — validate nothing until authorized.
        auth_error = self._auth_error()
        if auth_error is not None:
            self._send_json(
                401, {"error": auth_error}, extra_headers={"WWW-Authenticate": "Bearer"}
            )
            return

        # 2) Read and JSON-parse the body (1 MB cap inside _read_raw_body).
        raw = self._read_raw_body()
        if raw is None:
            self._send_json(400, {"error": "missing, oversized, or unreadable body"})
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "body is not valid JSON"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "payload must be a JSON object"})
            return

        # 3) Validate the fields. project and body are required strings; author is an
        # optional string defaulting to "". Strip both text fields so a whitespace-only
        # body counts as empty — same as the browser comment route.
        project = payload.get("project")
        body = payload.get("body")
        author = payload.get("author", "")
        if not isinstance(project, str) or not project.strip():
            self._send_json(400, {"error": "field 'project' must be a non-empty string"})
            return
        if not isinstance(body, str):
            self._send_json(400, {"error": "field 'body' must be a string"})
            return
        if not isinstance(author, str):
            self._send_json(400, {"error": "field 'author' must be a string"})
            return
        project = project.strip()
        body = body.strip()
        author = author.strip()
        if not body:
            self._send_json(400, {"error": "a comment body is required"})
            return
        if len(body) > MAX_COMMENT_BODY_CHARS or len(author) > MAX_AUTHOR_CHARS:
            self._send_json(400, {"error": "comment or author is too long"})
            return

        # report_id is optional. When present it must be a plain integer (JSON true/false
        # are ints in Python, so reject bools explicitly) — anything else is a 400 before
        # we touch the store.
        report_id = payload.get("report_id")
        if report_id is not None and (
            not isinstance(report_id, int) or isinstance(report_id, bool)
        ):
            self._send_json(400, {"error": "field 'report_id' must be an integer"})
            return

        # 4) Resolve the target report, then 5) store. One fresh connection per request
        # keeps each sqlite handle on its own thread (the threading-server pattern).
        conn = open_relay_store(self.server.db_path)
        try:
            if report_id is not None:
                # Explicit target: it must exist AND belong to the named project, so a
                # client cannot attach a comment to another project's report by id.
                target = get(conn, report_id)
                if target is None:
                    self._send_json(404, {"error": f"no report {report_id}"})
                    return
                if target["project"] != project:
                    self._send_json(
                        400, {"error": "report_id does not belong to project"}
                    )
                    return
            else:
                # Default (the smallest-slice bot path): the project's LATEST report.
                # history() is newest-first, so [0] is the most recent.
                reports = history(conn, project)
                if not reports:
                    self._send_json(
                        404, {"error": f"no reports for project {project!r}"}
                    )
                    return
                report_id = reports[0]["id"]

            new_id = add_comment(conn, report_id, author, body, _utc_now_iso())
        finally:
            conn.close()
        self._send_json(201, {"id": new_id, "report_id": report_id})

    def _handle_create_user(self) -> None:
        """Provision a user and return their raw login key ONCE (POST /api/users).

        Body fields:
          - name     (required): the user's unique display name / handle.
          - role     (optional): "viewer" (default) or "admin"; allowlisted.
          - projects (optional): project names the viewer is scoped to (ignored for an
            admin, which sees all). Defaults to [].

        The inbound-security checklist, IN ORDER (mirrors _handle_api_comment_post):
          1. Auth — the ADMIN token (NOT the ingest token); 401 otherwise, before the body.
          2. Read + parse — 1 MB cap, then JSON; non-object/invalid -> 400.
          3. Validate — name non-empty str; role in the allowlist; projects a list of str.
          4. Mint + store — a fresh ≥256-bit key, store only its peppered verifier, write
             an audit row; a duplicate name is a 409 (never clobbers an existing user).
          5. Respond 201 with {id, name, role, projects, key} — the raw key shown ONCE.

        Why:
            This is the only way a user is created, and the raw key exists only in this
            response: it is minted here, stored as a verifier (never in cleartext), and
            NEVER logged. Returning it once is the whole "shown once at creation" model.
            A duplicate name must not overwrite the existing user's credential/scope (that
            would silently change who can log in), so the UNIQUE(name) collision is
            surfaced as a 409, not a clobber.
        """
        # 1) Admin auth FIRST — validate nothing until authorized; only the admin token
        # passes (the ingest token does not), and a missing admin config is refused.
        auth_error = self._auth_admin_error()
        if auth_error is not None:
            self._send_json(
                401, {"error": auth_error}, extra_headers={"WWW-Authenticate": "Bearer"}
            )
            return

        # 2) Read + JSON-parse the body (1 MB cap inside _read_raw_body).
        raw = self._read_raw_body()
        if raw is None:
            self._send_json(400, {"error": "missing, oversized, or unreadable body"})
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "body is not valid JSON"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "payload must be a JSON object"})
            return

        # 3) Validate. name required non-empty; role allowlisted; projects a list of str.
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            self._send_json(400, {"error": "field 'name' must be a non-empty string"})
            return
        name = name.strip()
        role = payload.get("role", "viewer")
        if role not in _PROVISIONABLE_ROLES:
            self._send_json(
                400,
                {"error": f"field 'role' must be one of {list(_PROVISIONABLE_ROLES)}"},
            )
            return
        projects = payload.get("projects", [])
        if not isinstance(projects, list) or not all(
            isinstance(p, str) for p in projects
        ):
            self._send_json(400, {"error": "field 'projects' must be a list of strings"})
            return
        # Normalize: strip, drop blanks, dedupe (preserving order) — a scope should not
        # carry whitespace-only or duplicate project names into the store.
        projects = list(dict.fromkeys(p.strip() for p in projects if p.strip()))

        # The pepper is required to compute the verifier. cmd_relay_serve fails closed
        # when the admin token is set without it, so this is a defensive guard only.
        if self.server.user_pepper is None:
            self._send_json(500, {"error": "server is missing the user pepper"})
            return

        # 4) Mint a fresh key, store ONLY its peppered verifier + scope, audit. The raw
        # key lives in a local just long enough to return it; it is never logged.
        raw_key = mint_key()
        verifier = key_verifier(self.server.user_pepper, raw_key)
        conn = open_relay_store(self.server.db_path)
        try:
            try:
                user_id = add_user(
                    conn, name, verifier, role, projects, _ADMIN_ACTOR, _utc_now_iso()
                )
            except sqlite3.IntegrityError:
                # UNIQUE(name) (or an astronomically-unlikely verifier collision): a user
                # by this name already exists. Refuse rather than clobber their access.
                self._send_json(409, {"error": f"a user named {name!r} already exists"})
                return
            record_admin_audit(
                conn, _ADMIN_ACTOR, "create_user", name, role, projects, _utc_now_iso()
            )
        finally:
            conn.close()
        # 5) Return the raw key ONCE — the only place it ever appears.
        self._send_json(
            201,
            {"id": user_id, "name": name, "role": role, "projects": projects, "key": raw_key},
        )

    def _handle_list_users(self) -> None:
        """Return all users (without credential material) as JSON (GET /api/users).

        The inbound-security checklist:
          1. Auth — the ADMIN token; 401 otherwise, before any query.
          2. Query + respond — {"users": [...]} from list_users (which excludes
             key_verifier by construction).

        Why:
            Backs `relay-user list`. list_users deliberately omits key_verifier, so this
            admin view can never surface credential material (even hashed). Admin-token
            gated like the rest of the provisioning surface.
        """
        auth_error = self._auth_admin_error()
        if auth_error is not None:
            self._send_json(
                401, {"error": auth_error}, extra_headers={"WWW-Authenticate": "Bearer"}
            )
            return
        conn = open_relay_store(self.server.db_path)
        try:
            users = list_users(conn)
        finally:
            conn.close()
        self._send_json(200, {"users": users})

    def _handle_revoke_user(self) -> None:
        """Revoke a user by name: deactivate + force-logout (POST /api/users/revoke).

        Body fields:
          - name (required): the user to revoke.

        The inbound-security checklist, IN ORDER:
          1. Auth — the ADMIN token; 401 otherwise, before the body.
          2. Read + parse — 1 MB cap, JSON object; non-object/invalid -> 400.
          3. Validate — name a non-empty string.
          4. Resolve + revoke — 404 if no such user; else revoke_user (active=0 +
             session_version bump, atomically) and write an audit row.
          5. Respond 200 {name, revoked: true}.

        Why:
            Revocation is a settled Increment-1 requirement (decision C): "grant but
            can't revoke" is an unacceptable gap for sharing. revoke_user does the
            deactivation AND the session-version bump in one UPDATE, so the user's future
            logins are denied and any cookie already in a browser stops working on its
            next request — instant, stateless force-logout. Audited like creation.
        """
        auth_error = self._auth_admin_error()
        if auth_error is not None:
            self._send_json(
                401, {"error": auth_error}, extra_headers={"WWW-Authenticate": "Bearer"}
            )
            return
        raw = self._read_raw_body()
        if raw is None:
            self._send_json(400, {"error": "missing, oversized, or unreadable body"})
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "body is not valid JSON"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "payload must be a JSON object"})
            return
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            self._send_json(400, {"error": "field 'name' must be a non-empty string"})
            return
        name = name.strip()
        conn = open_relay_store(self.server.db_path)
        try:
            user = get_user_by_name(conn, name)
            if user is None:
                self._send_json(404, {"error": f"no user named {name!r}"})
                return
            revoke_user(conn, user["id"])
            record_admin_audit(
                conn, _ADMIN_ACTOR, "revoke_user", name, user["role"], [], _utc_now_iso()
            )
        finally:
            conn.close()
        self._send_json(200, {"name": name, "revoked": True})

    def _auth_error(self) -> str | None:
        """Return None if the request is authorized, else a 401 reason string.

        Returns:
            None when a valid Bearer token is present; otherwise a short message
            distinguishing a missing/malformed header from a wrong token.

        Why:
            We separate the two failure modes deliberately. With one shared token
            there is no account to enumerate, so revealing "you sent no token" vs
            "your token is wrong" tells an attacker nothing about the secret value —
            they already know what they sent — while it lets a legitimate self-hoster
            tell a config-omission from a mismatch at a glance. The real defenses are
            unchanged: the comparison is constant-time (hmac.compare_digest, so a
            timing side-channel can't reveal the token byte by byte), and the
            expected token is never echoed in any message.
        """
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return "missing or malformed Authorization header (expected 'Bearer <token>')"
        if not hmac.compare_digest(header[len(prefix):], self.server.token):
            return "invalid token"
        return None

    def _auth_admin_error(self) -> str | None:
        """Return None if the request bears the ADMIN token, else a reason string.

        Returns:
            None when a valid `Bearer <admin_token>` is present; otherwise a short
            message (provisioning disabled / missing header / wrong token).

        Why:
            Provisioning is gated by the SEPARATE admin token, never the ingest token —
            "the ingest token must NOT create users" (Codex hardening: independent
            secrets bound the blast radius). So this compares ONLY against
            self.server.admin_token, constant-time (hmac.compare_digest), and never
            against self.server.token — a client holding the ingest token therefore
            cannot provision. When no admin token is configured, provisioning is OFF and
            every request is refused. The admin token is never echoed in any message.
        """
        if self.server.admin_token is None:
            return "provisioning is disabled (no admin token configured)"
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return "missing or malformed Authorization header (expected 'Bearer <token>')"
        if not hmac.compare_digest(header[len(prefix):], self.server.admin_token):
            return "invalid admin token"
        return None

    def _read_session_cookie(self) -> str | None:
        """Return the raw session-cookie value from the request, or None.

        Why:
            Parsing the Cookie header with http.cookies.SimpleCookie applies the cookie
            quoting/splitting rules correctly (vs. a hand-rolled split). A malformed
            Cookie header degrades to None ("not authenticated"), never an exception.
        """
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except CookieError:
            return None
        morsel = jar.get(_SESSION_COOKIE_NAME)
        return morsel.value if morsel else None

    def _cookie_header(self, value: str, max_age: int) -> str:
        """Build a single Set-Cookie header value for the session cookie.

        Args:
            value: The cookie value (an empty string with max_age=0 clears it on logout).
            max_age: Lifetime in seconds.

        Why:
            SimpleCookie.OutputString() emits one correctly-formatted Set-Cookie (proper
            attribute syntax) instead of a hand-joined string. The attributes ARE the
            session's security posture: HttpOnly (no JS access → no theft via XSS),
            SameSite=Lax (the browser won't send it on a cross-site POST — CSRF
            defense-in-depth), Path=/ (the whole dashboard), and Secure WHEN the relay is
            HTTPS-exposed (from the proxy signal) so the cookie can't leak over plaintext,
            while a bare-loopback http dev relay still works (Secure off).
        """
        jar = SimpleCookie()
        jar[_SESSION_COOKIE_NAME] = value
        morsel = jar[_SESSION_COOKIE_NAME]
        morsel["path"] = "/"
        morsel["httponly"] = True
        morsel["samesite"] = "Lax"
        morsel["max-age"] = max_age
        if self.server.secure_cookie:
            morsel["secure"] = True
        return morsel.OutputString()

    def _users_exist(self, conn) -> bool:
        """True once any user has been provisioned (the dashboard is then multi-user)."""
        return conn.execute("SELECT 1 FROM relay_users LIMIT 1").fetchone() is not None

    def _auth_required(self, conn) -> bool:
        """Whether this request's dashboard route is access-gated.

        Why:
            Gated when a view secret is configured (the historical signal) OR once any
            user exists (provisioning implies access should be controlled). A bare
            loopback dev relay with neither stays open, exactly as before.
        """
        return bool(self.server.view_token) or self._users_exist(conn)

    def _legacy_admin_allowed(self, conn) -> bool:
        """Whether the legacy shared view key may still log in as a bootstrap admin.

        Why:
            Backward-compatible bootstrap: while there are NO provisioned users, the old
            view secret still gets you in (so a deploy is not locked out before it
            provisions anyone). Once users exist it is OFF unless the operator explicitly
            opts back in (allow_legacy_admin) — Codex's gated/deprecated-legacy guidance,
            so the shared key never silently remains a permanent peer to named users.
        """
        if not self.server.view_token:
            return False
        if self.server.allow_legacy_admin:
            return True
        return not self._users_exist(conn)

    def _authenticate(self, conn) -> dict | None:
        """Resolve the current principal from the session cookie, re-reading the DB.

        Returns:
            {"user_id", "role", "name"} for a valid session, else None. The legacy
            bootstrap admin (cookie sub == 0) resolves to a synthetic admin principal
            (user_id None) only while still permitted.

        Why:
            AuthZ trusts the DB, not the cookie. We verify the cookie's signature and
            expiry (verify_session_value), THEN re-load the user by id and confirm it is
            still active and its session_version still matches the cookie's — so a
            revoked or force-logged-out user is rejected on their very next request, with
            no server-side session store. The cookie never carries role/scope, so a
            privilege can't ride in a stale/forged cookie.
        """
        if self.server.session_key is None:
            return None
        value = self._read_session_cookie()
        if not value:
            return None
        claims = verify_session_value(self.server.session_key, value, int(time.time()))
        if claims is None:
            return None
        sub = claims.get("sub")
        # Legacy bootstrap admin: a reserved sentinel id (0) with no relay_users row.
        if sub == 0:
            if self._legacy_admin_allowed(conn):
                return {"user_id": None, "role": "admin", "name": "legacy-admin"}
            return None
        if not isinstance(sub, int):
            return None
        user = get_user_by_id(conn, sub)
        if user is None or not user["active"] or user["session_version"] != claims.get("sv"):
            return None
        return {"user_id": user["id"], "role": user["role"], "name": user["name"]}

    def _allowed_projects(self, conn, principal: dict | None) -> set | None:
        """Resolve which project names `principal` may see, or None for unrestricted.

        Args:
            conn: An open relay-store connection.
            principal: The authenticated principal from _authenticate, or None on an open
                (ungated) relay where no one logs in.

        Returns:
            None when the principal sees EVERYTHING — an admin, the legacy bootstrap admin
            (both role == "admin"), or an open relay (principal is None, reads are public).
            Otherwise a set of the viewer's allowed project names (possibly empty —
            default-deny, so a viewer with no grants sees nothing).

        Why:
            One place computes read scope, so every route (the index filter, /project,
            /report, and the comment POST) enforces the SAME rule from current DB state —
            authZ never drifts between routes. None vs a set is the explicit "unrestricted
            vs scoped" distinction; returning a set makes the membership checks at each
            call site a trivial, hard-to-get-wrong `in`. Scope is re-read per request (not
            cached on the cookie), so a regrant/revoke takes effect immediately.
        """
        if principal is None or principal["role"] == "admin":
            return None
        return set(projects_for_user(conn, principal["user_id"]))

    def _handle_login(self) -> None:
        """Verify a posted access key and set the session cookie (POST /login).

        Why:
            The login key is the credential: we recompute its verifier (HMAC with the
            pepper) and look it up; a matching ACTIVE user gets a fresh signed session
            cookie (sub = user id, sv = their session_version) and a redirect to the
            dashboard. The legacy view key, while still permitted, logs in as the
            bootstrap admin (sub = 0). A miss re-renders /login with a GENERIC error (we
            never reveal which part failed, so the form leaks nothing about which keys
            exist). All key compares are constant-time and the key is never logged.
        """
        # An unconfigured (no session key) relay has nothing to log into — treat /login
        # as a no-op redirect rather than erroring.
        if self.server.session_key is None:
            self._send_redirect(303, "/")
            return
        conn = open_relay_store(self.server.db_path)
        try:
            raw = self._read_raw_body()
            key = ""
            if raw is not None:
                try:
                    key = urllib.parse.parse_qs(raw.decode("utf-8")).get("key", [""])[0]
                except UnicodeDecodeError:
                    key = ""

            sub = sv = None
            if key and self.server.user_pepper is not None:
                user = get_user_by_verifier(conn, key_verifier(self.server.user_pepper, key))
                if user is not None and user["active"]:
                    sub, sv = user["id"], user["session_version"]
                    update_last_login(conn, user["id"], _utc_now_iso())
            if (
                sub is None
                and key
                and self.server.view_token
                and self._legacy_admin_allowed(conn)
                and hmac.compare_digest(key, self.server.view_token)
            ):
                sub, sv = 0, 0  # legacy bootstrap admin

            if sub is None:
                self._send_html(401, render_login("Invalid or expired access key."))
                return

            now = int(time.time())
            value = make_session_value(
                self.server.session_key, sub, sv, now, now + self.server.session_seconds
            )
            self._send_redirect(
                303,
                "/",
                extra_headers={
                    "Set-Cookie": self._cookie_header(value, self.server.session_seconds)
                },
            )
        finally:
            conn.close()

    def _handle_logout(self) -> None:
        """Clear the session cookie and redirect to /login (GET /logout)."""
        self._send_redirect(
            303, "/login", extra_headers={"Set-Cookie": self._cookie_header("", 0)}
        )

    def _origin_error(self) -> str | None:
        """Return None if the request is same-origin, else a 403 reason string.

        Returns:
            None when an Origin header is present and its host (netloc) equals the
            request's Host header; otherwise a short reason — Origin missing, or
            Origin not matching Host.

        Why:
            The comment POST is now authenticated by the session cookie, which the
            browser AUTO-SENDS on every request to this origin — including one a
            malicious third-party page triggers (classic CSRF). The Origin check is the
            primary defense (SameSite=Lax on the cookie is a second layer). When a
            canonical public origin is configured (the deployed HTTPS URL), we require
            the Origin to equal it EXACTLY — scheme+host+port — rather than merely match
            the Host header, which a client can set and a proxy rewrites (Codex's
            don't-blind-trust-Host point). On a loopback dev relay with none configured,
            we fall back to comparing the Origin's netloc against Host.
        """
        origin = self.headers.get("Origin")
        if not origin:
            return "missing Origin header"
        if self.server.public_origin:
            # Exact canonical-origin match (trailing slash ignored). Nothing about the
            # request (Host, proxy headers) is trusted — only the configured value.
            if origin.rstrip("/") != self.server.public_origin.rstrip("/"):
                return "Origin does not match the configured public origin"
            return None
        if urllib.parse.urlparse(origin).netloc != self.headers.get("Host", ""):
            return "Origin does not match Host"
        return None

    def _read_raw_body(self) -> bytes | None:
        """Read the request body per Content-Length, or None if absent/oversized.

        Returns:
            The body bytes, or None when the length header is missing, non-numeric,
            non-positive, or above the size cap (all of which become a 400).

        Why:
            We never trust a client-sent length: a non-numeric or absurd
            Content-Length must be rejected cleanly rather than crash or allocate
            unbounded memory. None is the single "bad request body" signal the
            caller turns into a 400.
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            return None
        if length <= 0 or length > _MAX_BLOB_BYTES:
            return None
        return self.rfile.read(length)

    def _security_headers(self, *, html: bool) -> dict:
        """Return the standard security response headers for this response.

        Args:
            html: True for an HTML response, which additionally gets the CSP and the
                legacy X-Frame-Options (both only meaningful for a document a browser
                renders/frames); False for JSON and bodyless redirects.

        Returns:
            A header-name -> value dict for the response writer to merge in.

        Why:
            Defense-in-depth on an internet-facing surface, defined ONCE so all three
            response writers send a consistent set (DRY). X-Content-Type-Options and
            Referrer-Policy ride EVERY response. HSTS is gated on secure_cookie — the
            same hosted-HTTPS signal the cookie's Secure attribute uses — so a plain
            HTTP loopback dev relay never claims HTTPS-only (which would wedge it in a
            browser). The CSP (from _CONTENT_SECURITY_POLICY) and the legacy
            X-Frame-Options clickjacking cover go on HTML only, where a browser renders
            or frames the response — they are meaningless on a JSON API reply.
        """
        headers = {
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        }
        # HSTS only in a hosted HTTPS posture: a plain-http loopback dev relay must not
        # send an HTTPS-only directive (the browser would then refuse plain http to it).
        # 2 years, includeSubDomains — the standard durable HSTS lifetime.
        if self.server.secure_cookie:
            headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        if html:
            headers["Content-Security-Policy"] = _CONTENT_SECURITY_POLICY
            # Legacy clickjacking cover for older browsers, alongside the CSP's
            # frame-ancestors 'none' (which supersedes it in modern ones).
            headers["X-Frame-Options"] = "DENY"
        return headers

    def _send_json(
        self, code: int, obj: dict, *, extra_headers: dict | None = None
    ) -> None:
        """Write a JSON response with the given status code.

        Args:
            code: HTTP status code.
            obj: The JSON-serializable response body.
            extra_headers: Optional extra response headers (e.g. WWW-Authenticate on
                a 401). Defaulted so the common case stays a two-arg call.

        Why:
            Every relay response (success or error) is small JSON, so this one
            helper keeps the status/header/body sequence in a single place (DRY) and
            guarantees a correct Content-Length on each reply. extra_headers keeps
            the one case that needs an additional header (the 401's spec-mandated
            WWW-Authenticate) from forking this method.
        """
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # Security headers first, then any caller-supplied extras (which take
        # precedence on a name clash, though none currently collide).
        merged = {**self._security_headers(html=False), **(extra_headers or {})}
        for name, value in merged.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(
        self, code: int, html_str: str, *, extra_headers: dict | None = None
    ) -> None:
        """Write an HTML response with the given status code.

        Args:
            code: HTTP status code.
            html_str: The HTML body.
            extra_headers: Optional extra response headers (e.g. WWW-Authenticate on a
                401). Defaulted so the common case stays a two-arg call.

        Why:
            The dashboard responses are all UTF-8 HTML; this keeps the
            status/header/body sequence and the charset in one place (DRY), so each
            view function just returns a string and never deals with the socket.
            extra_headers keeps the one case that needs an extra header (the dashboard
            401's WWW-Authenticate) from forking this method — same shape as _send_json.
        """
        body = html_str.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # HTML responses additionally carry the CSP and X-Frame-Options (html=True).
        # Security headers first, then caller extras (precedence on a clash).
        merged = {**self._security_headers(html=True), **(extra_headers or {})}
        for name, value in merged.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_redirect(
        self, code: int, location: str, *, extra_headers: dict | None = None
    ) -> None:
        """Write a bodyless redirect response to `location`.

        Args:
            code: The 3xx status code (303 for POST-redirect-GET).
            location: The path to redirect to (a site-relative path is fine).
            extra_headers: Optional extra response headers — used to attach a
                Set-Cookie on the login/logout redirects (set the session cookie while
                redirecting to the dashboard, or clear it while redirecting to /login).

        Why:
            After a successful comment POST we 303 back to the report page so the
            browser re-fetches it with a GET — the POST-redirect-GET pattern, which
            stops a page refresh from resubmitting the comment. Login/logout reuse the
            same redirect but also need to (un)set the session cookie, hence
            extra_headers — same shape as _send_html/_send_json. A redirect needs no
            body, so we send an explicit zero Content-Length plus the Location header.
        """
        self.send_response(code)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        # A redirect carries no rendered body, so it gets the common headers only
        # (html=False — no CSP/X-Frame-Options). Caller extras (e.g. the login/logout
        # Set-Cookie) take precedence on a clash.
        merged = {**self._security_headers(html=False), **(extra_headers or {})}
        for name, value in merged.items():
            self.send_header(name, value)
        self.end_headers()


@dataclass(frozen=True)
class AuthConfig:
    """The relay's multi-party auth/session configuration (Increment 1).

    Args:
        session_key: HMAC key for signing session cookies (independent secret). None
            disables sessions (a bare, open dev relay).
        user_pepper: HMAC pepper for the stored key verifiers (independent secret).
        admin_token: Bearer token for the provisioning endpoint (independent of the
            ingest token). None disables provisioning.
        secure_cookie: Set the cookie's Secure attribute (true when HTTPS-exposed).
        session_seconds: Session lifetime / cookie Max-Age in seconds.
        public_origin: The canonical external origin (e.g. "https://app.fly.dev") the
            comment-POST Origin must match. None falls back to Origin-vs-Host.
        allow_legacy_admin: Keep the legacy shared view key usable as admin even after
            users exist (default off → it is bootstrap-only).

    Why:
        Bundling the seven auth knobs into one frozen object keeps RelayServer /
        create_server / serve signatures readable, and makes "this is the auth posture"
        a single explicit thing to pass and reason about. Each secret is INDEPENDENT
        (never derived from / shared with the view or ingest tokens) per the Codex
        /second-opinion. The cookie attributes and origin live here so the request
        handler reads them via self.server, the same pattern as db_path/token.
    """

    session_key: bytes | None = None
    user_pepper: bytes | None = None
    admin_token: str | None = None
    secure_cookie: bool = False
    session_seconds: int = 30 * 24 * 3600
    public_origin: str | None = None
    allow_legacy_admin: bool = False


class RelayServer(ThreadingHTTPServer):
    """A threaded HTTP server carrying the relay's db path and auth token.

    Args:
        server_address: The (host, port) to bind. Port 0 lets the OS pick a free
            port (used by tests).
        db_path: Path to the relay's sqlite store.
        token: The shared Bearer token ingest is authenticated against.
        view_token: Optional shared secret for dashboard (GET) read auth via HTTP
            Basic. None (the default) leaves reads open — valid only for a loopback
            bind, which create_server() enforces with a fail-closed guard.
        display_tz: The IANA zone the dashboard renders timestamps in. Defaults to
            render.py's Pacific constant, so an operator who passes no --timezone gets
            the historical California output unchanged.

    Why:
        Subclassing ThreadingHTTPServer to hold db_path/token is the clean way to
        make per-server config available to each request handler (handlers read it
        via self.server). The display zone rides on the server object the same way,
        so each per-request handler reads it via self.server.display_tz rather than a
        module global. ThreadingHTTPServer handles each request on its own (daemon)
        thread, so a slow request never blocks the next push.
    """

    # Inherited as True from ThreadingHTTPServer; stated for clarity so threads
    # never keep the process alive after shutdown.
    daemon_threads = True

    def __init__(
        self,
        server_address,
        db_path: Path,
        token: str,
        view_token: str | None = None,
        display_tz: ZoneInfo = _DISPLAY_TZ,
        auth: "AuthConfig | None" = None,
    ):
        # Set config before binding so it is available to any request handled after
        # serve_forever() starts.
        self.db_path = db_path
        self.token = token
        self.view_token = view_token
        self.display_tz = display_tz
        # Spread the auth bundle onto the server so each handler reads the individual
        # knobs via self.server.<name> (the existing per-request access pattern).
        auth = auth or AuthConfig()
        self.session_key = auth.session_key
        self.user_pepper = auth.user_pepper
        self.admin_token = auth.admin_token
        self.secure_cookie = auth.secure_cookie
        self.session_seconds = auth.session_seconds
        self.public_origin = auth.public_origin
        self.allow_legacy_admin = auth.allow_legacy_admin
        super().__init__(server_address, _RelayHandler)


def _is_loopback(host: str) -> bool:
    """Return True if `host` names a loopback-only bind interface.

    Args:
        host: The interface string passed to bind (an IP literal or a hostname).

    Returns:
        True for loopback (127.0.0.0/8, ::1, or the literal "localhost"); False for
        anything potentially world-/LAN-reachable (0.0.0.0, a LAN IP, a hostname).

    Why:
        The fail-closed read-auth guard keys off this: a non-loopback bind must carry
        a view secret. We treat the literal "localhost" as loopback (it resolves
        there); for IP literals we defer to ipaddress.is_loopback (covers 127.x.x.x
        and ::1, and correctly rules 0.0.0.0 NOT loopback). Any other hostname is
        treated as non-loopback — for a security guard the safe default is to assume
        reachable.
    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def create_server(
    host: str,
    port: int,
    db_path: Path,
    token: str,
    view_token: str | None = None,
    require_view_auth: bool = False,
    display_tz: ZoneInfo = _DISPLAY_TZ,
    auth: "AuthConfig | None" = None,
) -> RelayServer:
    """Build and bind a RelayServer (does not start serving).

    Args:
        host: Interface to bind (e.g. "127.0.0.1").
        port: Port to bind; 0 lets the OS choose a free one.
        db_path: Path to the relay's sqlite store.
        token: The shared Bearer ingest token.
        view_token: Optional shared secret for dashboard read auth. Required (this
            function raises without it) whenever `host` is non-loopback, or whenever
            require_view_auth is set.
        require_view_auth: When True, demand view_token even on a LOOPBACK bind. For
            the reverse-proxy topology (relay on loopback, a proxy exposing it), where
            the host-based guard alone can't tell the dashboard is publicly reachable
            (KI-18). Off by default so local loopback dev stays password-free.
        display_tz: The IANA zone the dashboard renders timestamps in. Defaults to the
            Pacific constant, so an omitted --timezone keeps the historical output.

    Returns:
        A bound RelayServer. Its actual bound port is server.server_address[1]
        (useful when port 0 was requested).

    Raises:
        ValueError: when a view secret is required but absent — the fail-closed guard.
            That is either (a) a non-loopback bind, which would expose the dashboard to
            the network, or (b) require_view_auth on a loopback bind (the proxy case).
            We refuse to start rather than serve the dashboard unauthenticated.

    Why:
        Separating "build/bind" from "serve forever" makes the server testable: a
        test binds to port 0, reads the assigned port, drives requests on a thread,
        then calls shutdown() — none of which is possible if construction blocked.
        The guard lives HERE (not only in the CLI) so every path to a bound server —
        serve() and tests alike — is fail-closed by construction. Enforcement itself
        needs nothing extra: do_GET already requires Basic auth whenever a view secret
        is set, and these guards guarantee it is set when it must be.
    """
    # (b) Proxy case: read-auth forced even though the bind looks loopback-safe.
    if require_view_auth and _is_loopback(host) and not view_token:
        raise ValueError(
            "refusing to start: --require-view-auth is set but no dashboard view "
            "secret is configured — the dashboard would be served unauthenticated. "
            "Set a view secret (relay-serve --view-token-env) first."
        )
    # (a) Non-loopback bind: the dashboard would be reachable on the network.
    if not _is_loopback(host) and not view_token:
        raise ValueError(
            f"refusing to bind non-loopback host {host!r} without a dashboard view "
            "secret: the read-only dashboard would be world-readable. Set a view "
            "secret (relay-serve --view-token-env) before binding beyond loopback."
        )
    return RelayServer((host, port), db_path, token, view_token, display_tz, auth)


def serve(
    host: str,
    port: int,
    db_path: Path,
    token: str,
    view_token: str | None = None,
    require_view_auth: bool = False,
    display_tz: ZoneInfo = _DISPLAY_TZ,
    auth: "AuthConfig | None" = None,
) -> None:
    """Run the relay server until interrupted (the blocking entry point).

    Args:
        host: Interface to bind.
        port: Port to bind.
        db_path: Path to the relay's sqlite store.
        token: The shared Bearer ingest token.
        view_token: Optional dashboard read secret (HTTP Basic). Required by
            create_server() when host is non-loopback (or require_view_auth is set).
        require_view_auth: Force the view secret even on a loopback bind (the
            reverse-proxy case; see create_server / KI-18).
        display_tz: The IANA zone the dashboard renders timestamps in. Defaults to the
            Pacific constant, so an omitted --timezone keeps the historical output.

    Returns:
        None. Blocks serving requests until Ctrl-C, then shuts down cleanly.

    Why:
        This is what `orion relay-serve` calls. It prints the bound address and
        whether the dashboard requires read auth, so the user can see at a glance
        that a non-loopback bind is protected; it handles Ctrl-C as a clean stop
        rather than a traceback.
    """
    server = create_server(
        host, port, db_path, token, view_token, require_view_auth, display_tz, auth
    )
    bound_host, bound_port = server.server_address
    # Surface read-auth state at startup — the operator's confirmation that a
    # world-reachable bind is gated (the fail-closed guard guarantees it is).
    auth_state = "login required" if view_token else "open (loopback only)"
    print(
        f"[relay] listening on http://{bound_host}:{bound_port}  "
        f"(db: {db_path}; dashboard: {auth_state})",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

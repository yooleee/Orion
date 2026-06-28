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
import mimetypes
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

from . import api
from .derive import today_in_tz
from .store import (
    add_comment,
    add_user,
    comments_for,
    comments_for_project,
    get,
    get_checklist,
    get_project_kind,
    get_user_by_id,
    get_user_by_name,
    get_user_by_verifier,
    history,
    ingest,
    latest_report_per_project,
    list_users,
    observed_history,
    open_relay_store,
    projects_for_user,
    record_admin_audit,
    record_observations,
    revoke_user,
    set_project_kind,
    update_last_login,
    upsert_checklist,
)

# The default IANA zone the dashboard renders timestamps in (the SPA reads it from
# GET /api/me and formats relative time client-side). Defaulted so an omitted
# --timezone keeps the historical Pacific output. Formerly lived in render.py; it
# moved here when render.py retired (E2 Inc 4, KI-23), this being its only consumer.
_DISPLAY_TZ = ZoneInfo("America/Los_Angeles")

# Reject a Content-Length larger than this outright. A report blob is a few KB; 1 MB
# is far above any real payload but well below "read gigabytes into memory". Cheap
# defensive hygiene on an inbound surface — we do not trust a client-sent length.
_MAX_BLOB_BYTES = 1_000_000

# Per-field caps for a supervisor comment (C2): the semantic limits on the DECODED
# comment body / author, enforced by the JSON comment write handlers. The 1 MB raw-body
# cap above remains the outer memory guard. These formerly lived in render.py (where the
# form's maxlength hint also used them); they moved here when render.py retired (E2 Inc 4,
# KI-23). NOTE: the native-bot package keeps its OWN independent copies in orion.bot.core.
MAX_COMMENT_BODY_CHARS = 4_000
MAX_AUTHOR_CHARS = 200

# The blob fields the relay consumes, each required to be a string. NOTE: the legacy
# `source_marker` field (removed from the producer in KI-8; older blobs may still carry
# it, and the store ignores it either way) is intentionally NOT required — the relay
# stays backward-compatible by validating only the fields it consumes, never rejecting a
# missing or extra one. orion_version is here too, and additionally checked non-empty below: it
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
# E2 Inc 4: the CSP for the React SPA (the --web-dir static responses). Vite emits EXTERNAL
# hashed JS/CSS (no inline <script>/<style>), so script-src stays STRICT 'self' — that is the
# XSS guarantee, the same one the old hash-based policy gave. style-src adds 'unsafe-inline'
# ONLY because the SPA uses data-driven inline style ATTRIBUTES (progress-bar widths, status
# colours) that a static stylesheet cannot express; this allows no script execution, so the
# XSS property is unaffected. font-src 'self' because the fonts are self-hosted (not a CDN);
# connect-src 'self' for the SPA's same-origin fetch('/api/...'); img-src adds data: for any
# tiny inlined SVG (assetsInlineLimit is 0, but data: is harmless and future-proofs icons).
_SPA_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
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


def _checklist_items_error(checklist: object) -> str | None:
    """Return None when `checklist` is a valid list of {text, done} items, else a reason.

    Args:
        checklist: The candidate value (untrusted) — expected to be a list of objects
            each with a string `text` and a boolean `done`.

    Returns:
        None when the shape is valid; otherwise a short human-readable 400 reason.

    Why:
        Both the report ingest (`_validate_blob`, where the checklist is OPTIONAL) and
        the dedicated checklist push (`_validate_checklist_request`, where it is
        REQUIRED) need the SAME item-shape check — the exact shape get_checklist decodes
        and the renderer unpacks. Factoring it here keeps that one rule in one place
        (DRY) so the two endpoints can never validate the items differently.
    """
    if not isinstance(checklist, list):
        return "field 'checklist' must be a list"
    for item in checklist:
        if not (
            isinstance(item, dict)
            and isinstance(item.get("text"), str)
            and isinstance(item.get("done"), bool)
        ):
            return "each checklist item must be an object with string 'text' and boolean 'done'"
    return None


# E2 Inc 4: the project kinds the relay accepts (a checklist push may declare one). Kept
# as a relay-local constant — relay/ shares no code with orion/ (see store.py's header), so
# the wire's valid set is restated here, the same way _BUSY_TIMEOUT_SECONDS is duplicated.
_VALID_PROJECT_KINDS = ("project", "tracker")


def _validate_checklist_request(payload: object) -> str | None:
    """Check a parsed payload against the checklist-push contract (POST /checklist).

    Args:
        payload: The JSON-parsed request body (any type — untrusted input).

    Returns:
        None when the payload is a valid `{project, checklist[, kind]}` request; otherwise
        a short human-readable 400 reason.

    Why:
        The checklist-only push is an untrusted inbound surface like /ingest, so it
        validates shape BEFORE storing. Unlike the blob's optional checklist, here the
        checklist is REQUIRED (the request exists to set it), and `project` must be a
        non-empty string (it is the upsert key). The item shape reuses the shared helper.
        `kind` is OPTIONAL (a producer predating the flag omits it ⇒ the relay defaults to
        "project"), so it is validated only WHEN PRESENT, to a known value.
    """
    if not isinstance(payload, dict):
        return "payload must be a JSON object"
    project = payload.get("project")
    if not isinstance(project, str) or not project.strip():
        return "field 'project' must be a non-empty string"
    items_error = _checklist_items_error(payload.get("checklist"))
    if items_error is not None:
        return items_error
    kind = payload.get("kind")
    if kind is not None and kind not in _VALID_PROJECT_KINDS:
        return f"field 'kind' must be one of {_VALID_PROJECT_KINDS}"
    return None


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

    # checklist is OPTIONAL (E2 Inc 2): a producer without the feature omits it
    # entirely, exactly like the legacy source_marker field is not required. So we only
    # validate it WHEN PRESENT, via the shared item-shape helper. A malformed checklist
    # is a clean 400 here, never a later crash in store/render.
    checklist = payload.get("checklist")
    if checklist is not None:
        items_error = _checklist_items_error(checklist)
        if items_error is not None:
            return items_error

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


def _parse_api_comment_path(path: str) -> int | None:
    """Extract the report id from "/api/reports/<id>/comments", or None.

    Args:
        path: The request path, already stripped of any query string.

    Returns:
        The integer report id when `path` is exactly "/api/reports/<digits>/comments";
        otherwise None (the route did not match).

    Why:
        The SPA's JSON comment write is RESTfully scoped to a report. do_POST routes by
        path, so it needs a yes/no-with-id matcher with an all-digit guard: a non-numeric
        id simply fails to match and falls through to the 404 — no exception, no store
        touch. The GET /api/reports/<id> read uses the same id segment, so write and read
        agree on the identity.
    """
    prefix, suffix = "/api/reports/", "/comments"
    if not (path.startswith(prefix) and path.endswith(suffix)):
        return None
    middle = path[len(prefix):-len(suffix)]
    return int(middle) if middle.isdigit() else None


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
        """Route a GET request.

        Three GET surfaces remain after the server-rendered views retired (KI-23):
          1. The machine-JSON API ("/api/comments", "/api/users") — Bearer/admin-token
             authed, routed FIRST so a browser never reaches it and the CLI never trips
             the session gate.
          2. The SPA's read-only JSON API ("/api/me", "/api/portfolio",
             "/api/projects/<name>", "/api/reports/<id>", "/api/scheduling",
             "/api/showcase") — cookie-session authed (a 401 routes the SPA to /login),
             except /api/me + /api/showcase which answer publicly.
          3. The built SPA itself, when --web-dir is set: a real asset if the path maps to
             one, else index.html for client-side routing; "/logout" clears the cookie.
        Without --web-dir the relay is API-only (headless): only "/logout" and the JSON
        routes answer; any other path is a JSON 404.

        Read auth (multi-party): the SPA-API routes share the dashboard's gate — when
        access-gated (a view secret is set OR any user is provisioned) a valid SESSION
        COOKIE is required; on a bare loopback dev relay with neither, reads stay open. The
        fail-closed guard in create_server() guarantees a view secret on any non-loopback
        bind. AuthZ scoping layers on top: an admin (and the open relay) sees everything; a
        viewer is restricted to projects_for_user, and out-of-scope projects/reports return
        404 (existence-hiding, decision A), re-resolved from DB state on every request.
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

        # E2 Inc 4: the SPA's read-only JSON API. Unlike /api/comments + /api/users (machine
        # surfaces authed by a Bearer/admin token), these are consumed by the browser SPA and
        # use the SAME cookie session + scope the dashboard routes do — but answer with JSON
        # (and a 401 instead of a 303 redirect, so the SPA's fetch routes itself to /login).
        if (
            path in ("/api/me", "/api/portfolio", "/api/scheduling", "/api/showcase")
            or path.startswith("/api/projects/")
            or path.startswith("/api/reports/")
        ):
            self._handle_spa_api(path)
            return

        # E2 Inc 4 (KI-23): the React SPA is the dashboard's only front-end. When started
        # with --web-dir the relay serves the built SPA single-host — the SPA owns every
        # page (/, /project/*, /report/*, /login) via client-side routing. So serve a real
        # asset if the path maps to one, else fall back to index.html. The data is still
        # gated (the /api routes 401); the index shell itself is public (the SPA then calls
        # /api/me and routes to /login). /logout stays a real action (clear the cookie,
        # then the SPA reloads). The legacy server-rendered HTML views retired here.
        if self.server.web_dir is not None:
            if path == "/logout":
                self._handle_logout()
                return
            if self._serve_static(path):
                return
            self._serve_spa_index()
            return

        # No --web-dir: this relay is API-ONLY (headless). There is no HTML front-end to
        # serve, so /logout (a harmless cookie clear) is the only browser GET honoured;
        # every other path is a JSON 404. A real browser dashboard requires --web-dir.
        if path == "/logout":
            self._handle_logout()
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        """Route a POST to one of the relay's write surfaces.

        Routes: "/ingest" (the Bearer-authed report push), "/api/comments" (a
        Bearer-authed MACHINE comment write — the native bot's path, C2-bots),
        "/api/reports/<id>/comments" (the SPA's cookie-authed BROWSER comment write),
        and the JSON auth siblings "/api/login" / "/api/logout". Anything else is a 404.
        Routing is by path only, so any query string is stripped first — mirroring do_GET.

        Why a router: do_POST used to be the single ingest handler. C2 added a second,
        differently-authed write path (a CSRF check + cookie session), and the bots slice
        added a third — a machine sibling of GET /api/comments that takes Bearer (like
        /ingest). Each path gets its own handler, keeping this method a short, readable
        table of routes rather than one branching blob.

        Note "/api/comments" is shared between do_GET (the pull-back read) and do_POST
        (the bot's write): the HTTP method already disambiguates them, so the same
        Bearer-gated machine path serves both directions. (The legacy form routes
        POST /login and POST /report/<id>/comment retired with render.py — KI-23.)
        """
        path = urllib.parse.urlparse(self.path).path

        if path == "/ingest":
            self._handle_ingest()
            return

        if path == "/checklist":
            self._handle_checklist_push()
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

        # E2 Inc 4: the SPA's JSON auth siblings (the cookie-session login / logout).
        if path == "/api/login":
            self._handle_api_login()
            return
        if path == "/api/logout":
            self._handle_api_logout()
            return

        # E2 Inc 4: the SPA's cookie-authed JSON comment write — the only user-authored
        # write path. Distinct from the Bearer-authed machine /api/comments above.
        api_comment_report_id = _parse_api_comment_path(path)
        if api_comment_report_id is not None:
            self._handle_api_report_comment(api_comment_report_id)
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
            received_at = _utc_now_iso()
            new_id = ingest(conn, payload, received_at)
            # E2 Inc 2: if the push carries a live checklist, REPLACE this project's
            # current checklist (upsert). Stamped with the same receive clock as the
            # report. Absent ⇒ leave any existing checklist untouched (an old producer
            # or the feature off); present-but-empty ⇒ clears it (a real "no items now"
            # state). Validated above, so the items are the expected {text, done} shape.
            checklist = payload.get("checklist")
            if checklist is not None:
                upsert_checklist(conn, payload["project"], checklist, received_at)
                # E2 Inc 3: also APPEND each item to the observed-state history (the
                # forward-store's "remember"), sharing the report's receive clock.
                record_observations(conn, payload["project"], checklist, received_at)
        finally:
            conn.close()
        print(
            f"[relay] ingested report {new_id} for project {payload['project']!r}",
            file=sys.stderr,
        )
        self._send_json(201, {"id": new_id})

    def _handle_checklist_push(self) -> None:
        """Authenticate, validate, and upsert a project's live checklist (POST /checklist).

        Why:
            The dedicated checklist-only push: it sets a project's current checklist on
            the dashboard WITHOUT creating a report, so an edit to the local tasks_file
            can reach the dashboard in near-real-time (driven by `orion checklist-push
            --watch`). It reuses the ingest Bearer token (same machine-to-machine push
            credential) and the SAME upsert the report path calls — it simply skips the
            report row. The flow mirrors _handle_ingest's guard sequence; the payload is
            the smaller `{project, checklist}` instead of a full blob.
        """
        # 1) Authenticate first (same Bearer token as /ingest) — nothing about the
        # payload is touched until the caller is authorized.
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

        # 3) Validate the {project, checklist} shape BEFORE storing.
        error = _validate_checklist_request(payload)
        if error is not None:
            self._send_json(400, {"error": error})
            return

        # 4) Upsert this project's live checklist, stamped with the relay's receive
        # clock (same provenance meaning as a report's ingested_at). One fresh
        # connection per request keeps each sqlite handle on its own thread.
        conn = open_relay_store(self.server.db_path)
        try:
            # One receive clock shared by the live-state upsert and its history append, so
            # the current checklist and its newest observation never disagree by a second.
            received_at = _utc_now_iso()
            upsert_checklist(
                conn, payload["project"], payload["checklist"], received_at
            )
            # E2 Inc 3: append each item to the observed-state history (forward-store).
            record_observations(
                conn, payload["project"], payload["checklist"], received_at
            )
            # E2 Inc 4: record the project's kind (project | tracker) so the home can split
            # projects from trackers. Optional on the wire — absent ⇒ "project" (the safe
            # default, and what a producer predating the flag means). Its own meta row.
            set_project_kind(conn, payload["project"], payload.get("kind") or "project")
        finally:
            conn.close()
        count = len(payload["checklist"])
        print(
            f"[relay] updated checklist for project {payload['project']!r} "
            f"({count} item(s))",
            file=sys.stderr,
        )
        # 200 (not 201): an upsert of existing per-project state, not a created resource.
        self._send_json(200, {"updated": payload["project"], "items": count})

    def _handle_api_report_comment(self, report_id: int) -> None:
        """Store one comment from the SPA on a report (POST /api/reports/<id>/comments).

        Args:
            report_id: The report the comment attaches to (parsed from the path).

        Why:
            The only user-authored write surface: the SPA posts a comment via fetch and
            appends the returned comment to the thread. JSON-in / JSON-out, cookie-session
            authed with a CSRF (Origin) check, scope-filtered, and attributed to the
            authenticated identity. The machine path POST /api/comments stays Bearer-authed
            and separate (a browser never auto-sends a Bearer token, so it needs no CSRF
            check; this cookie path does). Every guard reuses the vetted auth/scope/origin
            helpers. (This absorbed the retired browser form route's role — KI-23.)

            Inbound-security checklist, enforced IN ORDER:
              1. Auth — a valid session cookie when the dashboard is gated; 401 otherwise.
              2. CSRF — the cookie is auto-sent by the browser, so a forged cross-site POST
                 is the threat; require a matching Origin/Referer (_origin_error), else 403.
              3. Validate — JSON {body, author?}; a non-empty body within the caps; else 400.
              4. Report exists AND is in scope — else 404, identical to a missing report
                 (existence-hiding), so a viewer cannot probe or write outside their grants.
              5. Store — attribute to the authenticated identity (NEVER the client-supplied
                 author, so a logged-in user cannot post as someone else); open loopback has
                 no session, so the typed name stands. Return 201 with the created comment in
                 the read-path shape (api._comment) so the SPA appends it without a refetch.
        """
        conn = open_relay_store(self.server.db_path)
        try:
            # 1) Auth: a valid session whenever the dashboard is access-gated (same as reads).
            gated = self._auth_required(conn)
            principal = self._authenticate(conn) if gated else None
            if gated and principal is None:
                self._send_json(401, {"error": "login required"})
                return

            # 2) CSRF: require a same-origin POST (the cookie is the forgeable credential).
            if self._origin_error() is not None:
                self._send_json(403, {"error": "origin check failed"})
                return

            # 3) Read + JSON-parse + validate the body (1 MB cap inside _read_raw_body).
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
            body = payload.get("body")
            client_author = payload.get("author", "")
            if not isinstance(body, str):
                self._send_json(400, {"error": "field 'body' must be a string"})
                return
            if not isinstance(client_author, str):
                self._send_json(400, {"error": "field 'author' must be a string"})
                return
            # Strip both so a whitespace-only body counts as empty.
            body = body.strip()
            client_author = client_author.strip()
            if not body:
                self._send_json(400, {"error": "a comment body is required"})
                return
            if len(body) > MAX_COMMENT_BODY_CHARS or len(client_author) > MAX_AUTHOR_CHARS:
                self._send_json(400, {"error": "comment or author is too long"})
                return

            # 4) Report exists AND is in this principal's scope, else 404 (existence-hiding,
            # identical to a nonexistent report) — the same rule do_GET applies to reads.
            allowed = self._allowed_projects(conn, principal)
            report = get(conn, report_id)
            if report is None or (
                allowed is not None and report["project"] not in allowed
            ):
                self._send_json(404, {"error": "not found"})
                return

            # 5) Attribute to the authenticated identity (logged in) or the typed name (open
            # loopback). The principal name is trusted (from the DB), so not re-length-capped.
            author = principal["name"] if principal is not None else client_author
            created_at = _utc_now_iso()
            new_id = add_comment(conn, report_id, author, body, created_at)
        finally:
            conn.close()
        # Return the created comment in the SAME shape the read path emits (api._comment):
        # role is null in 4a (contract gap 7). The SPA appends this to the thread directly.
        self._send_json(
            201,
            {
                "id": new_id,
                "author": author,
                "role": None,
                "body": body,
                "created_at": created_at,
            },
        )

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
        the project's report_comments — the EXACT same store the SPA comment write
        (`_handle_api_report_comment`) and the dashboard read from. So a chat reply and a
        dashboard comment are indistinguishable downstream, and `orion comments` keeps
        working unchanged.

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
             browser `_handle_api_report_comment`): a Bearer token is never auto-attached by
             a browser, so there is no cross-site-forgery vector — the same reasoning GET
             /api/comments uses to skip it.
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
            `_handle_api_report_comment` and `_handle_api_comments`: redaction is an OUTBOUND
            control for the developer's own secrets, whereas an inbound supervisor comment
            shown only on the access-gated dashboard is a different threat (the control there
            is the SPA's inert React text rendering, never dangerouslySetInnerHTML). This
            handler stays Bearer-gated all the same.
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

    def _resolve_login(self, conn, key: str) -> tuple | None:
        """Verify an access key and resolve it to (sub, sv, identity), or None on a miss.

        Args:
            conn: An open relay-store connection.
            key: The posted access key (free text — untrusted).

        Returns:
            (sub, sv, identity) on success — `sub`/`sv` the session cookie's subject +
            version, `identity` = {"name", "role"} — or None when the key matches no active
            user (and is not the still-permitted legacy admin key). Updates last_login on a
            real-user success.

        Why:
            The JSON login (POST /api/login) needs verify-and-mint logic; factoring it here
            keeps it testable and ready to share (the form login that used to share it
            retired with render.py — KI-23). All key compares stay constant-time
            (key_verifier / compare_digest) and the key is never logged. A generic None on
            any miss keeps the caller from leaking which part failed. Returns identity too,
            so the JSON caller can echo {name, role} without a second lookup.
        """
        if not key or self.server.session_key is None:
            return None
        if self.server.user_pepper is not None:
            user = get_user_by_verifier(conn, key_verifier(self.server.user_pepper, key))
            if user is not None and user["active"]:
                update_last_login(conn, user["id"], _utc_now_iso())
                return user["id"], user["session_version"], {
                    "name": user["name"],
                    "role": user["role"],
                }
        if (
            self.server.view_token
            and self._legacy_admin_allowed(conn)
            and hmac.compare_digest(key, self.server.view_token)
        ):
            # Legacy bootstrap admin: reserved sentinel id/version, synthetic identity.
            return 0, 0, {"name": "legacy-admin", "role": "admin"}
        return None

    def _mint_cookie(self, sub: int, sv: int) -> str:
        """Build the Set-Cookie header value for a freshly-minted session.

        Args:
            sub: The session subject (a user id, or 0 for the legacy admin).
            sv: The session_version the cookie is bound to (for stateless revocation).

        Returns:
            The Set-Cookie header value carrying the signed session, valid for the
            configured session lifetime.

        Why:
            The JSON login mints the session cookie through this one builder, which keeps the
            signing, lifetime, and cookie flags (HttpOnly/SameSite/Secure) in a single place
            (a security-relevant invariant). Factored out while the form login also used it;
            that route has since retired with render.py (KI-23).
        """
        now = int(time.time())
        value = make_session_value(
            self.server.session_key, sub, sv, now, now + self.server.session_seconds
        )
        return self._cookie_header(value, self.server.session_seconds)

    def _handle_api_login(self) -> None:
        """Verify a JSON-posted access key and set the session cookie (POST /api/login).

        Why:
            The SPA's login: it POSTs {"key": ...} and needs to tell success from failure
            without parsing a redirect, plus get {name, role} back to populate the account
            card. It reuses the factored-out _resolve_login + _mint_cookie helpers and
            applies the same same-origin CSRF check as the comment POST — the SPA fetch is
            same-origin (single host in prod, Vite proxy in dev), so it passes.
        """
        # CSRF: this sets a cookie, so guard it like the other state-changing POSTs.
        origin_error = self._origin_error()
        if origin_error is not None:
            self._send_json(403, {"ok": False, "error": origin_error})
            return
        if self.server.session_key is None:
            self._send_json(404, {"ok": False, "error": "login is not configured"})
            return
        raw = self._read_raw_body()
        key = ""
        if raw is not None:
            try:
                parsed = json.loads(raw.decode("utf-8"))
                key = parsed.get("key", "") if isinstance(parsed, dict) else ""
            except (ValueError, UnicodeDecodeError):
                key = ""
        conn = open_relay_store(self.server.db_path)
        try:
            resolved = self._resolve_login(conn, key if isinstance(key, str) else "")
            if resolved is None:
                self._send_json(401, {"ok": False})
                return
            sub, sv, identity = resolved
            self._send_json(
                200,
                {"ok": True, "user": identity},
                extra_headers={"Set-Cookie": self._mint_cookie(sub, sv)},
            )
        finally:
            conn.close()

    def _handle_api_logout(self) -> None:
        """Clear the session cookie via JSON (POST /api/logout).

        Why:
            The SPA's logout sibling of _handle_logout (the form GET). Same-origin CSRF
            check (a forged logout is a mild annoyance worth blocking), then it clears the
            cookie and returns JSON the SPA can act on without a navigation.
        """
        origin_error = self._origin_error()
        if origin_error is not None:
            self._send_json(403, {"ok": False, "error": origin_error})
            return
        self._send_json(
            200, {"ok": True}, extra_headers={"Set-Cookie": self._cookie_header("", 0)}
        )

    def _handle_logout(self) -> None:
        """Clear the session cookie and redirect to /login (GET /logout)."""
        self._send_redirect(
            303, "/login", extra_headers={"Set-Cookie": self._cookie_header("", 0)}
        )

    def _handle_spa_api(self, path: str) -> None:
        """Serve the SPA's read-only JSON API (/api/me|portfolio|projects/*|reports/*).

        Args:
            path: The request path (already stripped of its query string).

        Why:
            One handler shares the cookie gate + scope resolution across the four read
            endpoints, exactly as the dashboard GET routes do — but answers JSON and a 401
            (not a 303) when a gated relay has no valid session, so the SPA's fetch can route
            itself to /login. Scope is enforced identically to the HTML routes: a viewer's
            out-of-scope project/report returns a 404 byte-identical to a missing one
            (existence-hiding). All derivation runs against "today" in the display zone, the
            same day every viewer sees.
        """
        conn = open_relay_store(self.server.db_path)
        try:
            gated = self._auth_required(conn)
            principal = self._authenticate(conn) if gated else None
            allowed = self._allowed_projects(conn, principal)
            today = today_in_tz(self.server.display_tz)

            # /api/me is the ONE read that must answer even when unauthenticated — it is
            # how the SPA LEARNS it must log in (gated + authenticated:false). So it is
            # exempt from the 401 gate below and always returns 200 with the current state.
            if path == "/api/me":
                self._send_json(
                    200,
                    api.serialize_me(
                        gated=gated,
                        principal=principal,
                        allowed=allowed,
                        display_tz=self.server.display_tz,
                        showcase_enabled=self.server.showcase_enabled,
                    ),
                )
                return

            # /api/showcase is the public, no-login surface: like /api/me it is exempt from
            # the 401 gate (a guest has no session). It 404s when the Showcase is disabled
            # (existence-hiding), and otherwise serves ONLY the curated allowlist, in
            # allowlist order, independent of any viewer scope.
            if path == "/api/showcase":
                if not self.server.showcase_enabled:
                    self._send_json(404, {"error": "not found"})
                    return
                rows_by_name = {
                    r["project"]: r
                    for r in latest_report_per_project(conn, today=today)
                }
                # Walk the allowlist (not the store) so the order is the operator's and a
                # listed-but-absent project is simply skipped rather than erroring.
                entries = [
                    {**rows_by_name[name], "blurb": blurb}
                    for name, blurb in self.server.showcase_projects
                    if name in rows_by_name
                ]
                self._send_json(200, api.serialize_showcase(entries))
                return

            # Every OTHER read requires a session on a gated relay: answer 401 JSON (not a
            # 303) so the SPA's fetch can route itself to /login.
            if gated and principal is None:
                self._send_json(401, {"error": "login required"})
                return

            if path == "/api/portfolio":
                rows = latest_report_per_project(conn, today=today)
                if allowed is not None:
                    rows = [r for r in rows if r["project"] in allowed]
                # Enrich each row with its checklist items, which the deadline / segmented-bar
                # / chip derivations need (the row carries counts, not the items themselves).
                entries = [
                    {**r, "items": get_checklist(conn, r["project"])} for r in rows
                ]
                self._send_json(200, api.serialize_portfolio(entries, allowed, today))
                return

            if path.startswith("/api/projects/"):
                name = urllib.parse.unquote(path[len("/api/projects/"):])
                if allowed is not None and name not in allowed:
                    self._send_json(404, {"error": "not found"})
                    return
                reports = history(conn, name)
                checklist = get_checklist(conn, name)
                # A project with neither reports nor a checklist does not exist; 404 it
                # (existence-hiding, same as an out-of-scope one above).
                if not reports and checklist is None:
                    self._send_json(404, {"error": "not found"})
                    return
                self._send_json(
                    200,
                    api.serialize_project(
                        name=name,
                        kind=get_project_kind(conn, name),
                        reports=reports,
                        checklist=checklist,
                        observations=observed_history(conn, name),
                        comments=comments_for_project(conn, name),
                        today=today,
                    ),
                )
                return

            if path.startswith("/api/reports/"):
                id_str = path[len("/api/reports/"):]
                report = get(conn, int(id_str)) if id_str.isdigit() else None
                if report is None or (
                    allowed is not None and report["project"] not in allowed
                ):
                    self._send_json(404, {"error": "not found"})
                    return
                self._send_json(
                    200,
                    api.serialize_report(
                        report=report,
                        checklist=get_checklist(conn, report["project"]),
                        comments=comments_for(conn, report["id"]),
                        history=history(conn, report["project"]),
                        today=today,
                    ),
                )
                return

            if path == "/api/scheduling":
                # Cross-project deadline view: enumerate every project (latest_report_per_project
                # carries kind + includes checklist-only trackers), scope-filter, and hand each
                # project's checklist + observation history to the serializer for time-bucketing.
                rows = latest_report_per_project(conn, today=today)
                if allowed is not None:
                    rows = [r for r in rows if r["project"] in allowed]
                projects = [
                    {
                        "name": r["project"],
                        "kind": r["kind"],
                        "items": get_checklist(conn, r["project"]),
                        "observations": observed_history(conn, r["project"]),
                    }
                    for r in rows
                ]
                self._send_json(200, api.serialize_scheduling(projects, today))
                return

            # No SPA route matched (the do_GET prefix check is broader than the exact set).
            self._send_json(404, {"error": "not found"})
        finally:
            conn.close()

    def _origin_error(self) -> str | None:
        """Return None if the request is same-origin, else a 403 reason string.

        Returns:
            None when the request's origin — taken from the Origin header, or the
            Referer header's origin when Origin is absent — matches the expected origin;
            otherwise a short reason string.

        Why:
            The comment POST is authenticated by the session cookie, which the browser
            AUTO-SENDS on every request to this origin — including one a malicious
            third-party page triggers (classic CSRF). Verifying the request's origin is
            the primary defense (SameSite=Lax on the cookie is a second layer).

            We check the Origin header FIRST, then fall back to the Referer header's
            origin. The fallback covers a request whose Origin is absent OR the literal
            "null" (an opaque origin — e.g. a non-GET request under a strict referrer
            policy serializes Origin as "null"), so requiring a usable Origin alone
            rejected legitimate comments with a "CSRF" 403. OWASP's CSRF guidance is
            explicitly "verify Origin OR Referer": a cross-site attacker cannot forge a
            Referer that points at our origin (the browser sets it from the real page
            URL), and if BOTH headers are absent we still fail closed. The companion to
            this is the response Referrer-Policy: it MUST keep a same-origin Referer
            (see _security_headers — "same-origin", not "no-referrer"), or this fallback
            has nothing to read.

            When a canonical public origin is configured (the deployed HTTPS URL), we
            require an EXACT match — scheme+host+port — rather than trusting the Host
            header, which a client can set and a proxy rewrites (Codex's
            don't-blind-trust-Host point). Set ORION_RELAY_PUBLIC_ORIGIN on any deploy
            behind a TLS proxy (e.g. Fly) so this check is deterministic. On a loopback
            dev relay with none configured, we fall back to matching the origin's netloc
            against the Host header.
        """
        # Prefer the Origin header. When it is absent — OR the literal "null", which is
        # how an OPAQUE origin serializes (a sandboxed context, or a non-GET request
        # under a strict referrer policy) — fall back to the Referer's origin: reduce the
        # Referer (a full URL with a path) to scheme://netloc so both paths below compare
        # the same shape. "null" can never equal our canonical origin, so treating it as a
        # candidate would needlessly 403 a possibly-legitimate same-origin POST; the
        # Referer (present for a same-origin request under our "same-origin" policy) is the
        # correct thing to check instead. Both absent ⇒ fail closed.
        candidate = self.headers.get("Origin")
        if not candidate or candidate == "null":
            referer = self.headers.get("Referer")
            if not referer:
                return "missing or opaque Origin and no Referer"
            parsed = urllib.parse.urlparse(referer)
            if not parsed.scheme or not parsed.netloc:
                return "malformed Referer header"
            candidate = f"{parsed.scheme}://{parsed.netloc}"

        if self.server.public_origin:
            # Exact canonical-origin match (trailing slash ignored). Nothing about the
            # request (Host, proxy headers) is trusted — only the configured value.
            if candidate.rstrip("/") != self.server.public_origin.rstrip("/"):
                return "origin does not match the configured public origin"
            return None
        if urllib.parse.urlparse(candidate).netloc != self.headers.get("Host", ""):
            return "origin does not match Host"
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

    def _security_headers(self) -> dict:
        """Return the standard security response headers common to every response.

        Returns:
            A header-name -> value dict for the response writer to merge in.

        Why:
            Defense-in-depth on an internet-facing surface, defined ONCE so all response
            writers send a consistent set (DRY). X-Content-Type-Options and Referrer-Policy
            ride EVERY response. HSTS is gated on secure_cookie — the same hosted-HTTPS
            signal the cookie's Secure attribute uses — so a plain HTTP loopback dev relay
            never claims HTTPS-only (which would wedge it in a browser). The SPA's CSP +
            X-Frame-Options are NOT here: only the SPA index.html (an HTML document a
            browser renders/frames) needs them, and _send_file attaches them itself — they
            are meaningless on a JSON API reply or a bodyless redirect.

            Referrer-Policy is "same-origin", NOT "no-referrer": "no-referrer" is what
            broke the comment loop. Per the Fetch standard, a non-GET request under a
            "no-referrer" policy is sent with `Origin: null` (and no Referer), so EVERY
            browser comment POST arrived as Origin: null — which the canonical-origin
            CSRF check (_origin_error) rejects with a 403, and the Referer fallback could
            not rescue because the same policy strips Referer too. "same-origin" keeps
            the privacy intent (nothing leaks to OTHER origins) while letting a
            same-origin POST carry its real Origin (and a same-origin Referer), which is
            exactly what the CSRF check needs. Verified with real Chromium: no-referrer ->
            Origin: null; same-origin -> the real Origin.
        """
        headers = {
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "same-origin",
        }
        # HSTS only in a hosted HTTPS posture: a plain-http loopback dev relay must not
        # send an HTTPS-only directive (the browser would then refuse plain http to it).
        # 2 years, includeSubDomains — the standard durable HSTS lifetime.
        if self.server.secure_cookie:
            headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
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
        merged = {**self._security_headers(), **(extra_headers or {})}
        for name, value in merged.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_redirect(
        self, code: int, location: str, *, extra_headers: dict | None = None
    ) -> None:
        """Write a bodyless redirect response to `location`.

        Args:
            code: The 3xx status code (303).
            location: The path to redirect to (a site-relative path is fine).
            extra_headers: Optional extra response headers — used to attach the
                cookie-clearing Set-Cookie on the GET /logout redirect.

        Why:
            GET /logout clears the session cookie and 303-redirects to /login; this is its
            sole remaining caller now that the form login + comment POST routes retired with
            render.py (the SPA's JSON auth/comment paths use _send_json). It also keeps the
            redirect header sequence in one place — same shape as _send_json. A redirect
            needs no body, so we send an explicit zero Content-Length plus the Location.
        """
        self.send_response(code)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        # A redirect carries no rendered body, so it gets the common headers only.
        # Caller extras (e.g. the login/logout Set-Cookie) take precedence on a clash.
        merged = {**self._security_headers(), **(extra_headers or {})}
        for name, value in merged.items():
            self.send_header(name, value)
        self.end_headers()

    # --- E2 Inc 4: serving the built SPA single-host (when web_dir is set) -----------

    def _send_file(
        self, body: bytes, content_type: str, *, cache: str, spa_html: bool
    ) -> None:
        """Write a static-file response (an asset or the SPA index.html).

        Args:
            body: The file bytes.
            content_type: The resolved Content-Type.
            cache: The Cache-Control value (immutable for hashed assets; no-cache for
                index.html so a deploy is picked up).
            spa_html: True for the SPA index.html, which additionally carries the SPA CSP
                + X-Frame-Options (an HTML document a browser renders); False for assets.

        Why:
            One writer for both asset and index responses keeps the header sequence in one
            place. The SPA index gets its OWN CSP (script-src strict 'self') + the
            clickjacking X-Frame-Options here — the only HTML the relay serves now that the
            server-rendered views have retired (KI-23).
        """
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        headers = self._security_headers()
        if spa_html:
            headers["Content-Security-Policy"] = _SPA_CONTENT_SECURITY_POLICY
            headers["X-Frame-Options"] = "DENY"
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path: str) -> bool:
        """Serve a built asset under web_dir, if the path maps to a real file. Return served?

        Args:
            path: The request path (already stripped of its query string).

        Returns:
            True if a file was found and sent; False if no such file (so the caller falls
            back to the SPA index for client-side routing).

        Why:
            Path-traversal safety is the whole job here: we percent-decode, reject NULs,
            resolve the candidate, and confirm it is STILL inside web_dir (is_relative_to)
            before reading — so "../" or a symlink cannot escape the asset root. Hashed
            assets under /assets/ are immutable (content-hashed filenames), so they get a
            long immutable cache; anything else served as a file gets no-cache.
        """
        root = self.server.web_dir
        if root is None:
            return False
        rel = urllib.parse.unquote(path).lstrip("/")
        if "\x00" in rel or rel == "":
            return False
        candidate = (root / rel).resolve()
        # Containment check: the resolved path must remain under the asset root.
        if not candidate.is_relative_to(root) or not candidate.is_file():
            return False
        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        # Content-hashed assets never change under their name → cache hard; else no-cache.
        cache = (
            "public, max-age=31536000, immutable"
            if path.startswith("/assets/")
            else "no-cache"
        )
        self._send_file(body, content_type, cache=cache, spa_html=False)
        return True

    def _serve_spa_index(self) -> None:
        """Serve the SPA's index.html (the client-side-routing fallback).

        Why:
            Any GET that is not an API route and not a real asset file is a client-side
            route (/, /project/orion, /report/26, /login, …), so the server returns
            index.html and lets the React router render it. no-cache so a new deploy's
            index (which references the new hashed asset names) is always fetched fresh.
            A 404 here only happens if web_dir has no index.html (a broken build/deploy).
        """
        index = self.server.web_dir / "index.html"
        if not index.is_file():
            self._send_json(404, {"error": "SPA index.html not found"})
            return
        self._send_file(
            index.read_bytes(), "text/html; charset=utf-8", cache="no-cache", spa_html=True
        )


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


@dataclass(frozen=True)
class ShowcaseConfig:
    """The relay's public, no-login Showcase configuration (E2 Inc 4, slice 4a).

    Args:
        enabled: Master opt-in switch. Off by default — the public surface exists only
            when an operator explicitly turns it on (default-deny). While off,
            GET /api/showcase 404s and the SPA hides the "Public showcase" link.
        projects: The curated allowlist as ordered (name, blurb) pairs. ONLY these
            projects appear publicly, in this order; `blurb` is the curated one-line
            description ("" → fall back to the observed headline). Insertion order is the
            display order.

    Why:
        A public, no-login surface is a genuine privacy boundary (Orion's first rule:
        never leak), so curation is an EXPLICIT allowlist, not "publish everything when
        on." Bundling the two knobs into one frozen object keeps the already-long
        RelayServer / create_server / serve signatures readable — the same pattern
        AuthConfig uses. These are project NAMES + operator-authored blurbs, not secrets,
        so they ride on CLI flags (the relay never reads orion.toml), unlike the auth
        secrets which come from .env.
    """

    enabled: bool = False
    projects: tuple[tuple[str, str], ...] = ()


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
        display_tz: The IANA zone the dashboard renders timestamps in. Defaults to the
            module's _DISPLAY_TZ Pacific constant, so an operator who passes no --timezone
            gets the historical California output unchanged.

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
        web_dir: Path | None = None,
        showcase: "ShowcaseConfig | None" = None,
    ):
        # Set config before binding so it is available to any request handled after
        # serve_forever() starts.
        self.db_path = db_path
        self.token = token
        self.view_token = view_token
        self.display_tz = display_tz
        # E2 Inc 4: the public, no-login Showcase. None = disabled (no public surface).
        # Spread onto the server so each handler reads the knobs via self.server.<name>.
        showcase = showcase or ShowcaseConfig()
        self.showcase_enabled = showcase.enabled
        self.showcase_projects = showcase.projects
        # E2 Inc 4: when set, the built SPA (web/dist) is served from here single-host —
        # static assets + an index.html fallback for client-side routes. None = API-only
        # (headless): the JSON API works but there is no HTML front-end (the legacy
        # server-rendered views retired at parity, KI-23).
        self.web_dir = web_dir.resolve() if web_dir is not None else None
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
    web_dir: Path | None = None,
    showcase: "ShowcaseConfig | None" = None,
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
    return RelayServer(
        (host, port), db_path, token, view_token, display_tz, auth, web_dir, showcase
    )


def serve(
    host: str,
    port: int,
    db_path: Path,
    token: str,
    view_token: str | None = None,
    require_view_auth: bool = False,
    display_tz: ZoneInfo = _DISPLAY_TZ,
    auth: "AuthConfig | None" = None,
    web_dir: Path | None = None,
    showcase: "ShowcaseConfig | None" = None,
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
        host, port, db_path, token, view_token, require_view_auth, display_tz, auth,
        web_dir, showcase,
    )
    bound_host, bound_port = server.server_address
    # Surface read-auth state at startup — the operator's confirmation that a
    # world-reachable bind is gated (the fail-closed guard guarantees it is).
    auth_state = "login required" if view_token else "open (loopback only)"
    frontend = f"SPA from {web_dir}" if web_dir is not None else "API only (no SPA dir)"
    print(
        f"[relay] listening on http://{bound_host}:{bound_port}  "
        f"(db: {db_path}; dashboard: {auth_state}; frontend: {frontend})",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

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
import hmac
import ipaddress
import json
import sys
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .render import render_index, render_not_found, render_project, render_report
from .store import get, history, ingest, list_projects, open_relay_store

# Reject a Content-Length larger than this outright. A report blob is a few KB; 1 MB
# is far above any real payload but well below "read gigabytes into memory". Cheap
# defensive hygiene on an inbound surface — we do not trust a client-sent length.
_MAX_BLOB_BYTES = 1_000_000

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

# Minimal body for a 401 on a dashboard (GET) route. Browsers show their own
# Basic-auth login dialog on a 401 + WWW-Authenticate header and only render this
# body if the user cancels, so it stays deliberately tiny — and self-contained in
# server.py, leaving render.py (the dashboard view layer) untouched.
_UNAUTHORIZED_HTML = (
    "<!doctype html><html lang='en'><meta charset='utf-8'>"
    "<title>Orion — authentication required</title>"
    "<h1>401 — authentication required</h1>"
    "<p>This Orion dashboard requires a username and password.</p></html>"
)


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

        Read auth: when the server is configured with a view secret, every dashboard
        route requires HTTP Basic credentials (a browser-native login); without one —
        the loopback-only default — reads stay open for zero-friction local use. The
        fail-closed guard in create_server() guarantees a view secret IS set whenever
        the bind host is non-loopback, so a world-reachable dashboard is never
        unauthenticated. Ingest (the write surface) stays Bearer-authed independently.
        """
        # Read-auth gate: enforced only when a view secret is configured. A missing
        # or wrong credential is a 401 whose WWW-Authenticate header makes the browser
        # present its own login dialog.
        if self.server.view_token and self._view_auth_error() is not None:
            self._send_html(
                401,
                _UNAUTHORIZED_HTML,
                extra_headers={"WWW-Authenticate": 'Basic realm="Orion dashboard"'},
            )
            return

        # Strip any query string; routing is by path only.
        path = urllib.parse.urlparse(self.path).path
        # A fresh connection per request keeps each sqlite handle on its own thread.
        conn = open_relay_store(self.server.db_path)
        try:
            if path == "/":
                self._send_html(200, render_index(list_projects(conn)))
                return

            if path.startswith("/project/"):
                # Decode the percent-encoded name back to the stored project string.
                name = urllib.parse.unquote(path[len("/project/"):])
                self._send_html(200, render_project(name, history(conn, name)))
                return

            if path.startswith("/report/"):
                id_str = path[len("/report/"):]
                # Only a numeric id can match a row; anything else is a 404 without
                # touching the store.
                report = get(conn, int(id_str)) if id_str.isdigit() else None
                if report is None:
                    self._send_html(404, render_not_found(f"No report {id_str!r}."))
                    return
                self._send_html(200, render_report(report))
                return

            self._send_html(404, render_not_found(f"Unknown path {path!r}."))
        finally:
            conn.close()

    def do_POST(self) -> None:
        """Route a POST. The only POST route is /ingest."""
        if self.path != "/ingest":
            self._send_json(404, {"error": "not found"})
            return

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

    def _view_auth_error(self) -> str | None:
        """Return None if the request carries valid dashboard read credentials.

        Returns:
            None when valid HTTP Basic credentials are present; otherwise a short
            reason string (a missing/malformed header, undecodable credentials, or a
            wrong password). Only called when the server has a view secret set.

        Why:
            Read auth reuses the same single-shared-secret model as ingest, but over
            HTTP Basic so a browser shows a native login dialog. Basic credentials are
            "username:password"; with one shared secret there is no account to
            enumerate, so the username is accepted as-is and only the PASSWORD is
            checked — split on the FIRST ':' so a password may itself contain colons.
            The compare is constant-time (hmac.compare_digest, no timing side-channel),
            and the expected secret is never echoed, exactly as on the ingest path.
        """
        header = self.headers.get("Authorization", "")
        prefix = "Basic "
        if not header.startswith(prefix):
            return "missing or malformed Authorization header (expected Basic auth)"
        try:
            decoded = base64.b64decode(header[len(prefix):], validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return "malformed Basic credentials"
        # "username:password" — keep only the password (partition splits on the first
        # ':', so colons in the password survive). view_token is non-None here.
        _, _, password = decoded.partition(":")
        if not hmac.compare_digest(password, self.server.view_token):
            return "invalid credentials"
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
        for name, value in (extra_headers or {}).items():
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
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


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

    Why:
        Subclassing ThreadingHTTPServer to hold db_path/token is the clean way to
        make per-server config available to each request handler (handlers read it
        via self.server). ThreadingHTTPServer handles each request on its own
        (daemon) thread, so a slow request never blocks the next push.
    """

    # Inherited as True from ThreadingHTTPServer; stated for clarity so threads
    # never keep the process alive after shutdown.
    daemon_threads = True

    def __init__(
        self, server_address, db_path: Path, token: str, view_token: str | None = None
    ):
        # Set config before binding so it is available to any request handled after
        # serve_forever() starts.
        self.db_path = db_path
        self.token = token
        self.view_token = view_token
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
    host: str, port: int, db_path: Path, token: str, view_token: str | None = None
) -> RelayServer:
    """Build and bind a RelayServer (does not start serving).

    Args:
        host: Interface to bind (e.g. "127.0.0.1").
        port: Port to bind; 0 lets the OS choose a free one.
        db_path: Path to the relay's sqlite store.
        token: The shared Bearer ingest token.
        view_token: Optional shared secret for dashboard read auth. Required (this
            function raises without it) whenever `host` is non-loopback.

    Returns:
        A bound RelayServer. Its actual bound port is server.server_address[1]
        (useful when port 0 was requested).

    Raises:
        ValueError: when `host` is non-loopback and no view_token is given — the
            fail-closed guard. Binding a world-reachable interface would otherwise
            expose the read-only dashboard to anyone, so we refuse to start rather
            than serve it unauthenticated.

    Why:
        Separating "build/bind" from "serve forever" makes the server testable: a
        test binds to port 0, reads the assigned port, drives requests on a thread,
        then calls shutdown() — none of which is possible if construction blocked.
        The guard lives HERE (not only in the CLI) so every path to a bound server —
        serve() and tests alike — is fail-closed by construction.
    """
    if not _is_loopback(host) and not view_token:
        raise ValueError(
            f"refusing to bind non-loopback host {host!r} without a dashboard view "
            "secret: the read-only dashboard would be world-readable. Set a view "
            "secret (relay-serve --view-token-env) before binding beyond loopback."
        )
    return RelayServer((host, port), db_path, token, view_token)


def serve(
    host: str, port: int, db_path: Path, token: str, view_token: str | None = None
) -> None:
    """Run the relay server until interrupted (the blocking entry point).

    Args:
        host: Interface to bind.
        port: Port to bind.
        db_path: Path to the relay's sqlite store.
        token: The shared Bearer ingest token.
        view_token: Optional dashboard read secret (HTTP Basic). Required by
            create_server() when host is non-loopback.

    Returns:
        None. Blocks serving requests until Ctrl-C, then shuts down cleanly.

    Why:
        This is what `orion relay-serve` calls. It prints the bound address and
        whether the dashboard requires read auth, so the user can see at a glance
        that a non-loopback bind is protected; it handles Ctrl-C as a clean stop
        rather than a traceback.
    """
    server = create_server(host, port, db_path, token, view_token)
    bound_host, bound_port = server.server_address
    # Surface read-auth state at startup — the operator's confirmation that a
    # world-reachable bind is gated (the fail-closed guard guarantees it is).
    auth_state = "Basic-auth required" if view_token else "open (loopback only)"
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

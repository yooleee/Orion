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

import hmac
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

        Why no auth here (yet): the dashboard's access boundary at this stage is
        LOOPBACK binding — relay-serve binds 127.0.0.1 by default, so the reads are
        not world-reachable. Real read authentication arrives with the deferred
        hosting decision (it shapes how the dashboard is exposed); building it now
        would pre-empt that call. Ingest stays Bearer-authed regardless — it is the
        write surface.
        """
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

    def _send_html(self, code: int, html_str: str) -> None:
        """Write an HTML response with the given status code.

        Why:
            The dashboard responses are all UTF-8 HTML; this keeps the
            status/header/body sequence and the charset in one place (DRY), so each
            view function just returns a string and never deals with the socket.
        """
        body = html_str.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class RelayServer(ThreadingHTTPServer):
    """A threaded HTTP server carrying the relay's db path and auth token.

    Args:
        server_address: The (host, port) to bind. Port 0 lets the OS pick a free
            port (used by tests).
        db_path: Path to the relay's sqlite store.
        token: The shared Bearer token ingest is authenticated against.

    Why:
        Subclassing ThreadingHTTPServer to hold db_path/token is the clean way to
        make per-server config available to each request handler (handlers read it
        via self.server). ThreadingHTTPServer handles each request on its own
        (daemon) thread, so a slow request never blocks the next push.
    """

    # Inherited as True from ThreadingHTTPServer; stated for clarity so threads
    # never keep the process alive after shutdown.
    daemon_threads = True

    def __init__(self, server_address, db_path: Path, token: str):
        # Set config before binding so it is available to any request handled after
        # serve_forever() starts.
        self.db_path = db_path
        self.token = token
        super().__init__(server_address, _RelayHandler)


def create_server(host: str, port: int, db_path: Path, token: str) -> RelayServer:
    """Build and bind a RelayServer (does not start serving).

    Args:
        host: Interface to bind (e.g. "127.0.0.1").
        port: Port to bind; 0 lets the OS choose a free one.
        db_path: Path to the relay's sqlite store.
        token: The shared Bearer ingest token.

    Returns:
        A bound RelayServer. Its actual bound port is server.server_address[1]
        (useful when port 0 was requested).

    Why:
        Separating "build/bind" from "serve forever" makes the server testable: a
        test binds to port 0, reads the assigned port, drives requests on a thread,
        then calls shutdown() — none of which is possible if construction blocked.
    """
    return RelayServer((host, port), db_path, token)


def serve(host: str, port: int, db_path: Path, token: str) -> None:
    """Run the relay server until interrupted (the blocking entry point).

    Args:
        host: Interface to bind.
        port: Port to bind.
        db_path: Path to the relay's sqlite store.
        token: The shared Bearer ingest token.

    Returns:
        None. Blocks serving requests until Ctrl-C, then shuts down cleanly.

    Why:
        This is what `orion relay-serve` (CP8) calls. It prints the bound address so
        the user knows where the dashboard/ingest live, and handles Ctrl-C as a
        clean stop rather than a traceback.
    """
    server = create_server(host, port, db_path, token)
    bound_host, bound_port = server.server_address
    print(
        f"[relay] listening on http://{bound_host}:{bound_port}  (db: {db_path})",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

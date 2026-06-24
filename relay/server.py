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
from zoneinfo import ZoneInfo

from .render import (
    _DISPLAY_TZ,
    MAX_AUTHOR_CHARS,
    MAX_COMMENT_BODY_CHARS,
    render_index,
    render_not_found,
    render_project,
    render_report,
)
from .store import (
    add_comment,
    comments_for,
    comments_for_project,
    get,
    history,
    ingest,
    list_projects,
    open_relay_store,
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
        The comment POST is driven by a browser form, so its rejections (400/403)
        read better as HTML than the JSON /ingest returns. These pages carry NO
        dynamic input — every caller passes a fixed message — so, exactly like
        _UNAUTHORIZED_HTML, they need no escaping. Keeping them here leaves render.py
        (the dashboard view layer) untouched.
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

        Read auth: when the server is configured with a view secret, every dashboard
        route requires HTTP Basic credentials (a browser-native login); without one —
        the loopback-only default — reads stay open for zero-friction local use. The
        fail-closed guard in create_server() guarantees a view secret IS set whenever
        the bind host is non-loopback, so a world-reachable dashboard is never
        unauthenticated. Ingest (the write surface) stays Bearer-authed independently.

        The "/api/..." namespace is the EXCEPTION: it is a machine-JSON surface for the
        local pull-back client (C2), Bearer-authed like /ingest — NOT the browser's
        Basic scheme. So it is routed FIRST, before the Basic gate below, keeping the
        two consumers' auth schemes cleanly separated (a browser never reaches it; the
        CLI never trips the Basic dialog).
        """
        # Strip any query string; routing is by path only (the /api/ handler re-reads
        # the query itself, since it — unlike the dashboard — takes parameters).
        path = urllib.parse.urlparse(self.path).path

        # Machine-JSON API routes are Bearer-authed and bypass the Basic view gate.
        if path == "/api/comments":
            self._handle_api_comments()
            return

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
        # A fresh connection per request keeps each sqlite handle on its own thread.
        conn = open_relay_store(self.server.db_path)
        try:
            if path == "/":
                self._send_html(200, render_index(list_projects(conn)))
                return

            if path.startswith("/project/"):
                # Decode the percent-encoded name back to the stored project string.
                name = urllib.parse.unquote(path[len("/project/"):])
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
                if report is None:
                    self._send_html(404, render_not_found(f"No report {id_str!r}."))
                    return
                # report is not None implies id_str was numeric, so int() is safe.
                comments = comments_for(conn, int(id_str))
                self._send_html(
                    200, render_report(report, comments, self.server.display_tz)
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
          1. Auth — gated by the dashboard view secret, exactly as the GET dashboard
             is: a view secret set means require HTTP Basic (401 otherwise); none set
             (loopback dev) means open — consistent with reads.
          2. CSRF — Basic auth makes the browser auto-send credentials, so a forged
             cross-site POST would otherwise succeed. Require an Origin header whose
             host matches the request Host; reject a mismatch with 403.
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
        # 1) Auth: enforced only when a view secret is configured (same gate as do_GET).
        if self.server.view_token and self._view_auth_error() is not None:
            self._send_html(
                401,
                _UNAUTHORIZED_HTML,
                extra_headers={"WWW-Authenticate": 'Basic realm="Orion dashboard"'},
            )
            return

        # 2) CSRF: require a same-origin POST.
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

        # parse_qs maps each key to a LIST of values; take the first (or "" if absent),
        # then strip — a name/body of only whitespace counts as empty.
        author = fields.get("author", [""])[0].strip()
        body = fields.get("body", [""])[0].strip()

        # 4) Validate: a non-empty body within caps, and a capped author.
        if not body:
            self._send_html(400, _simple_html("bad request", "A comment body is required."))
            return
        if len(body) > MAX_COMMENT_BODY_CHARS or len(author) > MAX_AUTHOR_CHARS:
            self._send_html(400, _simple_html("bad request", "Comment or name is too long."))
            return

        # 5) Confirm the report exists, then store and redirect back to it. One fresh
        # connection per request keeps each sqlite handle on its own thread.
        conn = open_relay_store(self.server.db_path)
        try:
            if get(conn, report_id) is None:
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

    def _origin_error(self) -> str | None:
        """Return None if the request is same-origin, else a 403 reason string.

        Returns:
            None when an Origin header is present and its host (netloc) equals the
            request's Host header; otherwise a short reason — Origin missing, or
            Origin not matching Host.

        Why:
            The comment POST authenticates via the view secret over HTTP Basic, which
            the browser AUTO-SENDS on every request to this origin — including one a
            malicious third-party page triggers (classic CSRF). An Origin check is the
            lightweight, dependency-free defense: a genuine same-site form submit
            carries an Origin equal to our own host, while a cross-site forgery carries
            the attacker's origin (or, for some flows, none) — so we require Origin
            present AND matching. We compare netloc (host[:port]) because that is what
            survives the Fly topology: the browser sends Origin
            "https://<app>.fly.dev", whose netloc is "<app>.fly.dev", and the proxied
            request arrives with Host "<app>.fly.dev" — equal. (A SameSite-cookie
            defense doesn't apply here: there is no session cookie, the credential is
            Basic auth, so the Origin check is the right tool.)
        """
        origin = self.headers.get("Origin")
        if not origin:
            return "missing Origin header"
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

    def _send_redirect(self, code: int, location: str) -> None:
        """Write a bodyless redirect response to `location`.

        Args:
            code: The 3xx status code (303 for POST-redirect-GET).
            location: The path to redirect to (a site-relative path is fine).

        Why:
            After a successful comment POST we 303 back to the report page so the
            browser re-fetches it with a GET — the POST-redirect-GET pattern, which
            stops a page refresh from resubmitting the comment. A redirect needs no
            body, so we send an explicit zero Content-Length plus the Location header.
        """
        self.send_response(code)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()


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
    ):
        # Set config before binding so it is available to any request handled after
        # serve_forever() starts.
        self.db_path = db_path
        self.token = token
        self.view_token = view_token
        self.display_tz = display_tz
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
    return RelayServer((host, port), db_path, token, view_token, display_tz)


def serve(
    host: str,
    port: int,
    db_path: Path,
    token: str,
    view_token: str | None = None,
    require_view_auth: bool = False,
    display_tz: ZoneInfo = _DISPLAY_TZ,
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
        host, port, db_path, token, view_token, require_view_auth, display_tz
    )
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

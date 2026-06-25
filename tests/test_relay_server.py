# =============================================================================
# tests/test_relay_server.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the relay's HTTP surface end to end against a REAL
#                  running server — the ingest endpoint (Bearer auth 401, payload
#                  validation 400, successful store 201, unknown-path 404) AND the
#                  dashboard read auth (HTTP Basic) plus the fail-closed bind guard
#                  that forbids a non-loopback bind without a view secret.
# Role in project: The ingest endpoint is Orion's first inbound surface; these
#                  tests pin that it authenticates, validates, and only then
#                  stores. Critically, the happy path is fed REAL serialize_blob
#                  output, so it proves the local→relay contract holds end to end
#                  (not just against a hand-made payload).
# Test approach: a RelayServer is bound to an ephemeral port (port 0) and served on
#                a daemon thread; tests drive it with real urllib HTTP requests,
#                then assert on responses and on what landed in the store.
# =============================================================================

import base64
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from orion.config import ProjectConfig, Recipient
from orion.report import build_report, serialize_blob
from relay.render import MAX_COMMENT_BODY_CHARS
from relay.server import (
    _b64url_encode,
    _is_loopback,
    _sign,
    create_server,
    key_verifier,
    make_session_value,
    mint_key,
    verify_session_value,
)
from relay.store import (
    add_comment,
    comments_for,
    get,
    list_projects,
    open_relay_store,
)

_TOKEN = "test-ingest-token"
_VIEW = "test-view-secret"


@contextmanager
def _running_relay(tmp_path, token=_TOKEN, view_token=None, require_view_auth=False):
    """Start a RelayServer on an ephemeral port in a thread; yield (base_url, db).

    Args:
        tmp_path: pytest temp dir for the relay's sqlite file.
        token: the shared ingest Bearer token.
        view_token: optional dashboard read secret. None (default) leaves GETs open,
            matching the loopback access model; set it to exercise read auth.
        require_view_auth: force the view secret even on this loopback bind (the
            reverse-proxy topology); used to exercise that guard path.

    Why:
        The ingest/read contracts can only be proven against a real server (real auth
        header parsing, real status codes). Binding to port 0 avoids port clashes;
        serving on a daemon thread lets the test make blocking HTTP calls from the
        main thread. The context manager guarantees the server is shut down even if
        an assertion fails.
    """
    db = tmp_path / "relay.sqlite3"
    server = create_server("127.0.0.1", 0, db, token, view_token, require_view_auth)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _, port = server.server_address
        yield f"http://127.0.0.1:{port}", db
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _post(base_url, body, *, token=_TOKEN, path="/ingest"):
    """POST `body` (bytes) to the relay and return (status_code, response_bytes).

    Args:
        base_url: The server's base URL.
        body: The raw request body bytes.
        token: Bearer token to send, or None to omit the Authorization header.
        path: The request path (defaults to /ingest).

    Why:
        Centralizes the request plumbing so each test states only the body/token it
        is exercising. HTTPError is caught and unwrapped so a 4xx is returned as a
        normal (code, body) tuple to assert on, not raised.
    """
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        base_url + path, data=body, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _get(base_url, path, *, password=None, bearer=None):
    """GET `path` from the relay and return (status_code, response_text).

    Args:
        base_url: the server's base URL.
        path: the request path.
        password: optional dashboard read secret to send as HTTP Basic auth (any
            username; None omits the Authorization header).
        bearer: optional Bearer token for the machine-JSON /api routes (None omits
            it). Distinct from `password` because the two surfaces use different auth
            schemes — Basic for the browser dashboard, Bearer for the pull-back API.

    Why:
        Mirrors _post for the read side, unwrapping HTTPError so a 401/404 comes back
        as a tuple to assert on. The password/bearer args let read-auth tests send (or
        omit) the right credential scheme without each test rebuilding the request.
    """
    headers = {}
    if password is not None:
        raw = base64.b64encode(f"orion:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {raw}"
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    request = urllib.request.Request(base_url + path, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _real_blob_json():
    """Produce a real serialized blob via the production path (build + serialize).

    Why:
        The whole point of the end-to-end ingest test is that the EXACT bytes local
        Orion would push are accepted and stored. Building through build_report +
        serialize_blob (not a hand-made dict) is what proves the contract — if the
        server's validation and the serializer ever disagree, this breaks.
    """
    project = ProjectConfig(
        name="demo",
        repo_path=Path("/tmp/demo"),
        share_level="high_level",
        collectors=("git",),
        recipients=(Recipient(name="Alex", channel="discord", webhook_env_var="ORION_W"),),
    )
    blob = build_report(
        project,
        body="Shipped the relay seam.",
        lane="raw",
        source_marker="",
        generated_at="2026-06-18T00:00:00+00:00",
        sections=(("Code activity", "Shipped the seam."),),
    )
    return serialize_blob(blob)


def test_valid_push_is_stored_and_returns_201(tmp_path):
    """A real serialized blob with the right token is stored and returns 201 + id.

    Why this matters: this is the end-to-end contract — local Orion's actual output
    is accepted, persisted, and the row is queryable with its sections intact. If
    serialize_blob and the server's validation/store ever drift, this fails.
    """
    with _running_relay(tmp_path) as (base_url, db):
        status, body = _post(base_url, _real_blob_json().encode("utf-8"))
        assert status == 201
        new_id = json.loads(body)["id"]

        # The blob actually landed in the store, sections preserved end to end.
        conn = open_relay_store(db)
        report = get(conn, new_id)
        assert report is not None
        assert report["project"] == "demo"
        assert report["sections"] == [["Code activity", "Shipped the seam."]]


def test_wrong_token_is_401_invalid_and_stores_nothing(tmp_path):
    """A valid body with the WRONG token is rejected 401 ("invalid token"), not stored.

    Why this matters: auth is the gate on the inbound surface. A bad token must not
    only be refused — it must not leave any trace in the store (auth is checked
    before the body is even read). The message says the token is invalid (not that
    it is missing), so a self-hoster can tell a mismatch from an omission — and it
    never echoes the expected token.
    """
    with _running_relay(tmp_path) as (base_url, db):
        status, body = _post(base_url, _real_blob_json().encode("utf-8"), token="wrong")
        assert status == 401
        message = json.loads(body)["error"]
        assert "invalid token" in message
        # The expected token must never appear in any response.
        assert _TOKEN not in body.decode("utf-8")

        conn = open_relay_store(db)
        assert list_projects(conn) == []  # nothing stored


def test_absent_token_is_401_missing_header(tmp_path):
    """A request with no Authorization header is rejected 401 ("missing ... header").

    Why this matters: missing credentials are still refused (the endpoint is never
    open by omission), but the message distinguishes "you sent no token" from "your
    token is wrong" — which leaks nothing about the secret (there is no identity to
    enumerate) yet tells a self-hoster their config forgot the header entirely.
    """
    with _running_relay(tmp_path) as (base_url, _db):
        status, body = _post(base_url, _real_blob_json().encode("utf-8"), token=None)
        assert status == 401
        assert "missing or malformed" in json.loads(body)["error"]


def test_401_advertises_bearer_scheme(tmp_path):
    """A 401 carries the spec-mandated WWW-Authenticate: Bearer header.

    Why this matters: RFC 7235 says a 401 SHOULD advertise the auth scheme. Sending
    `WWW-Authenticate: Bearer` is the standards-correct way to tell a client how to
    authenticate, and costs nothing.
    """
    with _running_relay(tmp_path) as (base_url, _db):
        # Build the request directly so we can read the response headers on the
        # HTTPError (the _post helper discards them).
        req = urllib.request.Request(
            base_url + "/ingest",
            data=_real_blob_json().encode("utf-8"),
            headers={"Content-Type": "application/json"},  # no Authorization
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("expected a 401")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
            assert exc.headers.get("WWW-Authenticate") == "Bearer"


def test_malformed_json_is_400(tmp_path):
    """A valid token but non-JSON body returns 400.

    Why this matters: even an authenticated caller can send garbage; the endpoint
    must reject an unparseable body cleanly rather than crash.
    """
    with _running_relay(tmp_path) as (base_url, _db):
        status, _ = _post(base_url, b"this is not json")
        assert status == 400


def test_invalid_shape_is_400_and_stores_nothing(tmp_path):
    """Valid JSON that violates the blob contract returns 400 and stores nothing.

    Why this matters: shape validation protects the store and the renderer. Here we
    drop the required `project` field from an otherwise-real blob; the endpoint must
    reject it (400) and persist nothing.
    """
    with _running_relay(tmp_path) as (base_url, db):
        payload = json.loads(_real_blob_json())
        del payload["project"]  # break the contract
        status, _ = _post(base_url, json.dumps(payload).encode("utf-8"))
        assert status == 400

        conn = open_relay_store(db)
        assert list_projects(conn) == []


def test_section_not_a_pair_is_400(tmp_path):
    """A section that is not a [title, body] pair is rejected 400.

    Why this matters: CP7's renderer unpacks each section as exactly (title, body).
    A malformed section (here a 3-element list) must be caught at the inbound
    boundary, not blow up later in rendering.
    """
    with _running_relay(tmp_path) as (base_url, _db):
        payload = json.loads(_real_blob_json())
        payload["sections"] = [["Title", "Body", "extra"]]  # not a pair
        status, _ = _post(base_url, json.dumps(payload).encode("utf-8"))
        assert status == 400


def test_unknown_path_is_404(tmp_path):
    """A POST to a path other than /ingest returns 404.

    Why this matters: the ingest endpoint is the only POST route; anything else is
    not found, so a typo'd or probing request gets a clean 404.
    """
    with _running_relay(tmp_path) as (base_url, _db):
        status, _ = _post(base_url, _real_blob_json().encode("utf-8"), path="/nope")
        assert status == 404


# --- CP7: the read-only dashboard GET routes, end to end ----------------------
#
# These drive the SAME running server through HTTP after ingesting a real blob, so
# they prove the store -> render wiring (not just the pure render functions, which
# test_relay_render.py covers). Ingest is authenticated; the dashboard GETs are not
# (loopback-bound access model — see do_GET).


def test_dashboard_index_then_project_then_report(tmp_path):
    """After a real push, the index → project → report routes all render it.

    Why this matters: this is the dashboard's whole navigation path end to end. We
    ingest one real blob, then walk /, /project/demo, and /report/<id>, confirming
    each 200s and shows the expected content drawn from the store.
    """
    with _running_relay(tmp_path) as (base_url, _db):
        status, body = _post(base_url, _real_blob_json().encode("utf-8"))
        assert status == 201
        report_id = json.loads(body)["id"]

        # Index lists the project and links to its page.
        code, index_html = _get(base_url, "/")
        assert code == 200
        assert "demo" in index_html
        assert 'href="/project/demo"' in index_html

        # Project page lists the report and links to it.
        code, project_html = _get(base_url, "/project/demo")
        assert code == 200
        assert f'href="/report/{report_id}"' in project_html

        # Report page shows the section content.
        code, report_html = _get(base_url, f"/report/{report_id}")
        assert code == 200
        assert "Code activity" in report_html
        assert "Shipped the seam." in report_html


def test_dashboard_unknown_report_id_is_404(tmp_path):
    """A GET for a report id that does not exist returns 404.

    Why this matters: a stale or hand-typed /report/<id> link must be a clean 404
    page, the behavior the store's get()->None contract feeds.
    """
    with _running_relay(tmp_path) as (base_url, _db):
        code, html = _get(base_url, "/report/999")
        assert code == 404
        assert "Not found" in html


def test_dashboard_unknown_get_path_is_404(tmp_path):
    """A GET to an unrouted path returns 404.

    Why this matters: anything outside the three known views is not found — a
    probing or typo'd GET gets a clean 404 rather than a server error.
    """
    with _running_relay(tmp_path) as (base_url, _db):
        code, _ = _get(base_url, "/nope")
        assert code == 404


# --- CP1: dashboard read auth (HTTP Basic) + the fail-closed bind guard ---------
#
# Read auth is enforced ONLY when a view secret is configured; with none set (the
# loopback default) GETs stay open. The fail-closed guard then guarantees a secret
# IS set whenever the bind is non-loopback, so a world-reachable dashboard is never
# unauthenticated. Ingest (the write surface) is unaffected — it stays Bearer-authed.


def test_dashboard_open_when_no_view_secret(tmp_path):
    """With no view secret, dashboard GETs serve without credentials (200).

    Why this matters: this pins the backward-compatible default — local, loopback
    use stays zero-friction (no password) exactly as before CP1. The guard (below)
    is what keeps this default safe by forbidding it on a non-loopback bind.
    """
    with _running_relay(tmp_path) as (base_url, _db):
        code, _ = _get(base_url, "/")
        assert code == 200


def test_dashboard_requires_basic_auth_when_view_secret_set(tmp_path):
    """With a view secret set, a GET sent with no credentials is rejected 401.

    Why this matters: this is the read gate that makes leaving loopback safe — once a
    secret exists, the dashboard is closed to anyone who can't present it.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, _db):
        code, _ = _get(base_url, "/")
        assert code == 401


def test_dashboard_401_advertises_basic_and_never_echoes_secret(tmp_path):
    """A read 401 carries WWW-Authenticate: Basic and never leaks the secret.

    Why this matters: the Basic scheme header is what makes a browser show its native
    login dialog (the whole UX of read auth). And, like the ingest path, the response
    must never contain the expected secret.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, _db):
        # Build the request directly so the response headers are readable on the error.
        req = urllib.request.Request(base_url + "/", method="GET")
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("expected a 401")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
            assert exc.headers.get("WWW-Authenticate", "").startswith("Basic")
            assert _VIEW not in exc.read().decode("utf-8")


def test_dashboard_wrong_password_is_401(tmp_path):
    """Basic credentials with the wrong password are rejected 401.

    Why this matters: a present-but-wrong credential must be refused just like a
    missing one — the secret is the gate, not merely the presence of a header.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, _db):
        code, _ = _get(base_url, "/", password="not-the-secret")
        assert code == 401


def test_dashboard_correct_password_is_200(tmp_path):
    """Basic credentials with the right password render the dashboard (200).

    Why this matters: the positive path — a viewer who presents the secret sees the
    content, proving the auth check lets valid requests through to the renderer.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, _db):
        assert _post(base_url, _real_blob_json().encode("utf-8"))[0] == 201
        code, html = _get(base_url, "/", password=_VIEW)
        assert code == 200
        assert "demo" in html


def test_ingest_unaffected_by_view_secret(tmp_path):
    """A view secret gates GETs but does not touch the Bearer-authed ingest path.

    Why this matters: read auth and write auth are independent. With a view secret set,
    a normal Bearer-authed push must still be accepted (201) — read protection must not
    accidentally break the write surface.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, _db):
        status, _ = _post(base_url, _real_blob_json().encode("utf-8"))
        assert status == 201


def test_guard_refuses_non_loopback_bind_without_view_secret(tmp_path):
    """create_server raises (and never binds) for a non-loopback host with no secret.

    Why this matters: this is the fail-closed guard — the single rule that makes
    "deployed 0.0.0.0 with an open dashboard" impossible by construction. It raises
    BEFORE binding, so no world-reachable socket is ever opened.
    """
    db = tmp_path / "relay.sqlite3"
    with pytest.raises(ValueError, match="non-loopback"):
        create_server("0.0.0.0", 0, db, _TOKEN)  # no view_token


def test_guard_allows_non_loopback_bind_with_view_secret(tmp_path):
    """A non-loopback bind WITH a view secret is permitted (the guard passes).

    Why this matters: the guard must block only the unsafe case — a deployer who has
    set a read secret is allowed to bind beyond loopback. We construct then immediately
    close the server (never serve) to assert construction does not raise.
    """
    db = tmp_path / "relay.sqlite3"
    server = create_server("0.0.0.0", 0, db, _TOKEN, _VIEW)
    server.server_close()  # opened an ephemeral socket; close it without serving


def test_is_loopback_classification():
    """_is_loopback treats loopback hosts as such and everything else as reachable.

    Why this matters: the guard keys entirely off this classification, so its edges
    must be right — 0.0.0.0 (bind-all) and a LAN IP/hostname must NOT count as
    loopback, while 127.x, ::1, and the literal "localhost" must.
    """
    assert _is_loopback("127.0.0.1")
    assert _is_loopback("localhost")
    assert _is_loopback("::1")
    assert not _is_loopback("0.0.0.0")
    assert not _is_loopback("192.168.1.10")
    assert not _is_loopback("relay.example.com")


def test_require_view_auth_forces_secret_on_loopback(tmp_path):
    """--require-view-auth makes the guard demand a view secret even on a loopback bind.

    Why this matters: behind a reverse proxy the relay binds loopback, so the
    host-based guard can't see that the dashboard is publicly reachable (KI-18).
    require_view_auth closes that gap — a loopback bind with no secret now fails closed
    instead of serving an open dashboard through the proxy.
    """
    db = tmp_path / "relay.sqlite3"
    with pytest.raises(ValueError, match="require-view-auth"):
        create_server("127.0.0.1", 0, db, _TOKEN, None, require_view_auth=True)


def test_require_view_auth_with_secret_on_loopback_enforces_basic(tmp_path):
    """With require_view_auth + a secret, a loopback relay starts AND gates the dashboard.

    Why this matters: this is the proxy topology done right — the guard passes (a secret
    is set), and the loopback dashboard still demands Basic credentials, so the proxy
    cannot expose an unauthenticated view. Proves both the guard and the enforcement.
    """
    with _running_relay(tmp_path, view_token=_VIEW, require_view_auth=True) as (base_url, _db):
        assert _get(base_url, "/")[0] == 401  # no creds -> refused even on loopback
        assert _get(base_url, "/", password=_VIEW)[0] == 200  # correct creds -> served


# --- C2: dashboard comments (POST /report/<id>/comment) -------------------------
#
# The comment route is Orion's first write-from-a-browser surface, so these tests are
# the security checklist made executable: auth (view secret), CSRF (Origin check),
# validation (non-empty body + length caps), report-existence, and the 303
# POST-redirect-GET on success. The route speaks HTTP form data (not JSON like
# /ingest) and is gated by the SAME view secret as the read dashboard.


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """An opener handler that does NOT follow redirects.

    Why:
        A successful comment POST replies 303 (POST-redirect-GET). urllib follows 3xx
        automatically, which would swallow the 303 and return the redirected GET's 200.
        Returning None from redirect_request stops the follow, so the 303 + Location
        surface to the test (as an HTTPError we unwrap) instead of being hidden.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _ingest_one(base_url):
    """Push one real report and return its id (the target for comment tests).

    Why:
        Every comment test needs an existing report to hang a comment off. Ingest is
        Bearer-authed and independent of the view secret, so this works whether or not
        the relay has a view secret set.
    """
    status, body = _post(base_url, _real_blob_json().encode("utf-8"))
    assert status == 201
    return json.loads(body)["id"]


def _post_comment(
    base_url,
    report_id,
    *,
    author="Alex",
    body="Nice work.",
    password=None,
    origin="__match__",
):
    """POST a comment form and return (status, headers, text), without following 303.

    Args:
        base_url: the server base URL.
        report_id: the report id to comment on (goes in the path).
        author, body: the urlencoded form fields. Pass body="" to exercise the
            empty-body rejection (parse_qs drops blank values, so the server sees none).
        password: optional view secret sent as HTTP Basic (any username); None omits
            the Authorization header (the no-credentials case).
        origin: the Origin header. "__match__" (default) sends an Origin equal to the
            server's own base URL, so its host matches the request Host (a same-origin
            request). None omits the header; any other string is sent verbatim to
            exercise a cross-origin rejection.

    Why:
        Centralizes the form-POST plumbing (urlencoding, Basic auth, Origin, and the
        no-redirect opener) so each test states only the field it is exercising. A
        303 or any 4xx surfaces as an HTTPError, unwrapped here into a uniform
        (code, headers, text) tuple.
    """
    data = urllib.parse.urlencode({"author": author, "body": body}).encode("utf-8")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if password is not None:
        raw = base64.b64encode(f"orion:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {raw}"
    if origin == "__match__":
        # Same-origin: derive Origin from base_url so its netloc equals the Host header
        # urllib sets from the URL.
        headers["Origin"] = base_url
    elif origin is not None:
        headers["Origin"] = origin
    req = urllib.request.Request(
        base_url + f"/report/{report_id}/comment",
        data=data,
        headers=headers,
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=5) as response:
            return response.status, response.headers, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read().decode("utf-8")


def test_comment_is_stored_and_redirects(tmp_path):
    """A valid, authed, same-origin comment is stored and answered with a 303.

    Why this matters: this is the happy path end to end — correct view credentials, a
    matching Origin, and a non-empty body produce a stored comment and a 303 redirect
    back to the report (POST-redirect-GET, so a refresh won't resubmit). We assert both
    the redirect target and that the comment actually landed in the store.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        report_id = _ingest_one(base_url)

        status, headers, _ = _post_comment(
            base_url, report_id, author="Alex", body="Ship it.", password=_VIEW
        )
        assert status == 303
        assert headers.get("Location") == f"/report/{report_id}"

        conn = open_relay_store(db)
        stored = comments_for(conn, report_id)
        assert len(stored) == 1
        assert stored[0]["author"] == "Alex"
        assert stored[0]["body"] == "Ship it."


def test_comment_without_credentials_is_401(tmp_path):
    """With a view secret set, a comment POST sent with no credentials is 401, not stored.

    Why this matters: the comment write path reuses the dashboard read gate — once a
    view secret exists, you must present it to comment, exactly as to read. Auth is
    checked first, so nothing is stored.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        report_id = _ingest_one(base_url)
        status, _, _ = _post_comment(base_url, report_id, password=None)
        assert status == 401

        conn = open_relay_store(db)
        assert comments_for(conn, report_id) == []


def test_comment_cross_origin_is_403_and_stores_nothing(tmp_path):
    """An authed comment from a foreign Origin is rejected 403 (CSRF guard), not stored.

    Why this matters: Basic auth means the browser auto-sends credentials, so a
    malicious page could forge an authenticated comment POST. The Origin check is the
    defense: a request whose Origin host differs from Host is refused even with valid
    credentials, and leaves no trace in the store.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        report_id = _ingest_one(base_url)
        status, _, _ = _post_comment(
            base_url, report_id, password=_VIEW, origin="https://evil.example"
        )
        assert status == 403

        conn = open_relay_store(db)
        assert comments_for(conn, report_id) == []


def test_comment_missing_origin_is_403(tmp_path):
    """A comment POST with NO Origin header is rejected 403.

    Why this matters: the CSRF guard requires Origin to be present AND matching — a
    request that omits it entirely (as some forged cross-site flows do) must fail
    closed, not be treated as same-origin by default.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, _db):
        report_id = _ingest_one(base_url)
        status, _, _ = _post_comment(base_url, report_id, password=_VIEW, origin=None)
        assert status == 403


def test_comment_on_missing_report_is_404(tmp_path):
    """An authed, same-origin comment on a nonexistent report id returns 404.

    Why this matters: a stale or forged /report/<id>/comment link must be a clean 404,
    not a crash. Auth and the Origin check pass first (so this isolates the
    report-existence guard), then the missing report is reported as not found.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, _db):
        status, _, _ = _post_comment(base_url, 999, password=_VIEW)
        assert status == 404


def test_comment_empty_body_is_400_and_stores_nothing(tmp_path):
    """A comment with an empty body is rejected 400 and stored nothing.

    Why this matters: a comment must carry text. An empty (or whitespace-only) body is
    a validation failure, caught at the inbound boundary before the store is touched.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        report_id = _ingest_one(base_url)
        status, _, _ = _post_comment(base_url, report_id, body="", password=_VIEW)
        assert status == 400

        conn = open_relay_store(db)
        assert comments_for(conn, report_id) == []


def test_comment_oversized_body_is_400(tmp_path):
    """A comment body over the length cap is rejected 400.

    Why this matters: we never trust a client-sent size. A body beyond
    MAX_COMMENT_BODY_CHARS is refused, mirroring the ingest endpoint's length
    discipline, so no oversized text reaches the store.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        report_id = _ingest_one(base_url)
        too_long = "x" * (MAX_COMMENT_BODY_CHARS + 1)
        status, _, _ = _post_comment(base_url, report_id, body=too_long, password=_VIEW)
        assert status == 400

        conn = open_relay_store(db)
        assert comments_for(conn, report_id) == []


def test_comment_open_when_no_view_secret(tmp_path):
    """With NO view secret, a same-origin comment is accepted without credentials.

    Why this matters: this pins the loopback-dev default — reads are open without a
    secret, and so is commenting, keeping local use zero-friction. The Origin check
    still applies (it is not an auth control), so the request must be same-origin.
    """
    with _running_relay(tmp_path) as (base_url, db):  # view_token=None
        report_id = _ingest_one(base_url)
        status, _, _ = _post_comment(base_url, report_id, body="local note", password=None)
        assert status == 303

        conn = open_relay_store(db)
        assert [c["body"] for c in comments_for(conn, report_id)] == ["local note"]


def test_comment_appears_on_the_report_page(tmp_path):
    """After posting, the comment is shown on the report's GET page.

    Why this matters: this is the store -> render wiring end to end — a posted comment
    must actually surface (author and body) when the report page is fetched, proving
    do_GET feeds comments_for into render_report.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, _db):
        report_id = _ingest_one(base_url)
        _post_comment(
            base_url, report_id, author="Reviewer", body="Looks solid.", password=_VIEW
        )

        code, html = _get(base_url, f"/report/{report_id}", password=_VIEW)
        assert code == 200
        assert "Reviewer" in html
        assert "Looks solid." in html


# --- C2 pull-back: GET /api/comments (Bearer-authed machine JSON) ----------------
#
# This is the machine-readable counterpart to the browser dashboard: the local
# pull-back client fetches a project's comments as JSON over Bearer auth (the SAME
# token /ingest uses), entirely separate from the Basic-gated HTML routes. These
# tests pin the auth scheme, the project/since_id validation, and the JSON shape
# (comments + the latest_id watermark) the client advances from.


def _api_comments_path(project=None, since_id=None):
    """Build a "/api/comments" path with an (optionally) encoded query string.

    Args:
        project: value for the `project` query param, or None to omit it (the
            missing-required-param case).
        since_id: value for `since_id` (any type — str is sent verbatim to exercise
            the bad-input path), or None to omit it (defaults to 0 server-side).

    Why:
        Centralizes query-string construction so each test states only the params it
        is exercising, and so a deliberately-bad since_id (e.g. "nope") can be sent
        without urlencode rejecting it.
    """
    params = {}
    if project is not None:
        params["project"] = project
    if since_id is not None:
        params["since_id"] = since_id
    query = urllib.parse.urlencode(params)
    return "/api/comments" + (f"?{query}" if query else "")


def _seed_comments(db, report_id, bodies):
    """Append comments (by body) to a report directly via the store; return their ids.

    Why:
        The /api/comments tests need existing comments to read back. Seeding through
        the store (not the HTTP comment route) keeps these tests focused on the read
        endpoint alone, independent of the comment POST path's auth/CSRF rules.
    """
    conn = open_relay_store(db)
    try:
        return [
            add_comment(conn, report_id, "Alex", body, "2026-06-18T10:00:00+00:00")
            for body in bodies
        ]
    finally:
        conn.close()


def test_api_comments_requires_bearer_and_advertises_scheme(tmp_path):
    """GET /api/comments with no Bearer token is 401 + WWW-Authenticate: Bearer.

    Why this matters: the read endpoint is authed before any query runs, using the
    same Bearer scheme as /ingest (NOT the dashboard's Basic). A 401 advertising
    Bearer tells the client how to authenticate, and nothing about the data leaks
    because the query never executes.
    """
    with _running_relay(tmp_path) as (base_url, _db):
        # Build the request directly so the 401's headers are readable on the error.
        req = urllib.request.Request(
            base_url + _api_comments_path(project="demo"), method="GET"
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("expected a 401")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
            assert exc.headers.get("WWW-Authenticate") == "Bearer"


def test_api_comments_wrong_token_is_401_and_never_echoes_secret(tmp_path):
    """A wrong Bearer token is rejected 401 and the response never contains the token.

    Why this matters: like the ingest path, a mismatch is refused and the expected
    token is never echoed — the secret stays secret even in an error.
    """
    with _running_relay(tmp_path) as (base_url, _db):
        code, body = _get(base_url, _api_comments_path(project="demo"), bearer="wrong")
        assert code == 401
        assert _TOKEN not in body


def test_api_comments_returns_projects_comments_as_json(tmp_path):
    """A Bearer-authed pull returns the project's comments and a latest_id watermark.

    Why this matters: this is the endpoint's core promise — the client gets each
    comment's fields as JSON, oldest first, plus the highest id so it can advance its
    local unread watermark. We seed two comments across the project and read them back.
    """
    with _running_relay(tmp_path) as (base_url, db):
        report_id = _ingest_one(base_url)
        c1, c2 = _seed_comments(db, report_id, ["first reply", "second reply"])

        code, body = _get(base_url, _api_comments_path(project="demo"), bearer=_TOKEN)
        assert code == 200
        payload = json.loads(body)
        assert [c["body"] for c in payload["comments"]] == ["first reply", "second reply"]
        assert [c["id"] for c in payload["comments"]] == [c1, c2]
        # latest_id is the highest comment id, the watermark the client advances to.
        assert payload["latest_id"] == c2


def test_api_comments_since_id_returns_only_newer(tmp_path):
    """since_id returns only strictly-newer comments; latest_id reflects them.

    Why this matters: this is the unread cursor over HTTP. After seeing up to c1, a
    pull with since_id=c1 must return only c2 and report latest_id=c2 — no re-delivery
    of seen comments, no missed new ones.
    """
    with _running_relay(tmp_path) as (base_url, db):
        report_id = _ingest_one(base_url)
        c1, c2 = _seed_comments(db, report_id, ["seen", "new"])

        code, body = _get(
            base_url, _api_comments_path(project="demo", since_id=c1), bearer=_TOKEN
        )
        assert code == 200
        payload = json.loads(body)
        assert [c["body"] for c in payload["comments"]] == ["new"]
        assert payload["latest_id"] == c2


def test_api_comments_caught_up_echoes_since_id_as_latest(tmp_path):
    """With nothing newer than since_id, comments is [] and latest_id echoes since_id.

    Why this matters: "no new replies" must keep the watermark where the client asked
    (so advancing to latest_id is always safe and idempotent), and return a clean 200
    empty rather than a 404 — matching the dashboard's empty-state philosophy.
    """
    with _running_relay(tmp_path) as (base_url, db):
        report_id = _ingest_one(base_url)
        (last,) = _seed_comments(db, report_id, ["only"])

        code, body = _get(
            base_url, _api_comments_path(project="demo", since_id=last), bearer=_TOKEN
        )
        assert code == 200
        payload = json.loads(body)
        assert payload["comments"] == []
        assert payload["latest_id"] == last


def test_api_comments_unknown_project_is_200_empty(tmp_path):
    """An authed pull for a project with no comments is a 200 empty list, not a 404.

    Why this matters: "never seen" and "no replies yet" look the same to the client —
    both a clean empty result with latest_id echoing the requested since_id (0 here).
    """
    with _running_relay(tmp_path) as (base_url, _db):
        code, body = _get(
            base_url, _api_comments_path(project="never-seen"), bearer=_TOKEN
        )
        assert code == 200
        payload = json.loads(body)
        assert payload["comments"] == []
        assert payload["latest_id"] == 0


def test_api_comments_missing_project_is_400(tmp_path):
    """A pull without the required `project` param is rejected 400.

    Why this matters: project is the handle the whole pull is keyed on; an absent or
    blank one is a client error caught at the boundary, never a query with no filter.
    """
    with _running_relay(tmp_path) as (base_url, _db):
        # Authed, but no project param.
        code, _ = _get(base_url, _api_comments_path(), bearer=_TOKEN)
        assert code == 400


def test_api_comments_bad_since_id_is_400(tmp_path):
    """A non-numeric since_id is rejected 400 (never trust client input).

    Why this matters: since_id drives a SQL comparison; a non-integer must be refused
    cleanly (mirroring the report-id isdigit() guard) rather than coerced or crashing.
    A negative value is likewise non-numeric to isdigit(), so it is caught here too.
    """
    with _running_relay(tmp_path) as (base_url, _db):
        code, _ = _get(
            base_url, _api_comments_path(project="demo", since_id="nope"), bearer=_TOKEN
        )
        assert code == 400


# --- C2-bots: POST /api/comments (Bearer-authed machine comment write) -----------
#
# The native bot's write path: a Slack/Discord bot POSTs JSON {project, body, ...} and
# the relay appends it to the project's LATEST report's comments — the SAME store the
# browser comment form writes to, so a chat reply is indistinguishable from a dashboard
# comment downstream. These tests pin the Bearer gate (NOT the browser's Basic+CSRF), the
# field validation + caps, latest-report resolution (and the optional report_id override),
# and that nothing lands in the store on any rejection.


def _post_json(base_url, obj, *, token=_TOKEN):
    """POST a JSON object to /api/comments and return (status, parsed-or-raw).

    Args:
        base_url: the server base URL.
        obj: a dict serialized to the JSON request body. Pass a non-dict/garbage via
            _post directly when exercising the malformed-body path.
        token: Bearer token to send, or None to omit the Authorization header.

    Why:
        Centralizes the machine comment-POST plumbing (JSON encoding + Bearer header)
        so each test states only the fields it exercises. Reuses _post for the actual
        request; decodes the JSON response when possible, else returns the raw text, so
        a test can assert on either an error message or the 201 body.
    """
    status, raw = _post(
        base_url, json.dumps(obj).encode("utf-8"), token=token, path="/api/comments"
    )
    try:
        return status, json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return status, raw.decode("utf-8")


def test_api_comment_post_stores_on_latest_report_and_returns_201(tmp_path):
    """A valid Bearer-authed JSON comment is stored on the project's latest report (201).

    Why this matters: this is the bot's happy path end to end — {project, body} with the
    right token lands a comment on the most recent report, and the 201 echoes both the new
    comment id and the report it attached to. We then read it back through the store to
    prove it actually persisted where the dashboard will render it.
    """
    with _running_relay(tmp_path) as (base_url, db):
        report_id = _ingest_one(base_url)
        status, payload = _post_json(
            base_url, {"project": "demo", "author": "Alex", "body": "From Slack."}
        )
        assert status == 201
        assert payload["report_id"] == report_id

        conn = open_relay_store(db)
        stored = comments_for(conn, report_id)
        assert [(c["author"], c["body"]) for c in stored] == [("Alex", "From Slack.")]


def test_api_comment_post_attaches_to_newest_when_multiple_reports(tmp_path):
    """With two reports for a project, a comment attaches to the NEWER one.

    Why this matters: "latest report per channel" is the slice's whole mapping model, so
    the resolution must pick the most recent report (history() is newest-first). We ingest
    two reports and assert the comment hangs off the second, not the first.
    """
    with _running_relay(tmp_path) as (base_url, db):
        first_id = _ingest_one(base_url)
        second_id = _ingest_one(base_url)
        assert second_id != first_id

        status, payload = _post_json(base_url, {"project": "demo", "body": "newest"})
        assert status == 201
        assert payload["report_id"] == second_id

        conn = open_relay_store(db)
        assert comments_for(conn, first_id) == []
        assert [c["body"] for c in comments_for(conn, second_id)] == ["newest"]


def test_api_comment_post_optional_report_id_targets_that_report(tmp_path):
    """An explicit report_id attaches the comment to THAT report, not the latest.

    Why this matters: report_id is the future-additive seam for reply-targeting. When the
    bot (later) sends it, the comment must land on the named report even though a newer one
    exists — proving the override path works and bypasses latest-resolution.
    """
    with _running_relay(tmp_path) as (base_url, db):
        first_id = _ingest_one(base_url)
        _ingest_one(base_url)  # a newer report that should be IGNORED when id is given

        status, payload = _post_json(
            base_url, {"project": "demo", "body": "on the old one", "report_id": first_id}
        )
        assert status == 201
        assert payload["report_id"] == first_id

        conn = open_relay_store(db)
        assert [c["body"] for c in comments_for(conn, first_id)] == ["on the old one"]


def test_api_comment_post_report_id_wrong_project_is_400(tmp_path):
    """A report_id that belongs to a different project is rejected 400, not stored.

    Why this matters: report_id is a cross-project handle, so the endpoint must confirm the
    named report actually belongs to the named project — otherwise a client could graft a
    comment onto an unrelated project's report by id. We only have one project here, so we
    use a mismatched project name against a real report id to exercise the guard.
    """
    with _running_relay(tmp_path) as (base_url, db):
        report_id = _ingest_one(base_url)
        status, _ = _post_json(
            base_url, {"project": "other", "body": "x", "report_id": report_id}
        )
        assert status == 400

        conn = open_relay_store(db)
        assert comments_for(conn, report_id) == []


def test_api_comment_post_wrong_token_is_401_and_stores_nothing(tmp_path):
    """A comment POST with the wrong Bearer token is 401 and stores nothing.

    Why this matters: the write path is gated by the same shared token as ingest. A bad
    token must be refused before the body is even parsed, leaving no trace — and the
    expected token must never appear in the response.
    """
    with _running_relay(tmp_path) as (base_url, db):
        report_id = _ingest_one(base_url)
        status, payload = _post_json(
            base_url, {"project": "demo", "body": "nope"}, token="wrong"
        )
        assert status == 401
        assert _TOKEN not in json.dumps(payload)

        conn = open_relay_store(db)
        assert comments_for(conn, report_id) == []


def test_api_comment_post_absent_token_is_401_with_bearer_scheme(tmp_path):
    """A comment POST with no Authorization header is 401 + WWW-Authenticate: Bearer.

    Why this matters: missing credentials are refused (never open by omission), and the
    401 advertises the Bearer scheme — the same standards-correct behavior as /ingest and
    GET /api/comments, so the three machine endpoints are consistent.
    """
    with _running_relay(tmp_path) as (base_url, _db):
        req = urllib.request.Request(
            base_url + "/api/comments",
            data=json.dumps({"project": "demo", "body": "x"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},  # no Authorization
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("expected a 401")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
            assert exc.headers.get("WWW-Authenticate") == "Bearer"


def test_api_comment_post_no_reports_for_project_is_404(tmp_path):
    """A comment for a project with no reports yet is 404, not a crash.

    Why this matters: a channel can be mapped before the project's first report exists.
    The endpoint has no report to attach to, so it returns a clean, actionable 404 rather
    than failing — and stores nothing.
    """
    with _running_relay(tmp_path) as (base_url, _db):
        status, _ = _post_json(base_url, {"project": "never-seen", "body": "hi"})
        assert status == 404


def test_api_comment_post_empty_body_is_400_and_stores_nothing(tmp_path):
    """A comment with a whitespace-only body is rejected 400 and stores nothing.

    Why this matters: a comment must carry text. The endpoint strips the body, so a
    whitespace-only value counts as empty and is caught at the boundary before the store —
    mirroring the browser comment route's discipline.
    """
    with _running_relay(tmp_path) as (base_url, db):
        report_id = _ingest_one(base_url)
        status, _ = _post_json(base_url, {"project": "demo", "body": "   "})
        assert status == 400

        conn = open_relay_store(db)
        assert comments_for(conn, report_id) == []


def test_api_comment_post_oversized_body_is_400(tmp_path):
    """A comment body over the cap is rejected 400 and stores nothing.

    Why this matters: we never trust a client-sent size. A body beyond
    MAX_COMMENT_BODY_CHARS is refused at the boundary, the same cap the browser route and
    the store render enforce, so no oversized text reaches the store.
    """
    with _running_relay(tmp_path) as (base_url, db):
        report_id = _ingest_one(base_url)
        too_long = "x" * (MAX_COMMENT_BODY_CHARS + 1)
        status, _ = _post_json(base_url, {"project": "demo", "body": too_long})
        assert status == 400

        conn = open_relay_store(db)
        assert comments_for(conn, report_id) == []


def test_api_comment_post_missing_body_field_is_400(tmp_path):
    """A payload missing the required `body` field is rejected 400.

    Why this matters: body is required; an absent field is a client error caught cleanly,
    not a KeyError. Pairs with the empty-body test (present-but-blank) to cover both shapes.
    """
    with _running_relay(tmp_path) as (base_url, _db):
        _ingest_one(base_url)
        status, _ = _post_json(base_url, {"project": "demo"})
        assert status == 400


def test_api_comment_post_omitted_author_stored_as_empty(tmp_path):
    """A comment with no author field is stored with an empty author string.

    Why this matters: author is optional (it is a self-entered label, not identity). A bot
    that cannot resolve a display name may omit it; the comment must still store cleanly
    with author == "", which the dashboard renders as an anonymous note.
    """
    with _running_relay(tmp_path) as (base_url, db):
        report_id = _ingest_one(base_url)
        status, _ = _post_json(base_url, {"project": "demo", "body": "anon"})
        assert status == 201

        conn = open_relay_store(db)
        assert comments_for(conn, report_id)[0]["author"] == ""


def test_api_comment_post_malformed_json_is_400(tmp_path):
    """A Bearer-authed but non-JSON body returns 400.

    Why this matters: even an authenticated bot can send garbage; the endpoint must reject
    an unparseable body cleanly rather than crash — mirroring /ingest's behavior.
    """
    with _running_relay(tmp_path) as (base_url, _db):
        status, _ = _post(base_url, b"not json at all", path="/api/comments")
        assert status == 400


def test_api_comment_post_does_not_disturb_other_routes(tmp_path):
    """Adding the POST /api/comments route leaves /ingest and GET /api/comments working.

    Why this matters: a new route on a shared handler can accidentally shadow others. This
    regression check proves the three machine endpoints coexist — a normal ingest still
    201s and the GET pull-back still reads back what the POST wrote.
    """
    with _running_relay(tmp_path) as (base_url, _db):
        report_id = _ingest_one(base_url)  # POST /ingest still works
        assert _post_json(base_url, {"project": "demo", "body": "loop"})[0] == 201

        # GET /api/comments (the pull-back) sees the comment the POST just wrote.
        code, raw = _get(base_url, _api_comments_path(project="demo"), bearer=_TOKEN)
        assert code == 200
        payload = json.loads(raw)
        assert [c["body"] for c in payload["comments"]] == ["loop"]
        assert payload["comments"][0]["report_id"] == report_id


# --- Multi-party auth: session-cookie + key-verifier crypto (Increment 1) ------
# Pure-function tests for the signed stateless session cookie and the peppered key
# verifier. These are the cryptographic core of the new auth, so they're pinned in
# isolation (no running server): a valid token round-trips, and every way a token can
# be wrong (tampered, expired, wrong key, wrong format) is rejected.

_SKEY = b"test-session-signing-key-32-bytes!!"
_PEPPER = b"test-user-pepper-secret"


def test_session_cookie_round_trips():
    """A freshly minted cookie verifies and returns its exact claims.

    Why this matters: this is the happy path every authenticated request depends on —
    the claims (sub, sv, exp) must survive sign→verify intact so the request path can
    resolve the user and check revocation.
    """
    value = make_session_value(_SKEY, user_id=7, session_version=3, issued_at=1000, expires_at=2000)
    claims = verify_session_value(_SKEY, value, now=1500)
    assert claims is not None
    assert claims["sub"] == 7 and claims["sv"] == 3 and claims["exp"] == 2000 and claims["v"] == 1


def test_session_cookie_tampered_signature_is_rejected():
    """Flipping any byte of the signature fails verification (HMAC integrity).

    Why this matters: the signature is what makes the stateless cookie unforgeable;
    a mutated signature must never validate.
    """
    value = make_session_value(_SKEY, 7, 1, 1000, 2000)
    payload_b64, sig = value.split(".", 1)
    tampered = f"{payload_b64}.{sig[:-1]}{'A' if sig[-1] != 'A' else 'B'}"
    assert verify_session_value(_SKEY, tampered, now=1500) is None


def test_session_cookie_tampered_payload_is_rejected():
    """Editing the claims (e.g. escalating sub) without re-signing fails.

    Why this matters: an attacker must not be able to swap in a different user id /
    session_version while keeping a stale signature.
    """
    value = make_session_value(_SKEY, 7, 1, 1000, 2000)
    _, sig = value.split(".", 1)
    forged_payload = _b64url_encode(b'{"v":1,"sub":1,"iat":1000,"exp":2000,"sv":1}')
    assert verify_session_value(_SKEY, f"{forged_payload}.{sig}", now=1500) is None


def test_session_cookie_expired_is_rejected_server_side():
    """A token past its `exp` is rejected even if otherwise valid (server-side expiry).

    Why this matters: expiry is enforced from the signed payload, not just the cookie's
    Max-Age, so a browser that keeps the cookie past expiry still cannot use it.
    """
    value = make_session_value(_SKEY, 7, 1, 1000, 2000)
    assert verify_session_value(_SKEY, value, now=2001) is None  # now > exp
    assert verify_session_value(_SKEY, value, now=2000) is not None  # exactly at exp still valid


def test_session_cookie_wrong_key_is_rejected():
    """A token signed with a different key does not verify (key isolation).

    Why this matters: rotating the signing key must invalidate every outstanding
    cookie, and a token forged under any other key must fail.
    """
    value = make_session_value(_SKEY, 7, 1, 1000, 2000)
    assert verify_session_value(b"a-different-signing-key", value, now=1500) is None


def test_session_cookie_unknown_format_version_is_rejected():
    """A correctly-signed token with an unknown format version is rejected.

    Why this matters: bumping the format version must invalidate every old cookie at
    once; the version gate is what makes that rotation possible.
    """
    payload_b64 = _b64url_encode(b'{"v":999,"sub":7,"iat":1000,"exp":2000,"sv":1}')
    value = f"{payload_b64}.{_sign(_SKEY, payload_b64)}"  # validly signed, wrong v
    assert verify_session_value(_SKEY, value, now=1500) is None


def test_session_cookie_garbage_is_rejected():
    """Malformed cookie values are rejected without raising.

    Why this matters: hostile or corrupt cookie input must degrade to "not
    authenticated", never crash the request handler.
    """
    for junk in ["", "no-dot", "a.b.c", "....", "%%%.%%%"]:
        assert verify_session_value(_SKEY, junk, now=1500) is None


def test_key_verifier_is_deterministic_and_secret_dependent():
    """The verifier is stable per (pepper, key) and changes with either.

    Why this matters: login recomputes the verifier from the presented key and looks
    it up, so it must be deterministic; and a DB leak (verifier) without the pepper
    must not let an attacker recompute/test candidate keys.
    """
    v = key_verifier(_PEPPER, "the-raw-key")
    assert v == key_verifier(_PEPPER, "the-raw-key")          # deterministic
    assert v != key_verifier(_PEPPER, "a-different-key")      # key-dependent
    assert v != key_verifier(b"different-pepper", "the-raw-key")  # pepper-dependent
    assert len(v) == 64  # SHA-256 hex


def test_mint_key_is_high_entropy_and_unique():
    """Minted keys are long, URL-safe, and unique across calls.

    Why this matters: the login credential's whole security rests on its entropy —
    server-minted ≥256-bit randomness is why no slow KDF is needed.
    """
    keys = {mint_key() for _ in range(50)}
    assert len(keys) == 50  # no collisions
    assert all(len(k) >= 40 for k in keys)  # token_urlsafe(32) is ~43 chars

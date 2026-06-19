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
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from orion.config import ProjectConfig, Recipient
from orion.report import build_report, serialize_blob
from relay.server import _is_loopback, create_server
from relay.store import get, list_projects, open_relay_store

_TOKEN = "test-ingest-token"
_VIEW = "test-view-secret"


@contextmanager
def _running_relay(tmp_path, token=_TOKEN, view_token=None):
    """Start a RelayServer on an ephemeral port in a thread; yield (base_url, db).

    Args:
        tmp_path: pytest temp dir for the relay's sqlite file.
        token: the shared ingest Bearer token.
        view_token: optional dashboard read secret. None (default) leaves GETs open,
            matching the loopback access model; set it to exercise read auth.

    Why:
        The ingest/read contracts can only be proven against a real server (real auth
        header parsing, real status codes). Binding to port 0 avoids port clashes;
        serving on a daemon thread lets the test make blocking HTTP calls from the
        main thread. The context manager guarantees the server is shut down even if
        an assertion fails.
    """
    db = tmp_path / "relay.sqlite3"
    server = create_server("127.0.0.1", 0, db, token, view_token)
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


def _get(base_url, path, *, password=None):
    """GET `path` from the relay and return (status_code, response_text).

    Args:
        base_url: the server's base URL.
        path: the request path.
        password: optional dashboard read secret to send as HTTP Basic auth (any
            username; None omits the Authorization header).

    Why:
        Mirrors _post for the read side, unwrapping HTTPError so a 401/404 comes back
        as a tuple to assert on. The password arg lets read-auth tests send (or omit)
        Basic credentials without each test rebuilding the request by hand.
    """
    headers = {}
    if password is not None:
        raw = base64.b64encode(f"orion:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {raw}"
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

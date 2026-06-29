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

import json
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from http.cookies import SimpleCookie
from pathlib import Path

import pytest

from orion.collectors.tasks import ChecklistItem
from orion.config import ProjectConfig, Recipient
from orion.report import build_report, serialize_blob
from relay.server import (
    _SESSION_COOKIE_NAME,
    AuthConfig,
    MAX_COMMENT_BODY_CHARS,
    ShowcaseConfig,
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
    add_discussion_item,
    add_user,
    bump_session_version,
    comments_for,
    discussion_items_for_project,
    get,
    get_checklist,
    get_disciplines,
    get_skills,
    list_projects,
    observed_history,
    open_relay_store,
    skills_projects,
)

_TOKEN = "test-ingest-token"
_VIEW = "test-view-secret"
# Independent test secrets for the multi-party auth: the cookie-signing key and the
# per-user-key pepper. Defined here (not just beside the crypto tests) because the
# running-relay helper builds a default AuthConfig from them so login works end to end.
_SKEY = b"test-session-signing-key-32-bytes!!"
_PEPPER = b"test-user-pepper-secret"
# The independent admin/provisioning token (POST/GET /api/users). Distinct from the
# ingest token (_TOKEN) on purpose — the ingest token must NOT be able to create users.
_ADMIN = "test-admin-token"


@contextmanager
def _running_relay(
    tmp_path,
    token=_TOKEN,
    view_token=None,
    require_view_auth=False,
    auth=None,
    public_origin=False,
    web_dir=None,
    showcase=None,
):
    """Start a RelayServer on an ephemeral port in a thread; yield (base_url, db).

    Args:
        tmp_path: pytest temp dir for the relay's sqlite file.
        token: the shared ingest Bearer token.
        view_token: optional dashboard read secret. None (default) leaves GETs open,
            matching the loopback access model; set it to gate the dashboard (with a
            view secret OR provisioned users, GETs require a session cookie).
        require_view_auth: force the view secret even on this loopback bind (the
            reverse-proxy topology); used to exercise that guard path.
        auth: an AuthConfig override. None (default) supplies a session-capable config
            built from the shared test secrets (_SKEY / _PEPPER), so POST /login works
            in every relay; pass a custom one to exercise secure_cookie, allow_legacy_admin,
            etc. Supplying a session key does NOT gate anything — gating is still driven
            by view_token/users — so the open-mode tests are unaffected.

    Why:
        The ingest/read contracts can only be proven against a real server (real auth
        header parsing, real status codes). Binding to port 0 avoids port clashes;
        serving on a daemon thread lets the test make blocking HTTP calls from the
        main thread. The context manager guarantees the server is shut down even if
        an assertion fails.
    """
    db = tmp_path / "relay.sqlite3"
    if auth is None:
        auth = AuthConfig(session_key=_SKEY, user_pepper=_PEPPER)
    server = create_server(
        "127.0.0.1", 0, db, token, view_token, require_view_auth, auth=auth,
        web_dir=web_dir, showcase=showcase,
    )
    _, port = server.server_address
    base_url = f"http://127.0.0.1:{port}"
    if public_origin:
        # Mirror the hosted deploy (e.g. Fly), where ORION_RELAY_PUBLIC_ORIGIN is set so
        # the comment CSRF check does an EXACT canonical-origin match instead of the Host
        # fallback. The port is only known now, so we inject the real base_url onto the
        # already-built server. (The prior origin tests never set this, so the production
        # exact-match path went untested — the gap that hid the no-referrer CSRF bug.)
        server.public_origin = base_url
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield base_url, db
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


def _get(base_url, path, *, cookie=None, bearer=None):
    """GET `path` from the relay and return (status_code, response_text).

    Args:
        base_url: the server's base URL.
        path: the request path.
        cookie: optional session-cookie value (from _login) sent as the dashboard
            credential. None omits the Cookie header (the unauthenticated case).
        bearer: optional Bearer token for the machine-JSON /api routes (None omits
            it). Distinct from `cookie` because the two surfaces use different auth
            schemes — a signed session cookie for the browser dashboard, Bearer for
            the pull-back API.

    Why:
        Mirrors _post for the read side, unwrapping HTTPError so a 303/401/404 comes
        back as a tuple to assert on. The cookie/bearer args let auth tests send (or
        omit) the right credential without each test rebuilding the request. Redirects
        are NOT followed (a no-redirect opener), so a 303 to /login surfaces here
        instead of being swallowed into the login page's 200.
    """
    headers = {}
    if cookie is not None:
        headers["Cookie"] = f"{_SESSION_COOKIE_NAME}={cookie}"
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    request = urllib.request.Request(base_url + path, headers=headers, method="GET")
    # A no-redirect opener so a 303 -> /login (the unauthenticated gate) surfaces as a
    # 303 here rather than being auto-followed to the login page's 200.
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _provision_user(db, name, key, *, role="viewer", projects=()):
    """Provision a real user (name + key verifier + scope) directly via the store.

    Args:
        db: the relay's sqlite path.
        name: the user's unique display name / handle.
        key: the raw login key (its HMAC verifier under _PEPPER is what gets stored).
        role: "viewer" (default) or "admin".
        projects: the viewer's allowed project names (ignored for an admin).

    Why:
        The login / authZ tests need a genuine relay_users row to authenticate against.
        We insert it through the same store helper the provisioning endpoint will use,
        computing the verifier exactly as the server does (HMAC under the shared
        _PEPPER), so the real login path resolves the presented key to this user.
    """
    conn = open_relay_store(db)
    try:
        return add_user(
            conn,
            name,
            key_verifier(_PEPPER, key),
            role,
            list(projects),
            "test",
            "2026-06-24T00:00:00+00:00",
        )
    finally:
        conn.close()


def _post_login(base_url, key):
    """POST /api/login with an access key; return (status, headers, text), no redirect follow.

    Args:
        base_url: the server's base URL.
        key: the raw access key to present.

    Why:
        The low-level login driver every login test shares. After the server-rendered HTML
        retired (KI-23), login is the JSON route POST /api/login: success replies 200 +
        Set-Cookie, a bad key replies 401 with {"ok": false} and no Set-Cookie. The JSON
        body carries the same-origin Origin header so the CSRF check passes. Returning the
        raw headers lets a test inspect the Set-Cookie attributes (HttpOnly / SameSite /
        Secure); returning the body lets it assert the rejection leaks no secret.
    """
    data = json.dumps({"key": key}).encode("utf-8")
    req = urllib.request.Request(
        base_url + "/api/login",
        data=data,
        headers={"Content-Type": "application/json", "Origin": base_url},
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=5) as response:
            return response.status, response.headers, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read().decode("utf-8")


def _login(base_url, key):
    """Log in and return the session-cookie value, or None when the key is rejected.

    Args:
        base_url: the server's base URL.
        key: the raw access key to present (a provisioned user's key, or the legacy view
            secret while bootstrapping).

    Returns:
        The orion_session cookie value on success, or None on a rejected key (the server
        replies 401 with no Set-Cookie).

    Why:
        The convenience wrapper most tests use: it drives the real login flow via
        _post_login and extracts just the cookie value, so a follow-up _get / _get_json /
        _post_api_json can present it. Tests that need the raw Set-Cookie attributes call
        _post_login.
    """
    _, headers, _ = _post_login(base_url, key)
    set_cookie = headers.get("Set-Cookie")
    if not set_cookie:
        return None
    jar = SimpleCookie()
    jar.load(set_cookie)
    morsel = jar.get(_SESSION_COOKIE_NAME)
    return morsel.value if morsel else None


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
        # A blob WITHOUT a checklist must not create a live checklist row (back-compat
        # with producers that predate the field — the optional path stays inert).
        assert get_checklist(conn, "demo") is None


def _blob_json_with_checklist(checklist):
    """A real serialized blob (build + serialize) carrying a given checklist.

    Why: mirrors _real_blob_json but exercises the optional checklist field through
    the production path, so the server test validates the EXACT bytes local Orion
    would push when a project has the live checklist enabled.
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
        generated_at="2026-06-18T00:00:00+00:00",
        sections=(("Code activity", "Shipped the seam."),),
        checklist=checklist,
    )
    return serialize_blob(blob)


def test_push_with_checklist_upserts_the_live_row(tmp_path):
    """A real push carrying a checklist stores it as the project's live checklist.

    Why this matters: this is the receiver half of the E2 Inc 2 contract end to end.
    The exact bytes local Orion pushes (build_report + serialize_blob with a
    checklist) are accepted, and get_checklist returns the items as {text, done}
    dicts in file order — the shape the renderer unpacks.
    """
    checklist = (
        ChecklistItem(text="Wire the relay", done=True),
        ChecklistItem(text="Render the dashboard", done=False),
    )
    with _running_relay(tmp_path) as (base_url, db):
        status, _body = _post(base_url, _blob_json_with_checklist(checklist).encode("utf-8"))
        assert status == 201

        conn = open_relay_store(db)
        assert get_checklist(conn, "demo") == [
            {"text": "Wire the relay", "done": True},
            {"text": "Render the dashboard", "done": False},
        ]


def test_empty_checklist_push_clears_the_live_row(tmp_path):
    """An enabled-but-empty checklist ([]) is stored as [], clearing prior items.

    Why this matters: () on the producer means "checklist enabled, no items now" and
    must overwrite a project's prior list rather than be ignored. We push items, then
    push an empty checklist, and assert the live row is now [] (not the stale items).
    """
    with _running_relay(tmp_path) as (base_url, db):
        first = (ChecklistItem(text="Old item", done=False),)
        assert _post(base_url, _blob_json_with_checklist(first).encode("utf-8"))[0] == 201
        # Now push an empty checklist (the file was cleared).
        assert _post(base_url, _blob_json_with_checklist(()).encode("utf-8"))[0] == 201

        conn = open_relay_store(db)
        assert get_checklist(conn, "demo") == []


def test_malformed_checklist_is_400_and_stores_nothing(tmp_path):
    """A checklist with a non-boolean 'done' is rejected 400 and nothing is stored.

    Why this matters: /ingest is an untrusted surface, so a malformed checklist must
    become a clean 400 at validation, never a later crash in store/render. We start
    from a valid blob and corrupt only the checklist, so the failure is attributable
    to the new validation, then confirm the store is untouched.
    """
    payload = json.loads(_real_blob_json())
    payload["checklist"] = [{"text": "ok", "done": "yes"}]  # 'done' must be a bool
    with _running_relay(tmp_path) as (base_url, db):
        status, body = _post(base_url, json.dumps(payload).encode("utf-8"))
        assert status == 400
        assert "checklist" in json.loads(body)["error"]

        conn = open_relay_store(db)
        assert list_projects(conn) == []          # no report row stored
        assert get_checklist(conn, "demo") is None  # and no checklist row either


# --- POST /checklist: the dedicated checklist-only push (near-real-time) ----------
# This endpoint upserts a project's live checklist WITHOUT a report, so an edited
# tasks_file can reach the dashboard between reports. It reuses the ingest Bearer token.


def _checklist_body(project="demo", items=None):
    """Serialize a {project, checklist} push body. items defaults to one of each state."""
    if items is None:
        items = [{"text": "Wire it", "done": True}, {"text": "Render it", "done": False}]
    return json.dumps({"project": project, "checklist": items}).encode("utf-8")


def test_checklist_push_upserts_without_a_report(tmp_path):
    """A valid POST /checklist stores the project's checklist and returns 200, no report.

    Why this matters: this is the core of the near-real-time path — the checklist
    reaches the dashboard with NO report row created. We push, assert 200, and confirm
    get_checklist returns the items while the reports table stays empty.
    """
    with _running_relay(tmp_path) as (base_url, db):
        status, body = _post(base_url, _checklist_body(), path="/checklist")
        assert status == 200
        assert json.loads(body) == {"updated": "demo", "items": 2}

        conn = open_relay_store(db)
        assert get_checklist(conn, "demo") == [
            {"text": "Wire it", "done": True},
            {"text": "Render it", "done": False},
        ]
        assert list_projects(conn) == []  # no report row was created


def test_checklist_push_replaces_prior_checklist(tmp_path):
    """A second /checklist push replaces the project's checklist (current state).

    Why this matters: the live checklist is current state, so each push overwrites the
    last. We push, then push a different list, and assert only the second survives.
    """
    with _running_relay(tmp_path) as (base_url, db):
        _post(base_url, _checklist_body(items=[{"text": "Old", "done": False}]), path="/checklist")
        _post(base_url, _checklist_body(items=[{"text": "New", "done": True}]), path="/checklist")

        conn = open_relay_store(db)
        assert get_checklist(conn, "demo") == [{"text": "New", "done": True}]


def test_checklist_push_records_an_observation_per_push(tmp_path):
    """Each /checklist push APPENDS to the observed-state history (E2 Inc 3 Unit 3).

    Why this matters: where the live checklist is replaced on each push, the forward-store
    accumulates — so the dashboard can later see history/slippage. We push the same item
    twice (open, then done) and confirm two observations landed under one stable item_key,
    keyed by the producer-supplied `key`, with the done-state change visible in order.
    """
    with _running_relay(tmp_path) as (base_url, db):
        item_open = [{"text": "App - Not started", "done": False, "key": "App"}]
        item_done = [{"text": "App - Submitted", "done": True, "key": "App"}]
        assert _post(base_url, _checklist_body(items=item_open), path="/checklist")[0] == 200
        assert _post(base_url, _checklist_body(items=item_done), path="/checklist")[0] == 200

        conn = open_relay_store(db)
        hist = observed_history(conn, "demo")
        # Two observations, one stable identity (the key survived the status/text change).
        assert [h["item_key"] for h in hist] == ["App", "App"]
        assert [h["done"] for h in hist] == [False, True]


def test_checklist_push_malformed_is_400_and_stores_nothing(tmp_path):
    """A malformed checklist item (non-bool 'done') is 400 and stores nothing.

    Why this matters: /checklist is an untrusted surface; a bad payload must be a clean
    400 at validation, never a later store/render crash.
    """
    bad = _checklist_body(items=[{"text": "x", "done": "yes"}])
    with _running_relay(tmp_path) as (base_url, db):
        status, body = _post(base_url, bad, path="/checklist")
        assert status == 400
        assert "checklist" in json.loads(body)["error"]
        conn = open_relay_store(db)
        assert get_checklist(conn, "demo") is None


def test_checklist_push_missing_project_is_400(tmp_path):
    """A /checklist push with no project field is rejected 400.

    Why this matters: project is the upsert key; without it there is nothing to key the
    checklist on, so the request is invalid by contract.
    """
    body = json.dumps({"checklist": [{"text": "x", "done": False}]}).encode("utf-8")
    with _running_relay(tmp_path) as (base_url, _db):
        status, resp = _post(base_url, body, path="/checklist")
        assert status == 400
        assert "project" in json.loads(resp)["error"]


def test_checklist_push_wrong_token_is_401(tmp_path):
    """A /checklist push with the wrong Bearer token is 401 and stores nothing.

    Why this matters: the endpoint reuses the ingest credential, so the same auth gate
    applies — an unauthenticated push must not write a checklist.
    """
    with _running_relay(tmp_path) as (base_url, db):
        status, _ = _post(base_url, _checklist_body(), token="wrong", path="/checklist")
        assert status == 401
        conn = open_relay_store(db)
        assert get_checklist(conn, "demo") is None


def test_push_checklist_client_round_trips_through_the_relay(tmp_path):
    """The push_checklist CLIENT posts a checklist the relay accepts and stores.

    Why this matters: this pins the client+server contract end to end — the exact call
    local Orion makes (delivery.relay.push_checklist) reaches /checklist, authenticates,
    and lands in the store as the live checklist. If the client's path/payload/auth ever
    drift from the endpoint, this breaks.
    """
    from orion.delivery.relay import push_checklist

    items = [{"text": "Ship it", "done": True}, {"text": "Polish", "done": False}]
    with _running_relay(tmp_path) as (base_url, db):
        # base_url is the relay root; the client derives /checklist via urljoin.
        push_checklist(base_url, "demo", items, _TOKEN)
        conn = open_relay_store(db)
        assert get_checklist(conn, "demo") == items


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


# --- C3: session lifecycle — login, cookie hygiene, expiry, revocation, logout ---
#
# The running-server counterparts to the pure-crypto cookie tests at the bottom of the
# file: these drive the REAL login flow against a live relay and pin the whole session
# surface — a valid cookie's security attributes, gating by provisioned users (not just a
# view secret), every way a session is refused (tampered / expired / revoked), logout,
# the bootstrap-admin gate, and Secure-only-when-hosted.


def test_login_success_sets_a_valid_cookie(tmp_path):
    """A successful login sets a session cookie that is HttpOnly, SameSite=Lax, and signed.

    Why this matters: the cookie's attributes ARE the session's security posture —
    HttpOnly (no JS theft via XSS), SameSite=Lax (CSRF defense-in-depth), Path=/ — and the
    value must be a real signature that verifies under the signing key, not an opaque blob.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, _db):
        code, headers, _ = _post_login(base_url, _VIEW)  # legacy bootstrap admin
        assert code == 200  # POST /api/login replies 200 + Set-Cookie (was a 303 form post)
        set_cookie = headers.get("Set-Cookie")
        assert set_cookie is not None
        low = set_cookie.lower()
        assert "httponly" in low and "samesite=lax" in low and "path=/" in low
        # The value is a genuine signed session that verifies under _SKEY.
        jar = SimpleCookie()
        jar.load(set_cookie)
        value = jar[_SESSION_COOKIE_NAME].value
        claims = verify_session_value(_SKEY, value, int(time.time()))
        assert claims is not None and claims["v"] == 1


def test_provisioned_viewer_can_login_and_view(tmp_path):
    """A provisioned user gates the dashboard (no view secret needed) and can log in to view.

    Why this matters: this is the real per-user flow — provisioning a user is itself enough
    to gate the dashboard (the access model is "controlled once anyone exists"), an
    unauthenticated request is refused (401 on a data API route), and the user's own key logs
    them in to the content. Proves identity end to end: store -> login -> authenticated GET.
    """
    with _running_relay(tmp_path) as (base_url, db):  # no view secret
        _ingest_one(base_url)
        _provision_user(db, "erin", "erin-key", projects=["demo"])
        # Provisioning a user gates the dashboard even with no view secret set.
        assert _get_json(base_url, "/api/portfolio")[0] == 401  # no session -> login required
        cookie = _login(base_url, "erin-key")
        assert cookie is not None
        assert _get_json(base_url, "/api/portfolio", cookie=cookie)[0] == 200  # logged in -> served


def test_tampered_cookie_is_rejected(tmp_path):
    """A session cookie with a flipped byte is refused — a gated data API route returns 401.

    Why this matters: the HMAC signature is what makes the stateless cookie unforgeable; a
    mutated cookie must never authenticate, and the live gate must turn that into a clean
    401 (login required), not an error or (worse) an authenticated request.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, _db):
        cookie = _login(base_url, _VIEW)
        tampered = cookie[:-1] + ("A" if cookie[-1] != "A" else "B")
        assert _get_json(base_url, "/api/portfolio", cookie=tampered)[0] == 401


def test_expired_cookie_is_rejected(tmp_path):
    """A session whose `exp` has passed is refused server-side (a gated data route 401s).

    Why this matters: expiry is enforced from the signed payload, not just the browser's
    Max-Age, so a client that keeps a cookie past its lifetime still cannot use it. We mint
    a validly-signed but already-expired cookie under the known key and confirm the gate.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, _db):
        now = int(time.time())
        expired = make_session_value(
            _SKEY, user_id=0, session_version=0, issued_at=now - 100, expires_at=now - 10
        )
        assert _get_json(base_url, "/api/portfolio", cookie=expired)[0] == 401


def test_revoked_session_is_rejected(tmp_path):
    """Bumping a user's session_version force-logs-out their live cookie on the next request.

    Why this matters: this is STATELESS revocation — the cookie carries the session_version
    it was minted with, and the per-request DB re-read rejects it once that no longer
    matches. A working session must stop working the instant the user is revoked, with no
    server-side session store.
    """
    with _running_relay(tmp_path) as (base_url, db):
        uid = _provision_user(db, "bob", "bob-key", projects=["demo"])
        cookie = _login(base_url, "bob-key")
        assert _get_json(base_url, "/api/portfolio", cookie=cookie)[0] == 200  # works before revocation
        conn = open_relay_store(db)
        try:
            bump_session_version(conn, uid)
        finally:
            conn.close()
        assert _get_json(base_url, "/api/portfolio", cookie=cookie)[0] == 401  # sv mismatch -> logged out


def test_logout_clears_cookie(tmp_path):
    """GET /logout redirects to /login and overwrites the session cookie with Max-Age=0.

    Why this matters: logout must actively clear the cookie (a Max-Age=0 Set-Cookie tells
    the browser to drop it), not merely redirect — otherwise the session would linger in
    the browser. We confirm both the redirect target and the clearing Set-Cookie.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, _db):
        cookie = _login(base_url, _VIEW)
        req = urllib.request.Request(
            base_url + "/logout",
            headers={"Cookie": f"{_SESSION_COOKIE_NAME}={cookie}"},
            method="GET",
        )
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            opener.open(req, timeout=5)
            raise AssertionError("expected a 303 redirect")
        except urllib.error.HTTPError as exc:
            assert exc.code == 303
            assert exc.headers.get("Location") == "/login"
            set_cookie = exc.headers.get("Set-Cookie")
        assert set_cookie is not None and "max-age=0" in set_cookie.lower()


def test_legacy_bootstrap_admin_only_when_no_users(tmp_path):
    """The legacy view key logs in as admin ONLY while no users exist; off once any do.

    Why this matters: the shared view secret is a bootstrap convenience, NOT a permanent
    peer to named users (Codex's gated/deprecated-legacy guidance). It must stop working
    the moment real users are provisioned, so it can't silently remain a backdoor.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        # No users yet: the legacy view key logs in (bootstrap admin).
        assert _login(base_url, _VIEW) is not None
        # Provision a user; the legacy key is now OFF.
        _provision_user(db, "carol", "carol-key")
        assert _login(base_url, _VIEW) is None
        # ...but the real user still logs in.
        assert _login(base_url, "carol-key") is not None


def test_legacy_admin_override_allows_after_users_exist(tmp_path):
    """With allow_legacy_admin, the legacy view key still works even after users exist.

    Why this matters: the deprecation is the default, but an operator can explicitly opt
    back in (the escape hatch). This pins that the override actually re-enables the legacy
    key, so the gate is a deliberate, configurable choice — not a hard lockout.
    """
    auth = AuthConfig(session_key=_SKEY, user_pepper=_PEPPER, allow_legacy_admin=True)
    with _running_relay(tmp_path, view_token=_VIEW, auth=auth) as (base_url, db):
        _provision_user(db, "dave", "dave-key")
        assert _login(base_url, _VIEW) is not None  # opt-in re-enables the legacy key


def test_secure_cookie_only_when_hosted(tmp_path):
    """The Secure attribute is set in a hosted posture and absent on a plain loopback dev relay.

    Why this matters: Secure stops the cookie from ever traveling over plaintext (essential
    once HTTPS-exposed), but it would break a bare http loopback dev relay (the browser
    would withhold the cookie). So it must track the deployment posture, not be hardcoded.
    """
    # Hosted posture (e.g. behind TLS): Secure is set.
    hosted = AuthConfig(session_key=_SKEY, user_pepper=_PEPPER, secure_cookie=True)
    with _running_relay(tmp_path, view_token=_VIEW, auth=hosted) as (base_url, _db):
        _, headers, _ = _post_login(base_url, _VIEW)
        assert "secure" in headers.get("Set-Cookie", "").lower()
    # Plain loopback dev (the default): no Secure, so the cookie works over http.
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, _db):
        _, headers, _ = _post_login(base_url, _VIEW)
        assert "secure" not in headers.get("Set-Cookie", "").lower()


# --- Hardening: HSTS rides every response in a hosted posture -------------------
#
# The relay is internet-facing, so the hardening headers (here HSTS) must track the
# deployment posture. This drives a live relay and reads the response headers directly
# (the _get helper drops them), the same manual-request pattern the auth tests use.


def _get_headers(base_url, path, *, cookie=None):
    """GET `path` and return (status, headers, text); redirects NOT followed.

    Args:
        base_url: the server's base URL.
        path: the request path.
        cookie: optional session-cookie value, sent as the dashboard credential.

    Why:
        The header-presence tests need the response HEADERS, which _get discards (it
        returns only code + text). This mirrors _post_login's manual-request pattern:
        build the request directly and return response.headers, with a no-redirect
        opener so any 3xx surfaces here instead of being auto-followed.
    """
    headers = {}
    if cookie is not None:
        headers["Cookie"] = f"{_SESSION_COOKIE_NAME}={cookie}"
    req = urllib.request.Request(base_url + path, headers=headers, method="GET")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=5) as response:
            return response.status, response.headers, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read().decode("utf-8")


def test_hsts_only_when_hosted(tmp_path):
    """Strict-Transport-Security is sent in a hosted HTTPS posture, absent on a loopback dev relay.

    Why this matters: HSTS forbids plain http to the origin — essential once HTTPS-exposed,
    but it would wedge a bare http loopback dev relay (the browser would then refuse to
    connect over http). So it must track the hosted signal (secure_cookie), exactly like
    the cookie's Secure attribute, not be hardcoded. Mirrors test_secure_cookie_only_when_hosted.
    """
    # Hosted posture (behind TLS): HSTS is sent. /api/me is always reachable and the HSTS
    # header rides every response, so it is the probe now that the HTML routes have retired.
    hosted = AuthConfig(session_key=_SKEY, user_pepper=_PEPPER, secure_cookie=True)
    with _running_relay(tmp_path, auth=hosted) as (base_url, _db):
        _, headers, _ = _get_headers(base_url, "/api/me")
        assert "max-age=63072000" in headers.get("Strict-Transport-Security", "")
    # Plain loopback dev (the default): no HSTS, so plain http to the relay still works.
    with _running_relay(tmp_path) as (base_url, _db):
        _, headers, _ = _get_headers(base_url, "/api/me")
        assert headers.get("Strict-Transport-Security") is None


# --- C3: authZ scoping — a viewer sees only granted projects (decision A: 404) ---
#
# Unit 3 layers per-project authorization on top of the session gate. An admin (and the
# legacy bootstrap admin, and an open relay) sees everything; a viewer is restricted to
# their granted projects. Out-of-scope access returns 404 — byte-identical to a genuinely
# missing resource — so a viewer cannot even learn that a project/report they lack a grant
# for EXISTS (the audience may include guests). Scope is re-read from the DB per request.


def _ingest_project(base_url, project):
    """Push one real report relabeled to `project`; return its id.

    Why:
        The scoping tests need reports across SEVERAL projects to prove a viewer sees only
        their grants. We reuse the real serialized blob (so the ingest contract still
        holds) and only swap the project field, the one dimension these tests vary.
    """
    blob = json.loads(_real_blob_json())
    blob["project"] = project
    status, body = _post(base_url, json.dumps(blob).encode("utf-8"))
    assert status == 201
    return json.loads(body)["id"]


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


def test_require_view_auth_with_secret_on_loopback_enforces_login(tmp_path):
    """With require_view_auth + a secret, a loopback relay starts AND gates the dashboard.

    Why this matters: this is the proxy topology done right — the guard passes (a secret
    is set), and the loopback dashboard still demands a session, so the proxy cannot
    expose an unauthenticated view. Proves both the guard and the enforcement (a data API
    route 401s without a session, 200s with one).
    """
    with _running_relay(tmp_path, view_token=_VIEW, require_view_auth=True) as (base_url, _db):
        assert _get_json(base_url, "/api/portfolio")[0] == 401  # no session -> login required
        cookie = _login(base_url, _VIEW)  # legacy bootstrap admin
        assert _get_json(base_url, "/api/portfolio", cookie=cookie)[0] == 200  # session -> served


# --- CSRF for the SPA comment write (POST /api/reports/<id>/comments) ------------
#
# The browser comment route retired with render.py (KI-23); the SPA now writes comments
# via the cookie-authed JSON route POST /api/reports/<id>/comments (success 201). Its
# happy path, auth, scope, and validation are covered by the test_api_report_comment_*
# tests below; these remaining cases pin the CSRF (Origin/Referer) edge behavior — the
# Origin-absent / opaque-"null" / public_origin paths — that those tests do not exercise.


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """An opener handler that does NOT follow redirects.

    Why:
        Some routes (e.g. GET /logout) reply with a 303. urllib follows 3xx automatically,
        which would swallow the 303 and return the redirected GET's 200. Returning None
        from redirect_request stops the follow, so the 303 + Location surface to the test
        (as an HTTPError we unwrap) instead of being hidden.
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


def test_comment_missing_origin_and_referer_is_403(tmp_path):
    """A logged-in comment POST with NEITHER Origin nor Referer is rejected 403.

    Why this matters: the CSRF guard verifies Origin OR Referer. A request that omits
    BOTH gives us nothing to check against our origin, so it must fail closed rather
    than be treated as same-origin by default. A valid session isolates the origin
    check as the thing under test.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        rid = _ingest_one(base_url)
        cookie = _login(base_url, _VIEW)
        status, _, _ = _post_api_json(
            base_url, f"/api/reports/{rid}/comments", {"body": "x"},
            cookie=cookie, origin=None,
        )
        assert status == 403

        conn = open_relay_store(db)
        assert comments_for(conn, rid) == []


def test_comment_missing_origin_with_matching_referer_is_allowed(tmp_path):
    """A same-origin comment that omits Origin but sends a matching Referer succeeds.

    Why this matters: this is the BUG FIX. Some browsers (notably Safari) omit the
    Origin header on a same-origin POST, so requiring Origin alone rejected a legitimate
    logged-in admin's comment with a "CSRF" 403. The Referer fallback accepts the request
    when the Referer's origin matches ours — a cross-site attacker cannot forge that. We
    assert the comment is accepted (201) and actually stored.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        rid = _ingest_one(base_url)
        cookie = _login(base_url, _VIEW)
        status, _, _ = _post_api_json(
            base_url, f"/api/reports/{rid}/comments", {"body": "Looks great!"},
            cookie=cookie, origin=None, referer="__match__",
        )
        assert status == 201

        conn = open_relay_store(db)
        stored = comments_for(conn, rid)
        assert len(stored) == 1
        assert stored[0]["body"] == "Looks great!"


def test_comment_missing_origin_with_foreign_referer_is_403(tmp_path):
    """A comment that omits Origin and sends a FOREIGN Referer is rejected 403.

    Why this matters: the Referer fallback must not become a CSRF hole. A request whose
    Referer points at someone else's origin is exactly the cross-site case the guard
    exists to stop, so it stays a 403 even with a valid session and nothing is stored.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        rid = _ingest_one(base_url)
        cookie = _login(base_url, _VIEW)
        status, _, _ = _post_api_json(
            base_url, f"/api/reports/{rid}/comments", {"body": "x"},
            cookie=cookie, origin=None, referer="https://evil.example/x",
        )
        assert status == 403

        conn = open_relay_store(db)
        assert comments_for(conn, rid) == []


# --- The no-referrer CSRF bug: the production exact-match path + opaque Origin -------
# The live bug: the dashboard sent `Referrer-Policy: no-referrer`, which makes a browser
# send a comment POST with `Origin: null` and NO Referer (Fetch standard). With a
# configured public_origin, the exact-match check then 403'd EVERY browser comment, and
# the Referer fallback could not help (the same policy stripped Referer). The fix is two
# parts: the response policy is now "same-origin" (so a real Origin is sent), and
# _origin_error treats a literal "null" Origin as opaque and falls back to Referer. These
# tests set public_origin, the production path the earlier origin tests never exercised.


def test_comment_with_public_origin_canonical_origin_is_allowed(tmp_path):
    """With public_origin set, a same-origin Origin matches exactly and the comment stores.

    Why this matters: this is the production (Fly) path — ORION_RELAY_PUBLIC_ORIGIN set,
    so the check is an EXACT canonical-origin match, not the Host fallback. The previous
    origin tests never set it, so this path was untested; under the fix a real same-origin
    Origin (what a browser now sends under the "same-origin" policy) is accepted.
    """
    with _running_relay(tmp_path, view_token=_VIEW, public_origin=True) as (base_url, db):
        rid = _ingest_one(base_url)
        cookie = _login(base_url, _VIEW)
        status, _, _ = _post_api_json(
            base_url, f"/api/reports/{rid}/comments", {"body": "Canonical."},
            cookie=cookie, origin="__match__",
        )
        assert status == 201
        conn = open_relay_store(db)
        assert comments_for(conn, rid)[0]["body"] == "Canonical."


def test_comment_with_public_origin_foreign_origin_is_403(tmp_path):
    """With public_origin set, a foreign Origin still fails the exact-match (CSRF holds).

    Why this matters: the exact-match must reject a cross-site Origin even with a valid
    session — the fix must not weaken the guard. Nothing is stored.
    """
    with _running_relay(tmp_path, view_token=_VIEW, public_origin=True) as (base_url, db):
        rid = _ingest_one(base_url)
        cookie = _login(base_url, _VIEW)
        status, _, _ = _post_api_json(
            base_url, f"/api/reports/{rid}/comments", {"body": "x"},
            cookie=cookie, origin="https://evil.example",
        )
        assert status == 403
        conn = open_relay_store(db)
        assert comments_for(conn, rid) == []


def test_comment_with_public_origin_opaque_null_origin_falls_back_to_referer(tmp_path):
    """A literal `Origin: null` with a matching Referer is accepted (opaque-origin path).

    Why this matters: this is the defense-in-depth half of the fix. "null" is how an
    opaque origin serializes; treating it as a real candidate (as the old code did) made
    it fail the exact match and 403 a legitimate same-origin POST. We now treat "null" as
    no-usable-Origin and fall back to the Referer — which, under the "same-origin" policy,
    a same-origin request still carries. Accepted (201) and stored.
    """
    with _running_relay(tmp_path, view_token=_VIEW, public_origin=True) as (base_url, db):
        rid = _ingest_one(base_url)
        cookie = _login(base_url, _VIEW)
        status, _, _ = _post_api_json(
            base_url, f"/api/reports/{rid}/comments", {"body": "Via referer."},
            cookie=cookie, origin="null", referer="__match__",
        )
        assert status == 201
        conn = open_relay_store(db)
        assert comments_for(conn, rid)[0]["body"] == "Via referer."


def test_comment_with_public_origin_null_origin_no_referer_is_403(tmp_path):
    """A literal `Origin: null` with NO Referer fails closed (no usable same-origin proof).

    Why this matters: the opaque-Origin fallback must not become a hole. With nothing to
    prove same-origin (null Origin AND no Referer — exactly the old no-referrer combo),
    the request is rejected 403 and nothing is stored.
    """
    with _running_relay(tmp_path, view_token=_VIEW, public_origin=True) as (base_url, db):
        rid = _ingest_one(base_url)
        cookie = _login(base_url, _VIEW)
        status, _, _ = _post_api_json(
            base_url, f"/api/reports/{rid}/comments", {"body": "x"},
            cookie=cookie, origin="null", referer=None,
        )
        assert status == 403
        conn = open_relay_store(db)
        assert comments_for(conn, rid) == []


def test_comment_open_when_no_view_secret(tmp_path):
    """With NO view secret and no users, a same-origin comment is accepted without a session.

    Why this matters: this pins the loopback-dev default — reads are open without a login,
    and so is commenting, keeping local use zero-friction. The Origin check still applies
    (it is not an auth control), so the request must be same-origin.
    """
    with _running_relay(tmp_path) as (base_url, db):  # view_token=None, no users
        rid = _ingest_one(base_url)
        status, _, _ = _post_api_json(
            base_url, f"/api/reports/{rid}/comments", {"body": "local note"}
        )  # no cookie, default same-origin
        assert status == 201

        conn = open_relay_store(db)
        assert [c["body"] for c in comments_for(conn, rid)] == ["local note"]


def test_comment_in_open_mode_uses_the_typed_name(tmp_path):
    """With no session (open/loopback), the typed free-text name is used as the author.

    Why this matters: the authenticated-identity override applies only when there IS an
    identity. On a bare loopback relay with no login, there is no account to attribute to,
    so the optional typed name stands — preserving the zero-friction local-dev behavior.
    """
    with _running_relay(tmp_path) as (base_url, db):  # no view token, no users -> open
        rid = _ingest_one(base_url)
        status, _, _ = _post_api_json(
            base_url, f"/api/reports/{rid}/comments",
            {"body": "local note", "author": "Casey"},
        )  # no cookie
        assert status == 201

        conn = open_relay_store(db)
        assert comments_for(conn, rid)[0]["author"] == "Casey"


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


# --- C3 (PR B): the admin API — POST/GET /api/users + revoke (admin-token gated) -
#
# The remote provisioning surface backing `relay-user`. Authed with the SEPARATE admin
# token (never the ingest token — the ingest token must not create users), it mints a
# per-user key (returned ONCE), lists users without credential material, and revokes
# (deactivate + force-logout). These tests pin the independent-secret gate, the field
# validation, that a created key actually logs in end to end, and that revoke is instant.


def _admin_auth():
    """An AuthConfig with provisioning ENABLED (admin token set), for the admin-API tests.

    Why:
        The admin API is off unless an admin token is configured. This bundles the
        session secrets (so a created user can then log in end to end) with the admin
        token, the posture a real provisioning-enabled relay runs with.
    """
    return AuthConfig(session_key=_SKEY, user_pepper=_PEPPER, admin_token=_ADMIN)


def _admin_post(base_url, path, obj, *, token=_ADMIN):
    """POST a JSON object to an admin route; return (status, parsed-or-raw).

    Why:
        Centralizes the admin JSON-POST plumbing (Bearer admin token + JSON body) so each
        test states only the fields it exercises. Reuses _post; decodes the JSON response
        when possible, else returns the raw text.
    """
    status, raw = _post(
        base_url, json.dumps(obj).encode("utf-8"), token=token, path=path
    )
    try:
        return status, json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return status, raw.decode("utf-8")


def test_create_user_returns_key_once_and_user_can_login(tmp_path):
    """POST /api/users mints a user + key; that key logs the user in end to end.

    Why this matters: this is provisioning's whole job — an admin creates a scoped user
    and gets back a one-time key that actually works. We assert the 201 echoes the stored
    identity (name/role/scope), returns a high-entropy key, and that the SAME key then
    authenticates a real login (proving mint -> verifier -> login round-trips).
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, _db):
        status, payload = _admin_post(
            base_url, "/api/users", {"name": "alice", "role": "viewer", "projects": ["demo"]}
        )
        assert status == 201
        assert payload["name"] == "alice"
        assert payload["role"] == "viewer"
        assert payload["projects"] == ["demo"]
        key = payload["key"]
        assert key and len(key) >= 40  # high-entropy minted key
        # The returned key authenticates a real login (end to end).
        assert _login(base_url, key) is not None


def test_create_user_rejects_the_ingest_token(tmp_path):
    """The INGEST token cannot create users — provisioning needs the admin token (401).

    Why this matters: independent secrets are the core hardening — whoever can push
    reports (the ingest token) must NOT be able to mint users. We present the ingest token
    and require a 401, no user created, and the admin token never echoed.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        status, payload = _admin_post(
            base_url, "/api/users", {"name": "mallory"}, token=_TOKEN
        )
        assert status == 401
        assert _ADMIN not in json.dumps(payload)  # admin token never leaked

        conn = open_relay_store(db)
        assert conn.execute("SELECT COUNT(*) FROM relay_users").fetchone()[0] == 0


def test_create_user_absent_token_is_401(tmp_path):
    """A create request with no Authorization header is refused 401, nothing created.

    Why this matters: provisioning is never open by omission — a missing credential is
    refused just like a wrong one, before any user is minted.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        status, _ = _admin_post(base_url, "/api/users", {"name": "x"}, token=None)
        assert status == 401
        conn = open_relay_store(db)
        assert conn.execute("SELECT COUNT(*) FROM relay_users").fetchone()[0] == 0


def test_create_user_when_provisioning_disabled_is_401(tmp_path):
    """With NO admin token configured, the create endpoint refuses every request (401).

    Why this matters: provisioning is OFF unless an admin token is set, so a relay that
    never enabled it cannot be coaxed into minting users — even by a client guessing a
    token. The default _running_relay has no admin token.
    """
    with _running_relay(tmp_path) as (base_url, _db):  # no admin token
        status, _ = _admin_post(base_url, "/api/users", {"name": "x"}, token=_ADMIN)
        assert status == 401


def test_create_user_duplicate_name_is_409_and_does_not_clobber(tmp_path):
    """Creating a second user with an existing name is a 409 — the first is untouched.

    Why this matters: a duplicate name must NOT overwrite the existing user's key or scope
    (that would silently change who can log in). We create "alice", attempt a second
    "alice", and require a 409 with still exactly one alice in the store.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        assert _admin_post(base_url, "/api/users", {"name": "alice"})[0] == 201
        status, _ = _admin_post(base_url, "/api/users", {"name": "alice"})
        assert status == 409

        conn = open_relay_store(db)
        count = conn.execute(
            "SELECT COUNT(*) FROM relay_users WHERE name = 'alice'"
        ).fetchone()[0]
        assert count == 1


def test_create_user_invalid_role_is_400(tmp_path):
    """A role outside the allowlist is rejected 400 (no unvalidated role reaches the store).

    Why this matters: relay_users.role is an open enum, so the endpoint — not the DB — is
    the gate. An unknown role like "superuser" must be a clean 400, never a stored value
    that later code might mis-handle as privileged.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, _db):
        status, _ = _admin_post(
            base_url, "/api/users", {"name": "x", "role": "superuser"}
        )
        assert status == 400


def test_create_user_missing_name_is_400(tmp_path):
    """A create payload without a non-empty name is rejected 400.

    Why this matters: name is the unique handle every later op (login lookup, revoke)
    keys on, so an absent or blank one is a client error caught at the boundary.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, _db):
        assert _admin_post(base_url, "/api/users", {"role": "viewer"})[0] == 400
        assert _admin_post(base_url, "/api/users", {"name": "   "})[0] == 400


def test_create_admin_role_user_sees_all_projects(tmp_path):
    """A provisioned admin (not the legacy key) logs in and sees every project.

    Why this matters: role "admin" must grant all-access through the SAME _allowed_projects
    path the legacy admin uses — proving the provisioned-admin path is real, not a special
    case of the bootstrap key. We create an admin, log in with its key, and see both projects.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, _db):
        _ingest_project(base_url, "alpha")
        _ingest_project(base_url, "beta")
        _, payload = _admin_post(base_url, "/api/users", {"name": "root", "role": "admin"})
        cookie = _login(base_url, payload["key"])
        code, out = _get_json(base_url, "/api/portfolio", cookie=cookie)
        assert code == 200
        names = [p["name"] for p in out["projects"]]
        assert "alpha" in names and "beta" in names  # admin sees every project


def test_list_users_excludes_credential_material(tmp_path):
    """GET /api/users lists users with their scope but NEVER any credential material.

    Why this matters: an admin listing must not surface the key_verifier (even hashed) or
    the raw key. We create a scoped user and confirm the listing shows name/role/projects
    but neither 'key' nor 'key_verifier'.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, _db):
        _admin_post(base_url, "/api/users", {"name": "alice", "projects": ["demo"]})
        code, body = _get(base_url, "/api/users", bearer=_ADMIN)
        assert code == 200
        users = json.loads(body)["users"]
        assert [u["name"] for u in users] == ["alice"]
        assert users[0]["projects"] == ["demo"]
        assert "key" not in users[0] and "key_verifier" not in users[0]


def test_list_users_requires_admin_token(tmp_path):
    """GET /api/users with the ingest token (not the admin token) is refused 401.

    Why this matters: the listing is part of the admin surface, gated by the same separate
    admin token — the ingest token must not read the user roster.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, _db):
        code, body = _get(base_url, "/api/users", bearer=_TOKEN)
        assert code == 401
        assert _ADMIN not in body


def test_revoke_user_forces_logout_and_blocks_relogin(tmp_path):
    """POST /api/users/revoke deactivates a user: their cookie dies and the key stops working.

    Why this matters: revocation is a settled Increment-1 requirement (decision C). A
    revoke must be INSTANT — a session already in a browser stops on its next request AND
    the key can no longer log in. We provision, log in, revoke, then confirm both.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, _db):
        _, payload = _admin_post(
            base_url, "/api/users", {"name": "bob", "projects": ["demo"]}
        )
        key = payload["key"]
        cookie = _login(base_url, key)
        assert _get_json(base_url, "/api/portfolio", cookie=cookie)[0] == 200  # works before revoke

        status, r = _admin_post(base_url, "/api/users/revoke", {"name": "bob"})
        assert status == 200 and r["revoked"] is True

        assert _get_json(base_url, "/api/portfolio", cookie=cookie)[0] == 401  # force-logged-out
        assert _login(base_url, key) is None  # key no longer authenticates


def test_revoke_unknown_user_is_404(tmp_path):
    """Revoking a name that does not exist is a clean 404.

    Why this matters: a typo'd or already-deleted name must be a clear not-found, not a
    crash — the admin gets an actionable error.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, _db):
        assert _admin_post(base_url, "/api/users/revoke", {"name": "ghost"})[0] == 404


def test_revoke_requires_admin_token(tmp_path):
    """Revoke with the ingest token (not the admin token) is refused 401, user untouched.

    Why this matters: revocation is a privileged state change, gated by the admin token.
    The ingest token must not be able to revoke anyone.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _admin_post(base_url, "/api/users", {"name": "bob"})
        status, _ = _admin_post(
            base_url, "/api/users/revoke", {"name": "bob"}, token=_TOKEN
        )
        assert status == 401
        # bob is still active.
        conn = open_relay_store(db)
        assert conn.execute(
            "SELECT active FROM relay_users WHERE name = 'bob'"
        ).fetchone()[0] == 1


def test_admin_actions_write_audit_rows(tmp_path):
    """Create and revoke each append an admin-audit row (the accountability trail).

    Why this matters: a multi-party access model needs a record of who provisioned/revoked
    whom. We create then revoke a user and confirm both actions are logged with the actor,
    action verb, and target.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _admin_post(base_url, "/api/users", {"name": "carol", "role": "viewer", "projects": ["demo"]})
        _admin_post(base_url, "/api/users/revoke", {"name": "carol"})

        conn = open_relay_store(db)
        rows = conn.execute(
            "SELECT actor, action, target_user FROM relay_admin_audit ORDER BY id"
        ).fetchall()
        assert [(r["actor"], r["action"], r["target_user"]) for r in rows] == [
            ("admin-token", "create_user", "carol"),
            ("admin-token", "revoke_user", "carol"),
        ]


# --- Multi-party auth: session-cookie + key-verifier crypto (Increment 1) ------
# Pure-function tests for the signed stateless session cookie and the peppered key
# verifier. These are the cryptographic core of the new auth, so they're pinned in
# isolation (no running server): a valid token round-trips, and every way a token can
# be wrong (tampered, expired, wrong key, wrong format) is rejected.
# (_SKEY / _PEPPER are defined at the top of the file, shared with the running-relay
# helper so the session/login tests sign and verify under the same secrets.)


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


# =============================================================================
# E2 Inc 4: the SPA's read-only JSON API (/api/me|portfolio|projects|reports) +
# the JSON auth siblings (POST /api/login, POST /api/logout). These cover the HTTP
# wiring — the cookie gate, scope, kind split, and CSRF — over the pure serializers
# already pinned in test_relay_api.py.
# =============================================================================


def _get_json(base_url, path, *, cookie=None):
    """GET an /api route and return (status, parsed-json-or-None)."""
    status, text = _get(base_url, path, cookie=cookie)
    try:
        return status, json.loads(text)
    except ValueError:
        return status, None


def _post_api_json(base_url, path, payload, *, cookie=None, origin="__match__", referer=None):
    """POST JSON to an /api route; return (status, parsed-json-or-text, headers).

    origin "__match__" sends a same-origin Origin (so the CSRF check passes); None omits
    it (to exercise the guard); any other string is sent verbatim (a foreign origin).
    referer "__match__" sends a same-origin Referer (base_url + path); None omits it (the
    default); any other string is sent verbatim. The referer arg lets the CSRF tests
    exercise the Origin-absent / Referer-fallback path.
    """
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if origin == "__match__":
        headers["Origin"] = base_url
    elif origin is not None:
        headers["Origin"] = origin
    if referer == "__match__":
        headers["Referer"] = base_url + path
    elif referer is not None:
        headers["Referer"] = referer
    if cookie is not None:
        headers["Cookie"] = f"{_SESSION_COOKIE_NAME}={cookie}"
    req = urllib.request.Request(base_url + path, data=data, headers=headers, method="POST")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=5) as resp:
            body = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(body), resp.headers
            except ValueError:
                return resp.status, body, resp.headers
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body), exc.headers
        except ValueError:
            return exc.code, body, exc.headers


def _push_checklist(base_url, project, items, *, kind="project"):
    """Push a checklist (with a kind) through the real /checklist endpoint."""
    body = json.dumps({"project": project, "checklist": items, "kind": kind}).encode("utf-8")
    return _post(base_url, body, path="/checklist")


def test_api_me_open_relay(tmp_path):
    """On an ungated relay /api/me reports not-gated, anonymous, unrestricted.

    Why this matters: the SPA must not force a login on an open loopback relay.
    """
    with _running_relay(tmp_path) as (base_url, _db):
        status, me = _get_json(base_url, "/api/me")
        assert status == 200
        assert me["gated"] is False and me["authenticated"] is False
        assert me["scope"] == {"unrestricted": True, "projects": None}


def test_api_routes_require_session_when_gated(tmp_path):
    """A gated relay answers data /api/* with 401 JSON (not a 303) when unauthenticated.

    Why this matters: the SPA's fetch needs a 401 to route itself to /login — a 303 to the
    HTML login page would be useless to a JSON client.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, _db):
        for path in ("/api/portfolio", "/api/projects/orion", "/api/reports/1", "/api/scheduling"):
            status, body = _get_json(base_url, path)
            assert status == 401, path
            assert body == {"error": "login required"}


def test_api_me_is_reachable_unauthenticated_on_a_gated_relay(tmp_path):
    """/api/me always returns 200 — even gated + unauthenticated — so the SPA learns to log in.

    Why this matters: /api/me is the boot probe. If it 401'd like the data routes, the SPA
    could not distinguish "needs login" from "relay down" — it must answer with the state.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, _db):
        status, me = _get_json(base_url, "/api/me")
        assert status == 200
        assert me["gated"] is True and me["authenticated"] is False
        assert me["identity"] is None


def test_api_me_reflects_admin_and_viewer_scope(tmp_path):
    """With a session, /api/me carries identity and the right scope per role."""
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        _provision_user(db, "Yusuf", "admin-key", role="admin")
        _provision_user(db, "Mum", "mum-key", role="viewer", projects=["orion"])

        _, admin_me = _get_json(base_url, "/api/me", cookie=_login(base_url, "admin-key"))
        assert admin_me["identity"] == {"name": "Yusuf", "role": "admin"}
        assert admin_me["scope"]["unrestricted"] is True

        _, viewer_me = _get_json(base_url, "/api/me", cookie=_login(base_url, "mum-key"))
        assert viewer_me["identity"] == {"name": "Mum", "role": "viewer"}
        assert viewer_me["scope"] == {"unrestricted": False, "projects": ["orion"]}


def test_api_portfolio_splits_projects_and_trackers(tmp_path):
    """/api/portfolio puts a report-project under projects and a kind=tracker under trackers."""
    with _running_relay(tmp_path) as (base_url, _db):
        # A real project: push a report blob (the real blob's project is "demo").
        _post(base_url, _real_blob_json().encode("utf-8"))
        # A tracker: push a checklist marked kind=tracker.
        _push_checklist(
            base_url, "applications",
            [{"text": "Apply somewhere", "done": False, "due_date": "2026-12-01"}],
            kind="tracker",
        )
        status, out = _get_json(base_url, "/api/portfolio")
        assert status == 200
        assert "applications" in [t["name"] for t in out["trackers"]]
        # The blob's project lands under projects, never under trackers.
        assert "demo" in [p["name"] for p in out["projects"]]
        assert "applications" not in [p["name"] for p in out["projects"]]
        tracker = next(t for t in out["trackers"] if t["name"] == "applications")
        assert tracker["kind"] == "tracker"
        assert "segments" in tracker and "at_risk_items" in tracker


def test_api_portfolio_is_scoped_for_a_viewer(tmp_path):
    """A scoped viewer only sees granted projects in /api/portfolio."""
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        _post(base_url, _real_blob_json().encode("utf-8"))  # project "demo"
        _push_checklist(base_url, "applications", [{"text": "x", "done": False}], kind="tracker")
        _provision_user(db, "Mum", "mum-key", role="viewer", projects=["demo"])

        _, out = _get_json(base_url, "/api/portfolio", cookie=_login(base_url, "mum-key"))
        names = [p["name"] for p in out["projects"]] + [t["name"] for t in out["trackers"]]
        assert names == ["demo"]  # the tracker is out of scope
        assert out["scope"] == {"unrestricted": False, "projects": ["demo"]}


def test_api_project_detail_and_missing_404(tmp_path):
    """/api/projects/<name> returns detail for a real project, 404 for an unknown one."""
    with _running_relay(tmp_path) as (base_url, _db):
        _post(base_url, _real_blob_json().encode("utf-8"))
        status, detail = _get_json(base_url, "/api/projects/demo")
        assert status == 200 and detail["name"] == "demo"
        assert "stats" in detail and "checklist" in detail and "reports" in detail

        missing, body = _get_json(base_url, "/api/projects/ghost")
        assert missing == 404 and body == {"error": "not found"}


def test_api_report_detail_carries_nav_and_404s_unknown(tmp_path):
    """/api/reports/<id> returns the report with nav; an unknown id is 404."""
    with _running_relay(tmp_path) as (base_url, _db):
        created = json.loads(_post(base_url, _real_blob_json().encode("utf-8"))[1])
        report_id = created["id"]
        status, detail = _get_json(base_url, f"/api/reports/{report_id}")
        assert status == 200 and detail["id"] == report_id
        assert detail["number"] == 1  # per-project ordinal: the only report is #1
        # The only report has no neighbours; ids route, numbers label (both null at the ends).
        assert detail["nav"] == {
            "prev_id": None, "prev_number": None, "next_id": None, "next_number": None
        }

        missing, body = _get_json(base_url, "/api/reports/999999")
        assert missing == 404 and body == {"error": "not found"}


def test_api_login_sets_cookie_and_rejects_bad_key(tmp_path):
    """POST /api/login returns {ok,user}+Set-Cookie on a good key, 401 {ok:false} on a bad one."""
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        _provision_user(db, "Yusuf", "admin-key", role="admin")

        status, body, headers = _post_api_json(base_url, "/api/login", {"key": "admin-key"})
        assert status == 200
        assert body == {"ok": True, "user": {"name": "Yusuf", "role": "admin"}}
        assert _SESSION_COOKIE_NAME in (headers.get("Set-Cookie") or "")

        bad, bad_body, _ = _post_api_json(base_url, "/api/login", {"key": "wrong"})
        assert bad == 401 and bad_body == {"ok": False}


def test_api_login_minted_cookie_authenticates_me(tmp_path):
    """The cookie from /api/login authenticates a subsequent /api/me as that user."""
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        _provision_user(db, "Yusuf", "admin-key", role="admin")
        _, _body, headers = _post_api_json(base_url, "/api/login", {"key": "admin-key"})
        jar = SimpleCookie()
        jar.load(headers["Set-Cookie"])
        cookie = jar[_SESSION_COOKIE_NAME].value

        _, me = _get_json(base_url, "/api/me", cookie=cookie)
        assert me["authenticated"] is True
        assert me["identity"] == {"name": "Yusuf", "role": "admin"}


def test_api_login_rejects_foreign_origin(tmp_path):
    """A cross-origin POST /api/login is refused 403 (the CSRF guard)."""
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        _provision_user(db, "Yusuf", "admin-key", role="admin")
        status, _body, _h = _post_api_json(
            base_url, "/api/login", {"key": "admin-key"}, origin="https://evil.example"
        )
        assert status == 403


def test_api_logout_clears_cookie(tmp_path):
    """POST /api/logout returns {ok:true} and a cookie-clearing Set-Cookie."""
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, _db):
        status, body, headers = _post_api_json(base_url, "/api/logout", {})
        assert status == 200 and body == {"ok": True}
        assert _SESSION_COOKIE_NAME in (headers.get("Set-Cookie") or "")


# =============================================================================
# E2 Inc 4 (4a.5): serving the built SPA single-host via --web-dir. The relay serves
# static assets + an index.html fallback for client-side routes, with the SPA CSP and a
# path-traversal guard; the legacy server-rendered HTML is bypassed when web_dir is set.
# =============================================================================


def _build_web_dir(tmp_path):
    """Create a fake built-SPA dir (index.html + a hashed asset) for serving tests."""
    web = tmp_path / "dist"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text(
        "<!doctype html><title>Orion</title><div id=root></div>", encoding="utf-8"
    )
    (web / "assets" / "index-abc123.js").write_text("console.log('spa')", encoding="utf-8")
    return web


def test_spa_index_served_for_client_routes(tmp_path):
    """With --web-dir, "/" and unknown GET paths return index.html with the SPA CSP.

    Why this matters: the SPA owns client-side routes (/, /project/x, /report/1); the
    server must return index.html for them so the React router renders, and tag it with the
    strict-script SPA CSP (not the legacy hash-based policy).
    """
    web = _build_web_dir(tmp_path)
    with _running_relay(tmp_path, web_dir=web) as (base_url, _db):
        for path in ("/", "/project/orion", "/report/5", "/login"):
            req = urllib.request.Request(base_url + path, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 200, path
                assert b"id=root" in resp.read()
                csp = resp.headers["Content-Security-Policy"]
                assert "script-src 'self'" in csp and "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]


def test_spa_static_asset_served_with_immutable_cache(tmp_path):
    """A hashed asset under /assets/ is served with its content-type + an immutable cache."""
    web = _build_web_dir(tmp_path)
    with _running_relay(tmp_path, web_dir=web) as (base_url, _db):
        req = urllib.request.Request(base_url + "/assets/index-abc123.js", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            assert "javascript" in resp.headers["Content-Type"]
            assert "immutable" in resp.headers["Cache-Control"]
            assert resp.read() == b"console.log('spa')"


def test_spa_path_traversal_is_blocked(tmp_path):
    """A "../"-escape outside web_dir is NOT served as a file (falls back to the SPA index).

    Why this matters: the static handler must never read a file outside the asset root. A
    traversal attempt resolves outside web_dir, fails the containment check, and is treated
    as a client-route miss → index.html (200), never the escaped file's bytes.
    """
    web = _build_web_dir(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")
    with _running_relay(tmp_path, web_dir=web) as (base_url, _db):
        # urllib normalizes "../" in the client, so hit the handler with a raw encoded path.
        req = urllib.request.Request(base_url + "/assets/..%2f..%2fsecret.txt", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read()
            assert b"TOP SECRET" not in body  # the escaped file was NOT served
            assert b"id=root" in body  # served the SPA index instead


def test_api_still_json_when_web_dir_set(tmp_path):
    """/api/* keeps returning JSON (not the SPA index) when serving the SPA.

    Why this matters: the SPA-fallback must not swallow the API. /api/me is matched before
    the static handler, so it still answers JSON.
    """
    web = _build_web_dir(tmp_path)
    with _running_relay(tmp_path, web_dir=web) as (base_url, _db):
        status, me = _get_json(base_url, "/api/me")
        assert status == 200 and me["gated"] is False


def test_no_web_dir_is_api_only(tmp_path):
    """Without --web-dir the relay is API-only/headless: "/" 404s JSON; /api/me still 200s.

    Why this matters: the legacy server-rendered HTML retired (KI-23), so a relay started
    without --web-dir has no browser front-end to serve. A browser GET to "/" gets a clean
    JSON 404, while the JSON API (here /api/me, the boot probe) keeps answering.
    """
    with _running_relay(tmp_path) as (base_url, _db):
        status, body = _get_json(base_url, "/")
        assert status == 404
        assert body == {"error": "not found"}
        assert _get_json(base_url, "/api/me")[0] == 200


def test_api_scheduling_buckets_deadlines_across_sources(tmp_path):
    """/api/scheduling returns {summary, buckets} and time-buckets open dated items.

    Why this matters: this pins the endpoint end to end — routing, the cross-project
    enumeration, and the bucketing — against a running relay. Dates are computed relative to
    today (the relay buckets in its display zone) with wide margins so a TZ-boundary day-off
    can't reclassify them.
    """
    from datetime import date, timedelta

    iso = lambda days: (date.today() + timedelta(days=days)).isoformat()
    with _running_relay(tmp_path) as (base_url, _db):
        _push_checklist(
            base_url, "applications",
            [
                {"text": "Overdue app", "done": False, "due_date": iso(-5)},
                {"text": "Soon app", "done": False, "due_date": iso(3)},
                {"text": "Far app", "done": False, "due_date": iso(30)},
                {"text": "No date", "done": False},          # excluded
                {"text": "Done app", "done": True, "due_date": iso(-2)},  # excluded
            ],
            kind="tracker",
        )
        status, out = _get_json(base_url, "/api/scheduling")
        assert status == 200
        assert set(out) == {"summary", "buckets"}
        assert set(out["buckets"]) == {"overdue", "this_week", "later"}

        assert [r["label"] for r in out["buckets"]["overdue"]] == ["Overdue app"]
        assert [r["label"] for r in out["buckets"]["this_week"]] == ["Soon app"]
        assert [r["label"] for r in out["buckets"]["later"]] == ["Far app"]
        # Source tag carries the tracker's kind so the SPA renders ⊟.
        assert out["buckets"]["overdue"][0]["source"] == {"name": "applications", "kind": "tracker"}
        assert out["summary"]["overdue"] == 1 and out["summary"]["due_this_week"] == 1


def test_api_scheduling_is_scoped_for_a_viewer(tmp_path):
    """A scoped viewer's scheduling only aggregates deadlines from granted projects."""
    from datetime import date, timedelta

    overdue = (date.today() - timedelta(days=5)).isoformat()
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        _push_checklist(base_url, "granted", [{"text": "Mine", "done": False, "due_date": overdue}])
        _push_checklist(base_url, "secret", [{"text": "Hidden", "done": False, "due_date": overdue}])
        _provision_user(db, "Mum", "mum-key", role="viewer", projects=["granted"])

        _, out = _get_json(base_url, "/api/scheduling", cookie=_login(base_url, "mum-key"))
        labels = [r["label"] for b in out["buckets"].values() for r in b]
        assert labels == ["Mine"]  # the out-of-scope project's deadline never appears


# --- GET /api/showcase: the public, no-login curated surface --------------------------


def test_api_showcase_404_when_disabled(tmp_path):
    """With no --showcase config the endpoint 404s — and does so UNAUTHENTICATED.

    Why this matters: the public surface must be off by default (existence-hiding), and the
    404 has to come back to an anonymous caller — confirming the route is reached before the
    login gate, not after it (a 401 here would leak that the surface exists but is gated).
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, _db):
        _push_checklist(base_url, "demo", [{"text": "x", "done": False}])
        status, body = _get_json(base_url, "/api/showcase")  # no cookie
        assert status == 404
        assert body == {"error": "not found"}


def test_api_showcase_public_serves_only_the_allowlist(tmp_path):
    """Enabled showcase serves the curated allowlist to an ANONYMOUS caller, and nothing else.

    Why this matters: this is the privacy contract end-to-end. A gated relay (view_token set)
    still answers /api/showcase with no session, but the body carries ONLY allowlisted
    projects — a non-listed project must never appear — and only the summary card fields.
    """
    showcase = ShowcaseConfig(enabled=True, projects=(("demo", "A curated demo blurb."),))
    with _running_relay(tmp_path, view_token=_VIEW, showcase=showcase) as (base_url, _db):
        # Two projects exist in the store; only "demo" is on the allowlist.
        _push_checklist(base_url, "demo", [{"text": "Ship it", "done": True}])
        _push_checklist(base_url, "secret", [{"text": "Hidden", "done": False}])

        status, out = _get_json(base_url, "/api/showcase")  # no cookie — public
        assert status == 200
        names = [c["name"] for c in out["projects"]]
        assert names == ["demo"]  # "secret" is not allowlisted → absent
        card = out["projects"][0]
        assert card["description"] == "A curated demo blurb."  # the curated blurb wins
        assert card["status"] == "shipped"  # 1/1 done
        assert set(card) == {"name", "description", "status", "progress", "report_count"}


def test_api_me_showcase_enabled_tracks_config(tmp_path):
    """/api/me reports showcase_enabled matching the server config (drives the sidebar link)."""
    off = ShowcaseConfig(enabled=False)
    with _running_relay(tmp_path, showcase=off) as (base_url, _db):
        _, me = _get_json(base_url, "/api/me")
        assert me["showcase_enabled"] is False
    on = ShowcaseConfig(enabled=True, projects=(("demo", ""),))
    with _running_relay(tmp_path, showcase=on) as (base_url, _db):
        _, me = _get_json(base_url, "/api/me")
        assert me["showcase_enabled"] is True


# --- POST /api/reports/<id>/comments: the SPA's cookie-authed JSON comment write -------


def test_api_report_comment_stores_and_returns_created(tmp_path):
    """Authed + same-origin JSON comment → 201, stored, attributed to the session identity.

    Why this matters: the happy path of the SPA write. The returned shape matches the read
    path (_comment) so the SPA appends it directly. A logged-in user CANNOT post under
    another name — the client-supplied author is ignored in favour of the session identity.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        report_id = _ingest_one(base_url)
        cookie = _login(base_url, _VIEW)  # legacy bootstrap admin (no users yet)

        status, body, _ = _post_api_json(
            base_url, f"/api/reports/{report_id}/comments",
            {"body": "Ship it.", "author": "Mallory"}, cookie=cookie,
        )
        assert status == 201
        assert body["body"] == "Ship it." and body["role"] is None
        assert body["author"] == "legacy-admin"  # session identity, NOT the spoofed "Mallory"
        assert isinstance(body["id"], int) and body["created_at"]

        conn = open_relay_store(db)
        stored = comments_for(conn, report_id)
        assert len(stored) == 1
        assert stored[0]["author"] == "legacy-admin" and stored[0]["body"] == "Ship it."


def test_api_report_comment_requires_session_when_gated(tmp_path):
    """A gated relay rejects an unauthenticated comment write with 401 (and stores nothing)."""
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        report_id = _ingest_one(base_url)
        status, body, _ = _post_api_json(
            base_url, f"/api/reports/{report_id}/comments", {"body": "hi"}  # no cookie
        )
        assert status == 401 and body == {"error": "login required"}
        conn = open_relay_store(db)
        assert comments_for(conn, report_id) == []


def test_api_report_comment_rejects_foreign_origin(tmp_path):
    """A cross-site Origin is rejected with 403 even with a valid session (CSRF guard)."""
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        report_id = _ingest_one(base_url)
        cookie = _login(base_url, _VIEW)
        status, _body, _ = _post_api_json(
            base_url, f"/api/reports/{report_id}/comments", {"body": "x"},
            cookie=cookie, origin="https://evil.example",
        )
        assert status == 403
        conn = open_relay_store(db)
        assert comments_for(conn, report_id) == []


def test_api_report_comment_404_out_of_scope_and_missing(tmp_path):
    """An out-of-scope report and a missing id both 404 (existence-hiding), storing nothing."""
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        report_id = _ingest_one(base_url)  # project "demo"
        _provision_user(db, "Mum", "mum-key", role="viewer", projects=["other"])  # not demo
        cookie = _login(base_url, "mum-key")

        s1, b1, _ = _post_api_json(
            base_url, f"/api/reports/{report_id}/comments", {"body": "x"}, cookie=cookie
        )
        assert s1 == 404 and b1 == {"error": "not found"}  # out of scope == missing
        s2, _, _ = _post_api_json(
            base_url, "/api/reports/999999/comments", {"body": "x"}, cookie=cookie
        )
        assert s2 == 404
        conn = open_relay_store(db)
        assert comments_for(conn, report_id) == []


def test_api_report_comment_rejects_empty_and_oversized_body(tmp_path):
    """A whitespace-only body and an over-cap body are both 400 (and store nothing)."""
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        report_id = _ingest_one(base_url)
        cookie = _login(base_url, _VIEW)

        s_empty, _, _ = _post_api_json(
            base_url, f"/api/reports/{report_id}/comments", {"body": "   "}, cookie=cookie
        )
        assert s_empty == 400
        s_big, _, _ = _post_api_json(
            base_url, f"/api/reports/{report_id}/comments",
            {"body": "x" * (MAX_COMMENT_BODY_CHARS + 1)}, cookie=cookie,
        )
        assert s_big == 400
        conn = open_relay_store(db)
        assert comments_for(conn, report_id) == []


# --- POST /api/discussions/<project>/items: the supervisor-interaction loop (E2 Inc 5) ---
# The SPA's cookie-authed discussion write. Unlike the comment write, identity is fully
# server-derived (a viewer cannot post; a client-supplied author/role is ignored) and the
# project — not a report — is the thread anchor. The read folds into GET /api/projects/<name>.


def test_api_discussion_supervisor_post_stores_and_returns_created(tmp_path):
    """A supervisor's same-origin post → 201 as role 'supervisor', attributed to the session.

    Why this matters: the happy path of the loop's human-write surface. The supervisor is a
    scoped principal; the returned shape matches the read path (_discussion_item) so the SPA
    appends it directly. Crucially, a client-supplied author/role/author_id is IGNORED in
    favour of the server-derived identity — a poster cannot forge who they are.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        _ingest_one(base_url)  # creates project "demo"
        uid = _provision_user(db, "Dad", "dad-key", role="supervisor", projects=["demo"])
        cookie = _login(base_url, "dad-key")

        status, body, _ = _post_api_json(
            base_url, "/api/discussions/demo/items",
            # Spoofed attribution in the body — every field here must be ignored.
            {"body": "How's the auth slice?", "author": "Mallory",
             "role": "developer", "author_id": 999},
            cookie=cookie,
        )
        assert status == 201
        assert body["body"] == "How's the auth slice?"
        assert body["role"] == "supervisor"          # derived from the principal, not the body
        assert body["author_name"] == "Dad"          # session identity, not the spoofed name
        assert "author_id" not in body               # internal id is not on the wire
        assert isinstance(body["id"], int) and body["created_at"]

        conn = open_relay_store(db)
        stored = discussion_items_for_project(conn, "demo")
        assert len(stored) == 1
        assert stored[0]["author_id"] == uid         # real principal id, not the spoofed 999
        assert stored[0]["author_name"] == "Dad" and stored[0]["role"] == "supervisor"


def test_api_discussion_admin_posts_as_developer(tmp_path):
    """The admin/owner (here the legacy bootstrap admin) posts as role 'developer'.

    Why this matters: this is the mapping that lets the developer hold up their half of the
    thread from the dashboard (admin → 'developer'), so the loop is demonstrable end-to-end
    before the Unit 3 CLI path. The legacy admin has no relay_users row, so author_id is None
    — which the nullable column must store cleanly.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        _ingest_one(base_url)
        cookie = _login(base_url, _VIEW)  # legacy bootstrap admin (no users provisioned)

        status, body, _ = _post_api_json(
            base_url, "/api/discussions/demo/items", {"body": "Auth slice landed."},
            cookie=cookie,
        )
        assert status == 201
        assert body["role"] == "developer" and body["author_name"] == "legacy-admin"

        conn = open_relay_store(db)
        stored = discussion_items_for_project(conn, "demo")
        assert stored[0]["author_id"] is None and stored[0]["role"] == "developer"


def test_api_discussion_viewer_is_403(tmp_path):
    """A plain viewer has no thread standing: their post is 403 and stores nothing.

    Why this matters: a read-only family member can view the dashboard but is not a
    participant in the discussion. The role→403 gate is the authorization boundary that
    keeps the loop to supervisors and the developer.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        _ingest_one(base_url)
        _provision_user(db, "Kid", "kid-key", role="viewer", projects=["demo"])
        cookie = _login(base_url, "kid-key")

        status, body, _ = _post_api_json(
            base_url, "/api/discussions/demo/items", {"body": "let me in"}, cookie=cookie
        )
        assert status == 403
        conn = open_relay_store(db)
        assert discussion_items_for_project(conn, "demo") == []


def test_api_discussion_requires_session_when_gated(tmp_path):
    """A gated relay rejects an unauthenticated discussion post with 401 (stores nothing).

    Why this matters: a discussion entry is attributable by definition, so unlike a comment
    there is no anonymous/open-loopback path — no session means 401, full stop.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        _ingest_one(base_url)
        status, body, _ = _post_api_json(
            base_url, "/api/discussions/demo/items", {"body": "hi"}  # no cookie
        )
        assert status == 401 and body == {"error": "login required"}
        conn = open_relay_store(db)
        assert discussion_items_for_project(conn, "demo") == []


def test_api_discussion_rejects_foreign_origin(tmp_path):
    """A cross-site Origin is 403 even with a valid supervisor session (CSRF guard)."""
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        _ingest_one(base_url)
        _provision_user(db, "Dad", "dad-key", role="supervisor", projects=["demo"])
        cookie = _login(base_url, "dad-key")
        status, _body, _ = _post_api_json(
            base_url, "/api/discussions/demo/items", {"body": "x"},
            cookie=cookie, origin="https://evil.example",
        )
        assert status == 403
        conn = open_relay_store(db)
        assert discussion_items_for_project(conn, "demo") == []


def test_api_discussion_404_out_of_scope_and_missing_project(tmp_path):
    """An out-of-scope project and a nonexistent one both 404 (existence-hiding), store nothing.

    Why this matters: a supervisor scoped to other projects must not even learn that 'demo'
    exists (out-of-scope == missing), and an admin (unrestricted) posting to a phantom
    project is 404 too — the same two-part scope+existence rule the project read applies.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        _ingest_one(base_url)  # project "demo"
        _provision_user(db, "Aunt", "aunt-key", role="supervisor", projects=["other"])
        cookie = _login(base_url, "aunt-key")

        # Out of scope: 'demo' is real but not granted → 404, identical to missing.
        s1, b1, _ = _post_api_json(
            base_url, "/api/discussions/demo/items", {"body": "x"}, cookie=cookie
        )
        assert s1 == 404 and b1 == {"error": "not found"}

        # Unrestricted admin posting to a project that does not exist → 404 (existence check).
        # A real admin user (legacy-admin is off once any user is provisioned).
        _provision_user(db, "Owner", "owner-key", role="admin")
        admin_cookie = _login(base_url, "owner-key")
        s2, _, _ = _post_api_json(
            base_url, "/api/discussions/ghost/items", {"body": "x"}, cookie=admin_cookie
        )
        assert s2 == 404
        conn = open_relay_store(db)
        assert discussion_items_for_project(conn, "demo") == []


def test_api_discussion_rejects_empty_and_oversized_body(tmp_path):
    """A whitespace-only body and an over-cap body are both 400 (and store nothing)."""
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        _ingest_one(base_url)
        cookie = _login(base_url, _VIEW)

        s_empty, _, _ = _post_api_json(
            base_url, "/api/discussions/demo/items", {"body": "   "}, cookie=cookie
        )
        assert s_empty == 400
        s_big, _, _ = _post_api_json(
            base_url, "/api/discussions/demo/items",
            {"body": "x" * (MAX_COMMENT_BODY_CHARS + 1)}, cookie=cookie,
        )
        assert s_big == 400
        conn = open_relay_store(db)
        assert discussion_items_for_project(conn, "demo") == []


def test_api_discussion_thread_appears_in_project_detail_read(tmp_path):
    """A posted item shows up under 'discussions' in GET /api/projects/<name>, oldest first.

    Why this matters: the read side folds the thread into the project detail (where the panel
    lives), mirroring comments. This is the end-to-end write→read the SPA depends on, and it
    confirms the real role rides the wire (gap 7 closed for this surface).
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        _ingest_one(base_url)
        _provision_user(db, "Dad", "dad-key", role="supervisor", projects=["demo"])
        _provision_user(db, "Owner", "owner-key", role="admin")  # the developer/owner
        sup_cookie = _login(base_url, "dad-key")
        _post_api_json(
            base_url, "/api/discussions/demo/items", {"body": "How's auth?"}, cookie=sup_cookie
        )
        # The developer (admin) replies, so the read shows both roles.
        dev_cookie = _login(base_url, "owner-key")
        _post_api_json(
            base_url, "/api/discussions/demo/items", {"body": "Landed."}, cookie=dev_cookie
        )

        status, detail = _get_json(base_url, "/api/projects/demo", cookie=sup_cookie)
        assert status == 200
        thread = detail["discussions"]
        assert [(d["author_name"], d["role"], d["body"]) for d in thread] == [
            ("Dad", "supervisor", "How's auth?"),
            ("Owner", "developer", "Landed."),
        ]
        assert all("author_id" not in d for d in thread)  # internal id stays off the wire


# --- GET/POST /api/discussions: the developer's Bearer machine loop (E2 Inc 5, Unit 3a) ---
# The CLI's terminal half: a Bearer pull (mirrors GET /api/comments) + a Bearer reply that
# ALWAYS lands as role="developer" (the token is the developer's credential — it cannot forge
# a supervisor entry). Distinct from Unit 2's cookie routes.


def _api_discussions_path(project=None, since_id=None):
    """Build a "/api/discussions" path with an (optionally) encoded query string."""
    params = {}
    if project is not None:
        params["project"] = project
    if since_id is not None:
        params["since_id"] = since_id
    query = urllib.parse.urlencode(params)
    return "/api/discussions" + (f"?{query}" if query else "")


def _seed_discussion(db, project, entries):
    """Append discussion items directly via the store; return their ids.

    entries: list of (author_name, role, body). author_id is None (machine-style).
    """
    conn = open_relay_store(db)
    try:
        return [
            add_discussion_item(conn, project, None, name, role, body,
                                "2026-06-28T10:00:00+00:00")
            for (name, role, body) in entries
        ]
    finally:
        conn.close()


def test_api_discussions_pull_requires_bearer_and_never_echoes_secret(tmp_path):
    """The machine pull is Bearer-gated; a wrong token is 401 and never leaks the secret."""
    with _running_relay(tmp_path) as (base_url, db):
        code, body = _get(base_url, _api_discussions_path(project="demo"), bearer="wrong")
        assert code == 401
        assert _TOKEN not in body


def test_api_discussions_pull_returns_thread_and_latest_id(tmp_path):
    """A Bearer pull returns the project's items oldest-first plus a latest_id watermark.

    Why this matters: this is the developer's read promise — each item's fields (including
    the real role, so the terminal can label supervisor vs developer turns) and the highest
    id to advance the local watermark to.
    """
    with _running_relay(tmp_path) as (base_url, db):
        _ingest_one(base_url)  # project "demo"
        d1, d2 = _seed_discussion(
            db, "demo", [("Dad", "supervisor", "How's auth?"), ("Yusuf", "developer", "Landed.")]
        )
        code, body = _get(base_url, _api_discussions_path(project="demo"), bearer=_TOKEN)
        assert code == 200
        payload = json.loads(body)
        assert [(d["role"], d["body"]) for d in payload["discussions"]] == [
            ("supervisor", "How's auth?"), ("developer", "Landed."),
        ]
        assert [d["id"] for d in payload["discussions"]] == [d1, d2]
        assert payload["latest_id"] == d2


def test_api_discussions_pull_since_id_and_caught_up(tmp_path):
    """since_id returns only newer items; with nothing newer, [] and latest_id echoes since_id."""
    with _running_relay(tmp_path) as (base_url, db):
        _ingest_one(base_url)
        d1, d2 = _seed_discussion(
            db, "demo", [("Dad", "supervisor", "seen"), ("Dad", "supervisor", "new")]
        )
        code, body = _get(
            base_url, _api_discussions_path(project="demo", since_id=d1), bearer=_TOKEN
        )
        payload = json.loads(body)
        assert [d["body"] for d in payload["discussions"]] == ["new"]
        assert payload["latest_id"] == d2

        # Caught up: nothing newer than d2 → empty, watermark stays at d2 (idempotent advance).
        code2, body2 = _get(
            base_url, _api_discussions_path(project="demo", since_id=d2), bearer=_TOKEN
        )
        assert code2 == 200
        payload2 = json.loads(body2)
        assert payload2["discussions"] == [] and payload2["latest_id"] == d2


def test_api_discussions_pull_unknown_project_is_200_empty_and_missing_is_400(tmp_path):
    """An unknown project pulls a clean 200 empty; a missing project param is a 400."""
    with _running_relay(tmp_path) as (base_url, db):
        code, body = _get(base_url, _api_discussions_path(project="ghost"), bearer=_TOKEN)
        assert code == 200 and json.loads(body)["discussions"] == []
        miss, _ = _get(base_url, _api_discussions_path(), bearer=_TOKEN)  # no project
        assert miss == 400


def test_api_discussion_post_stores_as_developer_and_returns_201(tmp_path):
    """A Bearer reply lands as role='developer' with author_id None and the supplied name.

    Why this matters: the developer's write half. The 201 echoes the new id; the row is
    stored with the server-fixed role and the --as name, ready for the supervisor to read.
    """
    with _running_relay(tmp_path) as (base_url, db):
        _ingest_one(base_url)
        status, raw = _post(
            base_url, json.dumps({"project": "demo", "author": "Yusuf", "body": "Landed."}).encode(),
            token=_TOKEN, path="/api/discussions",
        )
        assert status == 201 and isinstance(json.loads(raw)["id"], int)

        conn = open_relay_store(db)
        stored = discussion_items_for_project(conn, "demo")
        assert len(stored) == 1
        assert stored[0]["role"] == "developer"
        assert stored[0]["author_id"] is None and stored[0]["author_name"] == "Yusuf"
        assert stored[0]["body"] == "Landed."


def test_api_discussion_post_omitted_author_uses_default_label(tmp_path):
    """With no author, the reply is stored under the fixed 'developer' fallback label."""
    with _running_relay(tmp_path) as (base_url, db):
        _ingest_one(base_url)
        status, _ = _post(
            base_url, json.dumps({"project": "demo", "body": "no name given"}).encode(),
            token=_TOKEN, path="/api/discussions",
        )
        assert status == 201
        conn = open_relay_store(db)
        assert discussion_items_for_project(conn, "demo")[0]["author_name"] == "developer"


def test_api_discussion_post_cannot_forge_supervisor_role(tmp_path):
    """A body claiming role='supervisor' is ignored: the Bearer path always stores 'developer'.

    Why this matters: this is the core integrity property of the machine write — the ingest
    token authorizes 'the developer' and nothing more, so a client cannot forge a supervisor
    entry by stuffing a role into the body (the handler never reads role from the body).
    """
    with _running_relay(tmp_path) as (base_url, db):
        _ingest_one(base_url)
        status, _ = _post(
            base_url,
            json.dumps({"project": "demo", "body": "I am dad", "role": "supervisor",
                        "author_id": 7}).encode(),
            token=_TOKEN, path="/api/discussions",
        )
        assert status == 201
        conn = open_relay_store(db)
        stored = discussion_items_for_project(conn, "demo")[0]
        assert stored["role"] == "developer" and stored["author_id"] is None


def test_api_discussion_post_wrong_token_is_401_and_stores_nothing(tmp_path):
    """A wrong Bearer token is 401 and writes nothing."""
    with _running_relay(tmp_path) as (base_url, db):
        _ingest_one(base_url)
        status, _ = _post(
            base_url, json.dumps({"project": "demo", "body": "x"}).encode(),
            token="wrong", path="/api/discussions",
        )
        assert status == 401
        conn = open_relay_store(db)
        assert discussion_items_for_project(conn, "demo") == []


def test_api_discussion_post_unknown_project_is_404(tmp_path):
    """Replying to a project with no reports or checklist is 404 (no orphan threads)."""
    with _running_relay(tmp_path) as (base_url, db):
        status, _ = _post(
            base_url, json.dumps({"project": "ghost", "body": "x"}).encode(),
            token=_TOKEN, path="/api/discussions",
        )
        assert status == 404


def test_api_discussion_post_empty_body_is_400(tmp_path):
    """A whitespace-only body is 400 and stores nothing."""
    with _running_relay(tmp_path) as (base_url, db):
        _ingest_one(base_url)
        status, _ = _post(
            base_url, json.dumps({"project": "demo", "body": "   "}).encode(),
            token=_TOKEN, path="/api/discussions",
        )
        assert status == 400
        conn = open_relay_store(db)
        assert discussion_items_for_project(conn, "demo") == []


# --- POST /disciplines + GET /api/disciplines (E2 Inc 4 slice 4b) ----------------
# /disciplines upserts a project's observed principles WITHOUT a report (like
# /checklist); /api/disciplines serves them split into Global + per-project groups.


def _disc(title, why="why", scope="project", source="CLAUDE.md"):
    """Build one discipline card dict (the push/serialize shape)."""
    return {"title": title, "why": why, "scope": scope, "source": source}


def _push_disciplines(base_url, project, cards, *, token=_TOKEN):
    """Push a {project, disciplines} body through the real /disciplines endpoint."""
    body = json.dumps({"project": project, "disciplines": cards}).encode("utf-8")
    return _post(base_url, body, token=token, path="/disciplines")


def test_disciplines_push_upserts_without_a_report(tmp_path):
    """A valid POST /disciplines stores the cards and returns 200, creating no report.

    Why this matters: the dedicated push sets the dashboard's Disciplines section with no
    report row — the same near-real-time model as /checklist.
    """
    with _running_relay(tmp_path) as (base_url, db):
        status, body = _push_disciplines(base_url, "demo", [_disc("Local-first", scope="global")])
        assert status == 200
        assert json.loads(body) == {"updated": "demo", "disciplines": 1}

        conn = open_relay_store(db)
        assert get_disciplines(conn, "demo") == [_disc("Local-first", scope="global")]
        assert list_projects(conn) == []  # no report row was created


def test_disciplines_push_wrong_token_is_401(tmp_path):
    """A bad Bearer token is rejected 401 and stores nothing.

    Why this matters: /disciplines is a machine push on the ingest credential — a wrong
    token must not write.
    """
    with _running_relay(tmp_path) as (base_url, db):
        status, _ = _push_disciplines(base_url, "demo", [_disc("X")], token="wrong")
        assert status == 401
        conn = open_relay_store(db)
        assert get_disciplines(conn, "demo") is None


def test_disciplines_push_malformed_is_400_and_stores_nothing(tmp_path):
    """A card with a non-string title is rejected 400 and nothing is stored.

    Why this matters: /disciplines is an untrusted surface, so a malformed card must fail
    validation before any write — the section can never hold a shape the SPA can't render.
    """
    with _running_relay(tmp_path) as (base_url, db):
        bad = [{"title": 123, "why": "w", "scope": "global", "source": "CLAUDE.md"}]
        body = json.dumps({"project": "demo", "disciplines": bad}).encode("utf-8")
        status, resp = _post(base_url, body, path="/disciplines")
        assert status == 400
        assert "disciplines" in json.loads(resp)["error"]
        conn = open_relay_store(db)
        assert get_disciplines(conn, "demo") is None


def test_api_disciplines_returns_global_and_project_groups(tmp_path):
    """GET /api/disciplines serves the stored cards split into Global + per-project.

    Why this matters: this is the end-to-end read the SPA renders — a pushed global card
    lands in `global`, a project card under its project group.
    """
    with _running_relay(tmp_path) as (base_url, _db):
        _push_disciplines(
            base_url,
            "orion",
            [_disc("Local-first", scope="global"), _disc("Observe", scope="project")],
        )
        status, out = _get_json(base_url, "/api/disciplines")
        assert status == 200
        assert [c["title"] for c in out["global"]] == ["Local-first"]
        assert out["projects"] == [
            {"name": "orion", "principles": [{"title": "Observe", "why": "why", "source": "CLAUDE.md"}]}
        ]


def test_api_disciplines_is_scoped_for_a_viewer(tmp_path):
    """A scoped viewer never sees an out-of-scope project's disciplines — even a global one.

    Why this matters: scope-filter-FIRST is the leak guard. A global principle declared
    only in a project the viewer can't see must not surface, since even its presence (and
    source path) would leak that project's existence.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        _push_disciplines(base_url, "granted", [_disc("Mine", scope="project")])
        _push_disciplines(base_url, "secret", [_disc("Hidden global", scope="global")])
        _provision_user(db, "Mum", "mum-key", role="viewer", projects=["granted"])

        status, out = _get_json(base_url, "/api/disciplines", cookie=_login(base_url, "mum-key"))
        assert status == 200
        # The out-of-scope project's global never appears, and only 'granted' is a group.
        assert out["global"] == []
        assert [g["name"] for g in out["projects"]] == ["granted"]


# --- POST /skills + GET /api/skills (E2 Inc 4 slice 4c, the skills comb) ----------
# /skills upserts a project's observed competencies WITHOUT a report (like /disciplines);
# /api/skills serves them MERGED across projects into the comb (depth derived server-side).


def _skill(name, *, category="Backend", evidence="ev", weight=2, signals=("git",)):
    """Build one skill card dict (the push/serialize shape)."""
    return {
        "name": name,
        "category": category,
        "evidence": evidence,
        "weight": weight,
        "signals": list(signals),
    }


def _push_skills(base_url, project, cards, *, token=_TOKEN):
    """Push a {project, skills} body through the real /skills endpoint."""
    body = json.dumps({"project": project, "skills": cards}).encode("utf-8")
    return _post(base_url, body, token=token, path="/skills")


def test_skills_push_upserts_without_a_report(tmp_path):
    """A valid POST /skills stores the cards and returns 200, creating no report.

    Why this matters: the dedicated push sets the dashboard's Skills comb with no report
    row — the same near-real-time model as /disciplines.
    """
    with _running_relay(tmp_path) as (base_url, db):
        status, body = _push_skills(base_url, "demo", [_skill("Python backends", weight=3)])
        assert status == 200
        assert json.loads(body) == {"updated": "demo", "skills": 1}

        conn = open_relay_store(db)
        assert get_skills(conn, "demo") == [_skill("Python backends", weight=3)]
        assert list_projects(conn) == []  # no report row was created


def test_skills_push_malformed_is_400_and_stores_nothing(tmp_path):
    """A card with a non-integer weight is rejected 400 and nothing is stored.

    Why this matters: /skills is an untrusted surface, so a malformed card must fail
    validation before any write — the comb can never hold a shape the SPA can't render.
    """
    with _running_relay(tmp_path) as (base_url, db):
        bad = [{"name": "X", "category": "Backend", "evidence": "e", "weight": "lots", "signals": []}]
        body = json.dumps({"project": "demo", "skills": bad}).encode("utf-8")
        status, resp = _post(base_url, body, path="/skills")
        assert status == 400
        assert "weight" in json.loads(resp)["error"]
        conn = open_relay_store(db)
        assert get_skills(conn, "demo") is None


def _push_skills_batch(base_url, slices, *, allow_empty=False, token=_TOKEN):
    """Push a {projects, allow_empty} batch through the real /skills-batch endpoint."""
    body = json.dumps({"projects": slices, "allow_empty": allow_empty}).encode("utf-8")
    return _post(base_url, body, token=token, path="/skills-batch")


def test_skills_batch_replaces_all_and_prunes(tmp_path):
    """POST /skills-batch writes every project's slice atomically and prunes absent ones.

    Why this matters: the global skills-sync front door. One request sets every project's
    skills together (so the canonical re-naming is atomic), and a project that drops out of
    the batch is pruned rather than left with stale-named cards that would break the merge.
    """
    with _running_relay(tmp_path) as (base_url, db):
        # Seed a project the batch will not mention, so prune must remove it.
        _push_skills(base_url, "gone", [_skill("Stale skill")])

        status, body = _push_skills_batch(
            base_url,
            {
                "alpha": [_skill("Python backends", weight=2)],
                "beta": [_skill("React", category="Frontend")],
            },
        )
        assert status == 200
        assert json.loads(body) == {"updated": 2, "skills": 2}

        conn = open_relay_store(db)
        assert get_skills(conn, "alpha") == [_skill("Python backends", weight=2)]
        assert get_skills(conn, "gone") is None  # pruned
        assert skills_projects(conn) == ["alpha", "beta"]


def test_skills_batch_refuses_to_clear_a_populated_comb(tmp_path):
    """An all-empty batch over a populated store is refused 409 unless allow_empty.

    Why this matters: the empty-clobber backstop. A whole-portfolio wipe is the signature
    of a degraded producer run, so the relay refuses it by default and leaves the stored
    skills intact; an explicit allow_empty acknowledges an intentional clear.
    """
    with _running_relay(tmp_path) as (base_url, db):
        _push_skills(base_url, "alpha", [_skill("Python backends")])

        # All slices empty, no allow_empty -> refused, store untouched.
        status, resp = _push_skills_batch(base_url, {"alpha": []})
        assert status == 409
        assert "allow_empty" in json.loads(resp)["error"]
        conn = open_relay_store(db)
        assert get_skills(conn, "alpha") == [_skill("Python backends")]

        # Same batch WITH allow_empty -> accepted, alpha cleared.
        status, _body = _push_skills_batch(base_url, {"alpha": []}, allow_empty=True)
        assert status == 200
        conn = open_relay_store(db)
        assert get_skills(conn, "alpha") == []


def test_skills_batch_requires_auth(tmp_path):
    """POST /skills-batch with no Bearer token is rejected 401 and stores nothing.

    Why this matters: the batch is a privileged ingest surface like /skills, so it must
    authenticate before any write.
    """
    with _running_relay(tmp_path) as (base_url, db):
        status, _resp = _push_skills_batch(
            base_url, {"alpha": [_skill("X")]}, token=None
        )
        assert status == 401
        conn = open_relay_store(db)
        assert get_skills(conn, "alpha") is None


def test_api_skills_merges_across_projects(tmp_path):
    """GET /api/skills merges the same skill across two projects into one deeper card.

    Why this matters: the end-to-end read the comb renders — a competency pushed by two
    projects becomes a single card whose breadth raises its depth, anchored to both.
    """
    with _running_relay(tmp_path) as (base_url, _db):
        _push_skills(base_url, "orion", [_skill("Python backends", weight=2)])
        _push_skills(base_url, "sar_hackathon", [_skill("python backends", weight=2)])
        status, out = _get_json(base_url, "/api/skills")
        assert status == 200
        assert len(out["skills"]) == 1
        merged = out["skills"][0]
        assert merged["projects"] == ["orion", "sar_hackathon"]
        assert merged["depth"] == 3  # re-tuned scale: total 4 + breadth 1 = score 5 -> depth 3


def test_api_skills_is_scoped_for_a_viewer(tmp_path):
    """A scoped viewer never sees a skill evidenced only by an out-of-scope project.

    Why this matters: scope-filter-FIRST is the leak guard — a skill (and the project that
    evidences it) outside the viewer's grant must not surface in the merge.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        _push_skills(base_url, "granted", [_skill("Mine", weight=2)])
        _push_skills(base_url, "secret", [_skill("Hidden", weight=2)])
        _provision_user(db, "Mum", "mum-key", role="viewer", projects=["granted"])

        status, out = _get_json(base_url, "/api/skills", cookie=_login(base_url, "mum-key"))
        assert status == 200
        assert [s["name"] for s in out["skills"]] == ["Mine"]
        assert out["skills"][0]["projects"] == ["granted"]

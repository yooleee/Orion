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
from datetime import timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from zoneinfo import ZoneInfo

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
    add_discussion_item,
    add_user,
    bump_session_version,
    discussion_items_for_project,
    get,
    get_about,
    get_checklist,
    get_due_soon_days,
    get_project_kind,
    get_project_lifecycle,
    get_user_by_name,
    history,
    list_projects,
    org_visible_projects,
    observed_history,
    open_relay_store,
    producer_checklists_for,
    producer_disciplines_for,
    project_disciplines,
    revoke_user,
)
from relay.derive import today_in_tz

_TOKEN = "test-ingest-token"

# The relay derives "today" in this zone (server._DISPLAY_TZ, the un-overridden default in
# _running_relay). The forward-look tests below compute deadlines RELATIVE to this same
# "today" so a 10-day-out item is deterministically inside a 14-day window but outside the
# 7-day default, independent of the wall-clock date the suite runs on.
_RELAY_TZ = ZoneInfo("America/Los_Angeles")
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
        # the cookie-write CSRF check does an EXACT canonical-origin match instead of the Host
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


def _provision_user(
    db, name, key, *, role="viewer", projects=(), account_kind=None, operated_by=None
):
    """Provision a real user (name + key verifier + scope) directly via the store.

    Args:
        db: the relay's sqlite path.
        name: the user's unique display name / handle.
        key: the raw login key (its HMAC verifier under _PEPPER is what gets stored).
        role: "viewer" (default) or "admin".
        projects: the viewer's allowed project names (ignored for an admin).
        account_kind: "agent" to seed an agent account, else None (a human).
        operated_by: for an agent, the operating human's user id.

    Why:
        The login / authZ tests need a genuine relay_users row to authenticate against.
        We insert it through the same store helper the provisioning endpoint will use,
        computing the verifier exactly as the server does (HMAC under the shared
        _PEPPER), so the real login path resolves the presented key to this user.

        The agent params write the row DIRECTLY, deliberately bypassing the endpoint's
        validation — so a test can seed a state (e.g. an agent whose operator was later
        revoked) that provisioning would refuse to create, and still assert how the read
        paths behave once it exists.
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
            account_kind=account_kind,
            operated_by=operated_by,
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


# --- E1.2 (forward-look) / KI-35: the due_soon_days knob on the relay's inbound surfaces --
# The knob rides BOTH carriers (the /checklist push and the /ingest blob) and is validated to
# an int in 1..365 when present. KI-35 made it TRI-STATE on /checklist: absent = leave the
# stored value alone, explicit null = clear, int = set. /ingest stays set-only and REJECTS an
# explicit null. End to end it widens the "due soon" classification the dashboard reads.


def _checklist_body_with_due_soon(
    project="demo", items=None, due_soon_days=None, clear=False
):
    """A {project, checklist[, due_soon_days]} push body.

    `due_soon_days=None, clear=False` omits the key (leave-alone); `clear=True` sends an
    explicit JSON null (the clear signal); an int sends the value.
    """
    if items is None:
        items = [{"text": "Wire it", "done": False}]
    payload = {"project": project, "checklist": items}
    if clear:
        payload["due_soon_days"] = None
    elif due_soon_days is not None:
        payload["due_soon_days"] = due_soon_days
    return json.dumps(payload).encode("utf-8")


def test_checklist_push_persists_due_soon_days(tmp_path):
    """A /checklist push carrying due_soon_days=14 stores it; a later push overwrites it.

    Why this matters: the horizon is CURRENT STATE riding the push. The first push must
    persist it, and a later push with a new value must win (last-writer-wins), so the
    dashboard always classifies against the newest configured window.
    """
    with _running_relay(tmp_path) as (base_url, db):
        assert _post(base_url, _checklist_body_with_due_soon(due_soon_days=14), path="/checklist")[0] == 200
        conn = open_relay_store(db)
        assert get_due_soon_days(conn, "demo") == 14
        # A later push with 30 overwrites the same meta row.
        assert _post(base_url, _checklist_body_with_due_soon(due_soon_days=30), path="/checklist")[0] == 200
        assert get_due_soon_days(open_relay_store(db), "demo") == 30


def test_checklist_push_without_due_soon_days_leaves_the_horizon_unset(tmp_path):
    """A /checklist push omitting due_soon_days on a fresh project leaves it unset — back-compat.

    Why this matters: a producer predating the knob omits it entirely; the relay must accept
    that and leave the horizon NULL so the classifier falls back to the 7-day default,
    byte-identically.
    """
    with _running_relay(tmp_path) as (base_url, db):
        assert _post(base_url, _checklist_body_with_due_soon(), path="/checklist")[0] == 200
        assert get_due_soon_days(open_relay_store(db), "demo") is None


def test_checklist_push_omitting_due_soon_days_preserves_a_set_horizon(tmp_path):
    """KI-35: a push that omits due_soon_days must PRESERVE a horizon someone else set.

    Why this matters: this is the whole point of the unit, and it inverts the previous
    behavior (absence used to clear). The scenario is two producers pushing to one shared
    project: the first has `due_soon_days = 14` in its config, the second does not configure
    it at all and so omits the field on every push. Once the second producer's push is
    SCHEDULED (E1.3), the old clear-on-absence rule wiped the first producer's horizon
    periodically and silently. Absence must now mean "leave it alone".
    """
    with _running_relay(tmp_path) as (base_url, db):
        # Producer A (configured) sets the horizon.
        assert _post(base_url, _checklist_body_with_due_soon(due_soon_days=14), path="/checklist")[0] == 200
        assert get_due_soon_days(open_relay_store(db), "demo") == 14
        # Producer B (no due_soon_days config) pushes the same project — omits the field.
        assert _post(base_url, _checklist_body_with_due_soon(), path="/checklist")[0] == 200
        assert get_due_soon_days(open_relay_store(db), "demo") == 14  # survives, not clobbered


def test_checklist_push_explicit_null_clears_the_horizon_and_is_idempotent(tmp_path):
    """An explicit JSON null on /checklist clears the horizon, and repeating it is a no-op.

    Why this matters: with absence no longer clearing, clearing needs its own signal — the
    tri-state's third state, sent by `checklist-push --clear-due-soon-days`. The clear must
    reset to NULL (→ the 7-day default), and running it twice must be safe: a user who is not
    sure whether the clear landed should be able to just run it again.
    """
    with _running_relay(tmp_path) as (base_url, db):
        assert _post(base_url, _checklist_body_with_due_soon(due_soon_days=14), path="/checklist")[0] == 200
        assert get_due_soon_days(open_relay_store(db), "demo") == 14
        assert _post(base_url, _checklist_body_with_due_soon(clear=True), path="/checklist")[0] == 200
        assert get_due_soon_days(open_relay_store(db), "demo") is None
        # Idempotent: a second clear on an already-cleared project still succeeds.
        assert _post(base_url, _checklist_body_with_due_soon(clear=True), path="/checklist")[0] == 200
        assert get_due_soon_days(open_relay_store(db), "demo") is None


def test_checklist_push_omitting_kind_preserves_a_stored_tracker(tmp_path):
    """KI-35 (sibling setting): a push omitting `kind` must not reset a tracker to a project.

    Why this matters: `kind` shares the meta row with due_soon_days and had the identical
    silent-clobber bug — the handler wrote `payload.get("kind") or "project"` unconditionally,
    so any push that omitted the field demoted a tracker back to a project on the home page.
    Set-only fixes both settings together. The second half of the test pins that a PRESENT
    kind still sets, so set-only did not turn into never-set.
    """
    with _running_relay(tmp_path) as (base_url, db):
        body = json.loads(_checklist_body_with_due_soon())
        body["kind"] = "tracker"
        assert _post(base_url, json.dumps(body).encode("utf-8"), path="/checklist")[0] == 200
        assert get_project_kind(open_relay_store(db), "demo") == "tracker"
        # A push with no `kind` at all (an older or differently-configured producer).
        assert _post(base_url, _checklist_body_with_due_soon(), path="/checklist")[0] == 200
        assert get_project_kind(open_relay_store(db), "demo") == "tracker"  # preserved
        # But a push that DOES carry a kind still sets it.
        body["kind"] = "project"
        assert _post(base_url, json.dumps(body).encode("utf-8"), path="/checklist")[0] == 200
        assert get_project_kind(open_relay_store(db), "demo") == "project"


def test_ingest_blob_without_due_soon_days_does_not_clear_it(tmp_path):
    """A report/intake blob (/ingest) that omits due_soon_days must NOT clear a set horizon.

    Why this matters: only the /checklist push clears on absence. /ingest also receives
    `intake` blobs, which legitimately omit checklist config — clearing on their absence would
    wipe a horizon the checklist carrier set. So /ingest is SET-ONLY: a /checklist push sets 14,
    then a plain report blob (which carries no due_soon_days) must leave it at 14, not reset it.
    """
    with _running_relay(tmp_path) as (base_url, db):
        # /checklist sets the horizon for "demo".
        assert _post(base_url, _checklist_body_with_due_soon(due_soon_days=14), path="/checklist")[0] == 200
        assert get_due_soon_days(open_relay_store(db), "demo") == 14
        # A real report blob for "demo" carries NO due_soon_days; ingesting it must not clear it.
        assert _post(base_url, _real_blob_json().encode("utf-8"))[0] == 201
        assert get_due_soon_days(open_relay_store(db), "demo") == 14  # preserved, not clobbered


@pytest.mark.parametrize("bad_value", [0, 366, "14", True, 3.5])
def test_checklist_push_rejects_invalid_due_soon_days(tmp_path, bad_value):
    """An out-of-range / non-int due_soon_days is a clean 400 and stores nothing.

    Why this matters: the horizon is untrusted input, so 0 (below the 1-day floor), 366
    (above the 365 ceiling), a string, a bool (an int subclass but never a real day count),
    and a float must all be rejected at the boundary — never a bad horizon in the classifier.
    """
    body = _checklist_body_with_due_soon(due_soon_days=bad_value)
    with _running_relay(tmp_path) as (base_url, db):
        status, resp = _post(base_url, body, path="/checklist")
        assert status == 400
        assert "due_soon_days" in json.loads(resp)["error"]
        # Nothing persisted: neither the checklist nor a meta horizon row.
        conn = open_relay_store(db)
        assert get_checklist(conn, "demo") is None
        assert get_due_soon_days(conn, "demo") is None


def test_ingest_blob_persists_due_soon_days(tmp_path):
    """The /ingest blob carrier also accepts and persists due_soon_days.

    Why this matters: the knob rides the report blob too, not only /checklist. A real blob
    with the field added must be accepted (201) and the horizon stored — the ingest half of
    the two-carrier contract. (It also confirms _validate_blob tolerates the field.)
    """
    blob = json.loads(_real_blob_json())
    blob["due_soon_days"] = 21
    with _running_relay(tmp_path) as (base_url, db):
        assert _post(base_url, json.dumps(blob).encode("utf-8"))[0] == 201
        assert get_due_soon_days(open_relay_store(db), "demo") == 21


def test_ingest_blob_rejects_invalid_due_soon_days(tmp_path):
    """A blob with an out-of-range due_soon_days is 400 and stores nothing.

    Why this matters: the blob carrier applies the SAME validation as /checklist, so a bad
    horizon is refused before any report row is written.
    """
    blob = json.loads(_real_blob_json())
    blob["due_soon_days"] = 999
    with _running_relay(tmp_path) as (base_url, db):
        status, resp = _post(base_url, json.dumps(blob).encode("utf-8"))
        assert status == 400
        assert "due_soon_days" in json.loads(resp)["error"]
        assert list_projects(open_relay_store(db)) == []  # no report row created


def test_ingest_blob_rejects_an_explicit_null_due_soon_days(tmp_path):
    """An explicit null on /ingest is a 400, and leaves an already-set horizon untouched.

    Why this matters: the two carriers deliberately diverge on the clear signal (KI-35). A
    report blob must never be able to clear a project's settings — /ingest also carries
    `intake` blobs, and a clear riding a report would be exactly the accident the set-only
    rule exists to prevent. Only the dedicated /checklist carrier owns clearing. The stored
    value is asserted afterwards, not just the status code: a rejected payload must leave no
    partial meta write behind.
    """
    with _running_relay(tmp_path) as (base_url, db):
        assert _post(base_url, _checklist_body_with_due_soon(due_soon_days=14), path="/checklist")[0] == 200
        blob = json.loads(_real_blob_json())
        blob["due_soon_days"] = None
        status, resp = _post(base_url, json.dumps(blob).encode("utf-8"))
        assert status == 400
        assert "due_soon_days" in json.loads(resp)["error"]
        assert get_due_soon_days(open_relay_store(db), "demo") == 14  # untouched


def _checklist_body_with_about(project="demo", items=None, about=None, clear=False):
    """A {project, checklist[, about]} push body.

    `about=None, clear=False` omits the key (leave-alone); `clear=True` sends an explicit
    JSON null (the clear signal); a string sends the value. Mirrors the due_soon helper.
    """
    if items is None:
        items = [{"text": "Wire it", "done": False}]
    payload = {"project": project, "checklist": items}
    if clear:
        payload["about"] = None
    elif about is not None:
        payload["about"] = about
    return json.dumps(payload).encode("utf-8")


def test_checklist_push_persists_about(tmp_path):
    """A /checklist push carrying `about` stores it; a later push overwrites it.

    Why this matters: About is CURRENT STATE riding the push — the first push persists it,
    a later one wins (last-writer-wins), so the dashboard shows the newest observed line.
    """
    with _running_relay(tmp_path) as (base_url, db):
        assert _post(base_url, _checklist_body_with_about(about="A build tool."), path="/checklist")[0] == 200
        assert get_about(open_relay_store(db), "demo") == "A build tool."
        assert _post(base_url, _checklist_body_with_about(about="A deploy tool."), path="/checklist")[0] == 200
        assert get_about(open_relay_store(db), "demo") == "A deploy tool."


def test_checklist_push_omitting_about_preserves_a_set_about(tmp_path):
    """KI-35: a push that omits `about` must PRESERVE an About another producer set.

    Why this matters: About rides the same tri-state carrier as due_soon_days, so absence
    must mean "leave alone" — a configless producer's scheduled push can never wipe an About
    another producer set (the silent scheduled clobber KI-35 closes, applied to About).
    """
    with _running_relay(tmp_path) as (base_url, db):
        assert _post(base_url, _checklist_body_with_about(about="Set by A."), path="/checklist")[0] == 200
        assert _post(base_url, _checklist_body_with_about(), path="/checklist")[0] == 200  # B omits it
        assert get_about(open_relay_store(db), "demo") == "Set by A."  # survives


def test_checklist_push_explicit_null_clears_about_and_is_idempotent(tmp_path):
    """An explicit JSON null on /checklist clears About, and repeating it is a no-op.

    Why this matters: with absence no longer clearing, clearing needs the tri-state's third
    state (sent by `--clear-about`). It must NULL the stored About (→ no band), and a second
    clear on an already-cleared project must still succeed.
    """
    with _running_relay(tmp_path) as (base_url, db):
        assert _post(base_url, _checklist_body_with_about(about="Clear me."), path="/checklist")[0] == 200
        assert get_about(open_relay_store(db), "demo") == "Clear me."
        assert _post(base_url, _checklist_body_with_about(clear=True), path="/checklist")[0] == 200
        assert get_about(open_relay_store(db), "demo") is None
        assert _post(base_url, _checklist_body_with_about(clear=True), path="/checklist")[0] == 200
        assert get_about(open_relay_store(db), "demo") is None


# --- S2.2 U3: the About-carrier decoupling (a checklist-less /checklist push) ----------
#
# /checklist used to REQUIRE items ("the request exists to set it"), which meant a project
# with no tasks_file could never carry an About — the carrier demanded an unrelated field.
# Items are now optional. The load-bearing property is that omitting them CHANGES NOTHING
# about the checklist, on all three of its write paths.


def _settings_only_body(project="demo", **fields):
    """A /checklist body with NO `checklist` key — the settings-only push."""
    return json.dumps({"project": project, **fields}).encode("utf-8")


def test_a_settings_only_push_never_disturbs_a_stored_checklist(tmp_path):
    """THE clobber test: an About-only push leaves live state, producer copy AND history alone.

    Why this matters: this is the risk the whole unit was scoped around. A push that omits
    items is not evidence that the checklist is empty, and three separate stores would
    record it as such if the guard were wrong — the aggregate (wiping the dashboard's live
    checklist), the per-producer copy (which the ≥2-producer merge reads), and the
    observation history. That last one is APPEND-ONLY: a false "no items existed at this
    instant" row cannot be undone, and would corrupt slippage derivation from then on.

    So all three are asserted, before and after, with an identified producer so the
    per-producer path is actually exercised.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "pusher", "pusher-key", role="contributor", projects=["demo"])
        items = [{"text": "Wire it", "done": False}, {"text": "Ship it", "done": True}]
        body = json.dumps({"project": "demo", "checklist": items}).encode("utf-8")
        assert _post(base_url, body, token="pusher-key", path="/checklist")[0] == 200

        conn = open_relay_store(db)
        before_aggregate = get_checklist(conn, "demo")
        before_producers = producer_checklists_for(conn, "demo")
        before_history = observed_history(conn, "demo")
        conn.close()
        assert len(before_aggregate) == 2
        assert len(before_producers) == 1  # the identified producer's own copy exists
        assert len(before_history) == 2

        # The About-only push, from the same producer.
        status, resp = _post(
            base_url,
            _settings_only_body(about="A tool with no checklist."),
            token="pusher-key",
            path="/checklist",
        )
        assert status == 200
        # `items` is null, NOT 0 — zero would claim the relay was told the checklist is empty.
        assert json.loads(resp)["items"] is None

        conn = open_relay_store(db)
        assert get_about(conn, "demo") == "A tool with no checklist."  # the push did its job
        assert get_checklist(conn, "demo") == before_aggregate        # live state untouched
        assert producer_checklists_for(conn, "demo") == before_producers
        assert observed_history(conn, "demo") == before_history       # nothing appended
        conn.close()


def test_a_checklist_less_project_can_carry_an_about(tmp_path):
    """A project that never pushed a checklist can still be given an About.

    Why this matters: this is the gap the unit closes, stated as an outcome. Before, About
    only rode a checklist or a report, so a project with neither had no way to get one.
    """
    with _running_relay(tmp_path) as (base_url, db):
        # This project has no reports and no checklist — nothing but the About push.
        status, _ = _post(
            base_url,
            _settings_only_body(project="barebones", about="A small experiment."),
            path="/checklist",
        )
        assert status == 200
        conn = open_relay_store(db)
        assert get_about(conn, "barebones") == "A small experiment."
        assert get_checklist(conn, "barebones") is None  # still genuinely checklist-less
        conn.close()


def test_a_null_checklist_is_still_rejected(tmp_path):
    """An explicit `"checklist": null` is a 400 — absence and null stay different requests.

    Why this matters: omitting the key means "leave it alone". There is deliberately NO
    clearing story for a checklist (unlike about / due_soon_days), so a null must not be
    quietly treated as either one. Accepting it would open a clobber path on live state.
    """
    with _running_relay(tmp_path) as (base_url, db):
        items = [{"text": "Keep me", "done": False}]
        assert _post(
            base_url,
            json.dumps({"project": "demo", "checklist": items}).encode("utf-8"),
            path="/checklist",
        )[0] == 200

        status, resp = _post(
            base_url,
            json.dumps({"project": "demo", "checklist": None}).encode("utf-8"),
            path="/checklist",
        )
        assert status == 400 and "checklist" in json.loads(resp)["error"]

        conn = open_relay_store(db)
        assert len(get_checklist(conn, "demo")) == 1  # untouched by the rejected push
        conn.close()


def test_a_push_that_sets_nothing_is_a_400(tmp_path):
    """`{project}` alone is refused rather than answered 200.

    Why this matters: with every field optional, a payload carrying only the project name
    parses fine and does nothing. Answering 200 would hide a client bug behind an apparent
    success — the caller believes it pushed something.
    """
    with _running_relay(tmp_path) as (base_url, _db):
        status, resp = _post(base_url, _settings_only_body(), path="/checklist")
        assert status == 400
        assert "at least one" in json.loads(resp)["error"]


def test_a_normal_checklist_push_is_unchanged(tmp_path):
    """The ordinary push still writes all three stores and reports its item count.

    Why this matters: the guard is new code on the hot path every producer uses. This pins
    that making items optional did not change what happens when they ARE sent.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "pusher", "pusher-key", role="contributor", projects=["demo"])
        items = [{"text": "Wire it", "done": False}]
        status, resp = _post(
            base_url,
            json.dumps({"project": "demo", "checklist": items}).encode("utf-8"),
            token="pusher-key",
            path="/checklist",
        )
        assert status == 200
        assert json.loads(resp) == {"updated": "demo", "items": 1}

        conn = open_relay_store(db)
        assert get_checklist(conn, "demo") == [{"text": "Wire it", "done": False}]
        assert len(producer_checklists_for(conn, "demo")) == 1
        assert len(observed_history(conn, "demo")) == 1
        conn.close()


def test_ingest_blob_persists_about_set_only(tmp_path):
    """The /ingest blob accepts and persists `about`, and omitting it does not clear a set one.

    Why this matters: About rides the report blob too (set-only). A blob with the field is
    accepted (201) and stored; a later plain report that omits it must leave the stored About
    untouched — /ingest never clears (that's the /checklist carrier's job).
    """
    blob = json.loads(_real_blob_json())
    blob["about"] = "Observed from the README."
    with _running_relay(tmp_path) as (base_url, db):
        assert _post(base_url, json.dumps(blob).encode("utf-8"))[0] == 201
        assert get_about(open_relay_store(db), "demo") == "Observed from the README."
        # A plain report blob (no `about`) must not clear it.
        assert _post(base_url, _real_blob_json().encode("utf-8"))[0] == 201
        assert get_about(open_relay_store(db), "demo") == "Observed from the README."


def test_ingest_blob_rejects_an_explicit_null_about(tmp_path):
    """An explicit null `about` on /ingest is a 400 and leaves a set About untouched.

    Why this matters: the two carriers diverge on the clear signal (KI-35) for About just as
    for due_soon_days — a report blob must never clear a project's About (an `intake` blob
    carrying a null would be exactly that accident). Only /checklist owns clearing.
    """
    with _running_relay(tmp_path) as (base_url, db):
        assert _post(base_url, _checklist_body_with_about(about="Keep me."), path="/checklist")[0] == 200
        blob = json.loads(_real_blob_json())
        blob["about"] = None
        status, resp = _post(base_url, json.dumps(blob).encode("utf-8"))
        assert status == 400
        assert "about" in json.loads(resp)["error"]
        assert get_about(open_relay_store(db), "demo") == "Keep me."  # untouched


@pytest.mark.parametrize("bad_value", [123, True, ["x"], {"a": 1}])
def test_checklist_push_rejects_non_string_about(tmp_path, bad_value):
    """A non-string `about` is a clean 400 and stores nothing.

    Why this matters: About is untrusted input; anything but a string (or the null/absent
    signals) is refused at the boundary, never a bad type stored or rendered.
    """
    body = json.dumps({"project": "demo", "checklist": [{"text": "x", "done": False}], "about": bad_value}).encode("utf-8")
    with _running_relay(tmp_path) as (base_url, db):
        status, resp = _post(base_url, body, path="/checklist")
        assert status == 400
        assert "about" in json.loads(resp)["error"]
        assert get_about(open_relay_store(db), "demo") is None


def test_checklist_push_rejects_over_long_about(tmp_path):
    """An About longer than the server cap is a clean 400 and stores nothing.

    Why this matters: the boundary guards against an unbounded string even though the real
    cap is the producer's extraction cap — a defensive limit, so a giant payload can't be
    stored or rendered.
    """
    body = _checklist_body_with_about(about="x" * 5000)
    with _running_relay(tmp_path) as (base_url, db):
        status, resp = _post(base_url, body, path="/checklist")
        assert status == 400
        assert "about" in json.loads(resp)["error"]
        assert get_about(open_relay_store(db), "demo") is None


def test_due_soon_days_widens_at_risk_but_not_the_scheduling_week(tmp_path):
    """A custom horizon widens the AT-RISK classification, but NOT the scheduling timeline week.

    Why this matters: the knob's job is to widen at-risk/due-soon on the project + portfolio
    surfaces (10 <= 14 → at risk for a 14-day project, 10 > 7 → not for a default one). But the
    cross-project Scheduling timeline's "this_week" bucket is a fixed CALENDAR week: raising one
    project's horizon to 14 days must NOT drag its 10-day-out item into a bucket labelled "this
    week" (a shared timeline can't mean different spans per project). Both concerns exercised on
    the SAME 10-day-out item so the split is unambiguous.
    """
    today = today_in_tz(_RELAY_TZ)
    ten_days_out = (today + timedelta(days=10)).isoformat()
    item = [{"text": "Ship", "done": False, "due_date": ten_days_out}]
    with _running_relay(tmp_path) as (base_url, db):
        # "wide" widens its window to 14 days; "narrow" leaves it at the default.
        assert _post(base_url, _checklist_body_with_due_soon("wide", item, 14), path="/checklist")[0] == 200
        assert _post(base_url, _checklist_body_with_due_soon("narrow", item), path="/checklist")[0] == 200

        # The default _running_relay is ungated (no view_token / users), so /api reads are open.
        # (1) Portfolio at-risk DOES honour the horizon: 10 <= 14 for wide → 1; 10 > 7 for narrow → 0.
        code, body = _get(base_url, "/api/portfolio")
        assert code == 200
        projects = {e["name"]: e for e in json.loads(body)["projects"]}
        assert projects["wide"]["at_risk"] == 1
        assert projects["narrow"]["at_risk"] == 0

        # (2) Scheduling buckets do NOT: the fixed 7-day week puts BOTH 10-day-out items in
        # "later" (10 > 7), so the custom horizon never redefines "this week".
        code, body = _get(base_url, "/api/scheduling")
        assert code == 200
        buckets = json.loads(body)["buckets"]

        def bucket_of(project):
            for name, rows in buckets.items():
                if any(r["source"]["name"] == project for r in rows):
                    return name
            return None

        assert bucket_of("wide") == "later"    # NOT widened into "this_week"
        assert bucket_of("narrow") == "later"
        assert json.loads(body)["summary"]["due_this_week"] == 0


def test_wrong_token_is_401_generic_and_stores_nothing(tmp_path):
    """A valid body with the WRONG token is rejected with the generic 401, not stored.

    Why this matters: auth is the gate on the inbound surface. A bad token must not only be
    refused — it must not leave any trace in the store (auth is checked before the body is
    even read). Since C3 Inc 2 the failure message is GENERIC ("unauthorized"): named
    contributor keys now exist, so distinguishing "wrong token" from other failure modes
    would help an attacker enumerate them. The expected token is never echoed.
    """
    with _running_relay(tmp_path) as (base_url, db):
        status, body = _post(base_url, _real_blob_json().encode("utf-8"), token="wrong")
        assert status == 401
        assert json.loads(body)["error"] == "unauthorized"
        # The expected token must never appear in any response.
        assert _TOKEN not in body.decode("utf-8")

        conn = open_relay_store(db)
        assert list_projects(conn) == []  # nothing stored


def test_absent_and_wrong_token_return_the_same_generic_401(tmp_path):
    """A missing header and a wrong token yield the IDENTICAL generic 401 body.

    Why this matters: the endpoint is never open by omission (a missing credential is refused
    just like a wrong one), and — since named contributor keys exist (C3 Inc 2) — the two
    failures must be indistinguishable so an attacker cannot probe which keys or header shapes
    exist. We require byte-identical 401 bodies for both, replacing the earlier design that
    deliberately told them apart (safe only while there was a single, identity-free token).
    """
    with _running_relay(tmp_path) as (base_url, _db):
        blob = _real_blob_json().encode("utf-8")
        absent_status, absent_body = _post(base_url, blob, token=None)
        wrong_status, wrong_body = _post(base_url, blob, token="wrong")
        assert absent_status == wrong_status == 401
        assert json.loads(absent_body)["error"] == "unauthorized"
        assert absent_body == wrong_body  # indistinguishable — no enumeration signal


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


# ---------------------------------------------------------------------------
# C3 Increment 2 — contributor push identity + per-project scope (Unit 1.2a).
# A provisioned "contributor" authenticates the Bearer push path with its OWN key,
# confined to its granted projects; the legacy shared token keeps working anonymously
# until --disable-legacy-ingest. Every Bearer failure is one generic 401.
# ---------------------------------------------------------------------------


def _blob_for(project):
    """A real serialized blob relabeled to `project` (bytes), for push tests."""
    blob = json.loads(_real_blob_json())
    blob["project"] = project
    return json.dumps(blob).encode("utf-8")


def test_contributor_key_ingests_granted_project(tmp_path):
    """A contributor's own key authenticates a push to a project it is granted (201).

    Why this matters: the point of C3 Inc 2 — a producer authenticates the ingest path with
    a named per-user key, not the shared token. The key resolves through the same
    key_verifier machinery as login, so identity is SERVER-derived, never asserted.
    """
    with _running_relay(tmp_path) as (base_url, db):
        _provision_user(db, "mac", "contrib-key", role="contributor", projects=["demo"])
        status, _ = _post(base_url, _blob_for("demo"), token="contrib-key")
        assert status == 201
        conn = open_relay_store(db)
        assert "demo" in [p["project"] for p in list_projects(conn)]


def test_contributor_ingest_out_of_scope_is_404_and_stores_nothing(tmp_path):
    """A contributor pushing to an UNgranted project 404s, indistinguishable from missing.

    Why this matters: write scope = grant scope (decisions #7/#8). An out-of-scope write must
    be refused AND look identical to a project that does not exist, so the push path never
    confirms a project the caller cannot touch. Nothing is stored.
    """
    with _running_relay(tmp_path) as (base_url, db):
        _provision_user(db, "mac", "contrib-key", role="contributor", projects=["demo"])
        status, body = _post(base_url, _blob_for("secret-proj"), token="contrib-key")
        assert status == 404
        assert json.loads(body)["error"] == "not found"
        conn = open_relay_store(db)
        assert "secret-proj" not in [p["project"] for p in list_projects(conn)]


def test_viewer_and_supervisor_keys_cannot_push(tmp_path):
    """A viewer's or supervisor's key is refused on the push path (fail-closed, generic 401).

    Why this matters: the mirror of "a contributor can't log in" — only push-capable roles
    (_BEARER_ROLES) may authenticate ingest. An interactive-only key pointed at /ingest is
    refused exactly like an unknown key, so a role can never write outside its world.
    """
    with _running_relay(tmp_path) as (base_url, db):
        _provision_user(db, "viewer-user", "view-key", role="viewer", projects=["demo"])
        _provision_user(db, "sup-user", "sup-key", role="supervisor", projects=["demo"])
        for key in ("view-key", "sup-key"):
            status, body = _post(base_url, _blob_for("demo"), token=key)
            assert status == 401
            assert json.loads(body)["error"] == "unauthorized"


def test_revoked_contributor_cannot_push(tmp_path):
    """A revoked contributor's key stops pushing on its very next request (401).

    Why this matters: authZ trusts current DB state, not a cached credential. Revocation
    (active=0) takes effect immediately — a decommissioned push machine's key is dead the
    next time it fires, with no server-side session to expire.
    """
    with _running_relay(tmp_path) as (base_url, db):
        uid = _provision_user(
            db, "mac", "contrib-key", role="contributor", projects=["demo"]
        )
        assert _post(base_url, _blob_for("demo"), token="contrib-key")[0] == 201  # works first
        conn = open_relay_store(db)
        try:
            revoke_user(conn, uid)
        finally:
            conn.close()
        assert _post(base_url, _blob_for("demo"), token="contrib-key")[0] == 401


def test_contributor_report_is_attributed_through_the_read_api(tmp_path):
    """A contributor's pushed report surfaces its author_name; a legacy push surfaces null.

    Why this matters: the C3 Inc 2 report-attribution promise, end to end — the producer's
    server-derived name rides from ingest through the store to BOTH read shapes the dashboard
    uses (the project timeline and the single-report page), while a legacy push stays anonymous
    (null), so old/shared-token reports render with no "pushed by".
    """
    with _running_relay(tmp_path) as (base_url, db):
        _provision_user(db, "Teammate B", "contrib-key", role="contributor", projects=["demo"])
        _provision_user(db, "root", "admin-key", role="admin")  # reads via the cookie SPA API
        assert _post(base_url, _blob_for("demo"), token="contrib-key")[0] == 201  # attributed
        assert _post(base_url, _blob_for("demo"), token=_TOKEN)[0] == 201  # legacy, anonymous

        cookie = _login(base_url, "admin-key")

        # Project timeline (serialize_project): newest first — legacy then the contributor's.
        code, body = _get(base_url, "/api/projects/demo", cookie=cookie)
        assert code == 200
        timeline = json.loads(body)["reports"]
        assert [r["author_name"] for r in timeline] == [None, "Teammate B"]

        # Single-report page (serialize_report) for the attributed report.
        attributed_id = timeline[1]["id"]
        code, rbody = _get(base_url, f"/api/reports/{attributed_id}", cookie=cookie)
        assert code == 200
        detail = json.loads(rbody)
        assert detail["author_name"] == "Teammate B"
        assert "author_id" not in detail  # internal id never on the wire


def test_two_contributors_get_separate_producer_checklists(tmp_path):
    """Two contributors pushing checklists surface as two producer_checklists; legacy stays out.

    Why this matters: the C3 Inc 2 per-producer promise, end to end. Each identified producer's
    checklist is dual-written and shows as its own entry (name + progress + rows), while the
    aggregate stays last-writer-wins and a legacy push contributes only to the aggregate — never
    a producer card. We assert two producer entries with the right names/progress and that a
    legacy push adds none.
    """
    with _running_relay(tmp_path) as (base_url, db):
        _provision_user(db, "Teammate B", "b-key", role="contributor", projects=["demo"])
        _provision_user(db, "Teammate C", "c-key", role="contributor", projects=["demo"])
        _provision_user(db, "root", "admin-key", role="admin")

        # Each contributor pushes its OWN checklist via /checklist (Bearer, their own key).
        def push_checklist(project, token, items):
            body = json.dumps({"project": project, "checklist": items}).encode("utf-8")
            return _post(base_url, body, token=token, path="/checklist")

        assert push_checklist("demo", "b-key", [
            {"text": "B task 1", "done": True}, {"text": "B task 2", "done": False},
        ])[0] == 200
        assert push_checklist("demo", "c-key", [{"text": "C task", "done": False}])[0] == 200
        # A legacy push updates the aggregate only — no producer card.
        assert push_checklist("demo", _TOKEN, [{"text": "legacy", "done": False}])[0] == 200

        cookie = _login(base_url, "admin-key")
        code, body = _get(base_url, "/api/projects/demo", cookie=cookie)
        assert code == 200
        producers = json.loads(body)["producer_checklists"]

        assert [p["author_name"] for p in producers] == ["Teammate B", "Teammate C"]  # by name
        by_name = {p["author_name"]: p for p in producers}
        assert by_name["Teammate B"]["progress"] == {"done": 1, "total": 2, "pct": 50}
        assert [i["text"] for i in by_name["Teammate B"]["items"]] == ["B task 1", "B task 2"]
        assert by_name["Teammate C"]["progress"] == {"done": 0, "total": 1, "pct": 0}


def test_effective_checklist_agrees_across_portfolio_project_and_scheduling(tmp_path):
    """After two identified pushes, the portfolio badge, project stats, and scheduling all
    reflect the SAME merged (done-OR) checklist — KI-30's fix end to end.

    Scenario: B and C both track "Ship" and "Docs". B has Ship done / Docs open; C has Ship
    open (with a long-overdue due date) / Docs done. The merged effective checklist OR-s done,
    so BOTH items read done. We assert:
      - the project page's stats.progress and the portfolio badge AGREE at 2/2 done (the
        aggregate, last-writer C, would show only 1/2);
      - scheduling shows no open "Ship" deadline for demo, even though C's aggregate copy left
        it open and overdue — proving the timeline reads the effective view too, not the
        last-writer aggregate. All three surfaces would disagree if any still read get_checklist.
    """
    with _running_relay(tmp_path) as (base_url, db):
        _provision_user(db, "Teammate B", "b-key", role="contributor", projects=["demo"])
        _provision_user(db, "Teammate C", "c-key", role="contributor", projects=["demo"])
        _provision_user(db, "root", "admin-key", role="admin")

        def push_checklist(token, items):
            body = json.dumps({"project": "demo", "checklist": items}).encode("utf-8")
            return _post(base_url, body, token=token, path="/checklist")

        # B first, then C — so the aggregate (last-writer) ends up as C's copy (Ship OPEN).
        assert push_checklist("b-key", [
            {"text": "Ship", "done": True}, {"text": "Docs", "done": False},
        ])[0] == 200
        # A past due date so "Ship" is unambiguously overdue regardless of the real test clock.
        assert push_checklist("c-key", [
            {"text": "Ship", "done": False, "due_date": "2020-01-01"}, {"text": "Docs", "done": True},
        ])[0] == 200

        cookie = _login(base_url, "admin-key")

        # (1) Project page: stats computed from the effective checklist → both items done.
        code, body = _get(base_url, "/api/projects/demo", cookie=cookie)
        assert code == 200
        project_progress = json.loads(body)["stats"]["progress"]
        assert project_progress == {"done": 2, "total": 2, "pct": 100}

        # (2) Portfolio badge: precomputed counts also from the effective checklist → AGREES.
        code, body = _get(base_url, "/api/portfolio", cookie=cookie)
        assert code == 200
        # demo has default kind "project", so it lands in the "projects" section.
        demo_entry = next(e for e in json.loads(body)["projects"] if e["name"] == "demo")
        assert demo_entry["progress"] == project_progress  # portfolio and project agree

        # (3) Scheduling: the merged Ship is done, so it is off the timeline entirely — the
        # aggregate's still-open, overdue Ship must NOT surface here.
        code, body = _get(base_url, "/api/scheduling", cookie=cookie)
        assert code == 200
        buckets = json.loads(body)["buckets"]
        demo_labels = [
            row["label"]
            for rows in buckets.values()
            for row in rows
            if row["source"]["name"] == "demo"
        ]
        assert "Ship" not in demo_labels  # done in the merged view → no open deadline


def test_legacy_shared_token_still_ingests_by_default(tmp_path):
    """The shared ingest token keeps working (anonymously, unrestricted) while enabled.

    Why this matters: decision #4 — a machine credential must not silently expire. Even after
    named contributors exist, the shared token still ingests by default (any project, no
    scope), so existing crons do not break the moment the first contributor is provisioned.
    """
    with _running_relay(tmp_path) as (base_url, db):
        _provision_user(db, "mac", "contrib-key", role="contributor", projects=["demo"])
        # Shared token, and a project the contributor is NOT granted — legacy is unrestricted.
        assert _post(base_url, _blob_for("anything"), token=_TOKEN)[0] == 201


def test_disable_legacy_ingest_rejects_shared_token_but_contributor_still_pushes(tmp_path):
    """--disable-legacy-ingest 401s the shared token while named keys keep pushing.

    Why this matters: the deliberate cutover. Once every producer has its own key, the
    operator retires the shared token; from then on only named per-user keys can push, and the
    shared token is dead — the moment the operator (not a silent expiry) chooses.
    """
    auth = AuthConfig(session_key=_SKEY, user_pepper=_PEPPER, disable_legacy_ingest=True)
    with _running_relay(tmp_path, auth=auth) as (base_url, db):
        _provision_user(db, "mac", "contrib-key", role="contributor", projects=["demo"])
        assert _post(base_url, _blob_for("demo"), token=_TOKEN)[0] == 401  # shared token dead
        assert _post(base_url, _blob_for("demo"), token="contrib-key")[0] == 201  # own key lives


def test_legacy_ingest_use_logs_a_line_but_identified_push_does_not(tmp_path, capfd):
    """Each legacy shared-token push logs a line; a named contributor push does not.

    Why this matters: decision #4 makes retirement operator-driven, and the operator's signal
    for "the shared token has gone quiet" is this log line. It must fire for a legacy push
    (naming the route, never the token) and NOT for an identified push, so a quiet log means
    every producer has actually migrated to its own key.
    """
    with _running_relay(tmp_path) as (base_url, db):
        _provision_user(db, "mac", "contrib-key", role="contributor", projects=["demo"])
        capfd.readouterr()  # drain startup/setup noise

        _post(base_url, _blob_for("demo"), token=_TOKEN)  # legacy shared token
        legacy_err = capfd.readouterr().err
        assert "legacy shared ingest token used" in legacy_err
        assert "POST /ingest" in legacy_err
        assert _TOKEN not in legacy_err  # the token itself is never logged

        _post(base_url, _blob_for("demo"), token="contrib-key")  # named key
        identified_err = capfd.readouterr().err
        assert "legacy shared ingest token used" not in identified_err


def test_contributor_discussion_read_is_scoped(tmp_path):
    """A contributor's Bearer discussion pull sees granted threads; out-of-scope reads [].

    Why this matters (decided 2026-07-07): reads are scoped too. An out-of-scope project is
    returned as the EMPTY thread — byte-identical to an unknown project — so a contributor can
    neither read nor even confirm the existence of a project outside its grants.
    """
    with _running_relay(tmp_path) as (base_url, db):
        _provision_user(db, "mac", "contrib-key", role="contributor", projects=["demo"])
        _ingest_project(base_url, "demo")
        _ingest_project(base_url, "secret-proj")
        _seed_discussion(db, "demo", [("Supervisor A", "supervisor", "hi demo")])
        _seed_discussion(db, "secret-proj", [("Supervisor A", "supervisor", "hi secret")])

        code, body = _get(
            base_url, _api_discussions_path(project="demo"), bearer="contrib-key"
        )
        assert code == 200
        assert [d["body"] for d in json.loads(body)["discussions"]] == ["hi demo"]

        code2, body2 = _get(
            base_url, _api_discussions_path(project="secret-proj"), bearer="contrib-key"
        )
        assert code2 == 200 and json.loads(body2)["discussions"] == []


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


# --- Shared HTTP test helpers (a no-follow-redirect opener + a one-report seeder) ---
#
# Used across the relay tests below (logout redirects, the discussion write/read, etc.).
# They live here for historical reasons; nothing about them is comment-specific.


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
    """Push one real report and return its id (a target for tests needing a report).

    Why:
        Many tests need an existing report to act on (a discussion thread, a scope
        check). Ingest is Bearer-authed and independent of the view secret, so this
        works whether or not the relay has a view secret set.
    """
    status, body = _post(base_url, _real_blob_json().encode("utf-8"))
    assert status == 201
    return json.loads(body)["id"]


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


def test_create_contributor_role_user_provisions(tmp_path):
    """A "contributor" role provisions cleanly — it is in the allowlist (201, echoed back).

    Why this matters: contributor is the C3 Inc 2 producer identity. Adding it to
    _PROVISIONABLE_ROLES must let it through the same 400 role-gate that rejects unknown
    roles (see test_create_user_invalid_role_is_400), so an admin can actually mint one. We
    assert the 201 echoes role "contributor" and the requested scope.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, _db):
        status, payload = _admin_post(
            base_url,
            "/api/users",
            {"name": "mac", "role": "contributor", "projects": ["demo"]},
        )
        assert status == 201
        assert payload["role"] == "contributor"
        assert payload["projects"] == ["demo"]


def test_contributor_key_cannot_login(tmp_path):
    """A contributor key is push-only: /api/login rejects it exactly like an unknown key.

    Why this matters: this is the permanent invariant of C3 Inc 2 — one credential never
    spans both auth worlds. A contributor's key may authenticate the Bearer ingest path
    (Unit 1.2) but must NEVER mint a dashboard session. The rejection is the GENERIC 401
    {"ok": false} with no Set-Cookie, byte-identical to a bad key, so the login form can't
    tell a producer identity from a miss. We also log in an interactive role through the SAME
    machinery first, proving the rejection is about the ROLE, not a broken key/pepper setup.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        _provision_user(db, "mac", "contrib-key", role="contributor", projects=["demo"])
        _provision_user(db, "Teammate B", "admin-key", role="admin")

        # Control: an interactive role logs in fine through this exact flow.
        good, good_body, _ = _post_api_json(base_url, "/api/login", {"key": "admin-key"})
        assert good == 200 and good_body["ok"] is True

        # The contributor key is refused with the generic miss — no session minted.
        status, body, headers = _post_api_json(
            base_url, "/api/login", {"key": "contrib-key"}
        )
        assert status == 401 and body == {"ok": False}
        assert _SESSION_COOKIE_NAME not in (headers.get("Set-Cookie") or "")


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


def test_grant_widens_scope_so_a_contributor_can_push_the_new_project(tmp_path):
    """POST /api/users/grant adds projects; the contributor can then push the new one (KI-31).

    Why this matters: a contributor's scope was frozen at creation, stranding a multi-project
    producer. We provision a contributor scoped to 'demo', confirm 'other' 404s, grant 'other',
    then confirm the SAME key now pushes 'other' (201) and the response echoes the full new scope.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "prod", "prod-key", role="contributor", projects=["demo"])
        assert _post(base_url, _blob_for("other"), token="prod-key")[0] == 404  # out of scope

        status, r = _admin_post(
            base_url, "/api/users/grant", {"name": "prod", "projects": ["other"]}
        )
        assert status == 200
        assert set(r["projects"]) == {"demo", "other"}  # full scope after the grant

        assert _post(base_url, _blob_for("other"), token="prod-key")[0] == 201  # now in scope


def test_grant_is_idempotent_and_requires_a_project(tmp_path):
    """Re-granting a held project is a no-op; an empty project list is a 400."""
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "prod", "prod-key", role="contributor", projects=["demo"])
        status, r = _admin_post(
            base_url, "/api/users/grant", {"name": "prod", "projects": ["demo"]}
        )
        assert status == 200 and set(r["projects"]) == {"demo"}  # idempotent
        assert _admin_post(
            base_url, "/api/users/grant", {"name": "prod", "projects": []}
        )[0] == 400


def test_key_add_lets_two_keys_push_under_one_account(tmp_path):
    """The two-machine case: add a second key, and BOTH keys push as the same identity.

    Why this matters: this is what the whole credential split exists to deliver. Before it,
    a second machine meant a second identity (or re-keying the first). Both pushes must also
    be attributed to the SAME account name, or "one identity, two machines" is a fiction.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "prod", "mac-key", role="contributor", projects=["demo"])
        assert _post(base_url, _blob_for("demo"), token="mac-key")[0] == 201

        status, r = _admin_post(
            base_url, "/api/users/key-add", {"name": "prod", "label": "wsl2"}
        )
        assert status == 200
        wsl_key = r["key"]
        assert wsl_key and len(wsl_key) >= 40 and wsl_key != "mac-key"

        # BOTH keys now work — adding never disturbs the existing credential.
        assert _post(base_url, _blob_for("demo"), token="mac-key")[0] == 201
        assert _post(base_url, _blob_for("demo"), token=wsl_key)[0] == 201

        conn = open_relay_store(db)
        authors = {row["author_name"] for row in conn.execute("SELECT author_name FROM relay_reports")}
        assert authors == {"prod"}  # one identity, whichever machine pushed


def test_key_revoke_kills_one_credential_and_leaves_the_other(tmp_path):
    """Revoking one credential leaves the account and its other keys working.

    Why this matters: the point of the split — a lost laptop costs ONE key, not the identity.
    This is the property the retired `rotate` could not express, since it replaced the single
    key wholesale.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "prod", "mac-key", role="contributor", projects=["demo"])
        wsl_key = _admin_post(
            base_url, "/api/users/key-add", {"name": "prod", "label": "wsl2"}
        )[1]["key"]

        listed = _admin_post(base_url, "/api/users/key-list", {"name": "prod"})[1]["credentials"]
        wsl_id = next(c["id"] for c in listed if c["label"] == "wsl2")
        assert all("verifier" not in c for c in listed)  # never leaks key material

        assert _admin_post(
            base_url, "/api/users/key-revoke", {"name": "prod", "id": wsl_id}
        )[0] == 200
        assert _post(base_url, _blob_for("demo"), token=wsl_key)[0] == 401   # revoked
        assert _post(base_url, _blob_for("demo"), token="mac-key")[0] == 201  # untouched


def test_key_revoke_does_not_log_the_human_out(tmp_path):
    """Revoking a machine key leaves the account's live dashboard session valid.

    Why this matters: amendment 9 — credential and session lifecycles are separate. Losing a
    machine key must not force the human to log in again everywhere; only a password change or
    an account revocation does that. Coupling them would make people delay revocations.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "human", "login-key", role="admin")
        cookie = _login(base_url, "login-key")
        assert cookie is not None
        machine_id = _admin_post(
            base_url, "/api/users/key-add", {"name": "human", "label": "mac"}
        )[1]["id"]

        assert _admin_post(
            base_url, "/api/users/key-revoke", {"name": "human", "id": machine_id}
        )[0] == 200
        assert _get(base_url, "/api/portfolio", cookie=cookie)[0] == 200  # session survives


def test_key_revoke_of_another_accounts_credential_is_404(tmp_path):
    """A credential id belonging to a different account cannot be revoked through this name.

    Why this matters: the id is a global autoincrement, so without an ownership check a typo'd
    id would silently revoke someone else's credential — a cross-account action from a
    single-account request. Existence-hiding 404, consistent with every other scope boundary.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "alice", "a-key", role="contributor", projects=["demo"])
        _provision_user(db, "bob", "b-key", role="contributor", projects=["demo"])
        bob_id = _admin_post(base_url, "/api/users/key-list", {"name": "bob"})[1]["credentials"][0]["id"]

        assert _admin_post(
            base_url, "/api/users/key-revoke", {"name": "alice", "id": bob_id}
        )[0] == 404
        assert _post(base_url, _blob_for("demo"), token="b-key")[0] == 201  # bob unaffected


def test_key_add_rejects_a_duplicate_active_label(tmp_path):
    """Two active credentials cannot share a label on one account (409, not a 500).

    Why this matters: labels are how a human tells credentials apart when deciding what to
    revoke, so duplicates would defeat their only purpose. The DB index enforces it; the
    handler turns that into a clear 409 rather than leaking an IntegrityError as a 500.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "prod", "k", role="contributor", projects=["demo"])
        assert _admin_post(base_url, "/api/users/key-add", {"name": "prod", "label": "mac"})[0] == 200
        assert _admin_post(base_url, "/api/users/key-add", {"name": "prod", "label": "mac"})[0] == 409


def test_a_bearer_key_on_an_admin_account_is_scoped_to_its_grants(tmp_path):
    """THE bounded-authority invariant: an admin account's key pushes ONLY to granted projects.

    Why this matters: amendment 1, and the reason it is a permanent invariant. The
    multi-credential model actively invites attaching a machine key to a human's account — and
    that human is often an admin. Under the old rule (role admin ⇒ unrestricted) that machine
    would silently gain push access to EVERY project, making compartmentalization worse than
    the single-key model this replaces. A key is a machine credential, so it carries
    contributor authority regardless of who owns it.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        # An ADMIN account, granted exactly one project.
        _provision_user(db, "root", "admin-key", role="admin", projects=["demo"])

        assert _post(base_url, _blob_for("demo"), token="admin-key")[0] == 201    # granted
        assert _post(base_url, _blob_for("secret"), token="admin-key")[0] == 404  # NOT granted


def test_an_admin_account_with_no_grants_can_push_nowhere(tmp_path):
    """An admin account with zero grants pushes to nothing at all (default-deny, not a bypass).

    Why this matters: the sharper edge of the same invariant. Admin accounts routinely have no
    grants precisely because the old rule made grants meaningless for them. Bounded authority
    must therefore mean "scoped to grants" even when that set is empty — if an empty grant set
    fell back to unrestricted, the invariant would be inverted exactly where it matters most.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "root", "admin-key", role="admin")  # no projects

        assert _post(base_url, _blob_for("demo"), token="admin-key")[0] == 404


def test_an_admin_key_still_reads_everything_through_the_cookie(tmp_path):
    """The Bearer bound does NOT shrink an admin's dashboard scope after logging in.

    Why this matters: amendment 1 bounds the PUSH path only. An admin logging in to the
    dashboard is a human exercising human authority, and must still see everything — otherwise
    "keys are contributor-bounded" would have quietly demoted the admin role itself.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "root", "admin-key", role="admin")  # no grants at all
        _provision_user(db, "prod", "prod-key", role="contributor", projects=["demo"])
        assert _post(base_url, _blob_for("demo"), token="prod-key")[0] == 201

        cookie = _login(base_url, "admin-key")
        status, body = _get(base_url, "/api/portfolio", cookie=cookie)
        assert status == 200
        assert any(e["name"] == "demo" for e in json.loads(body)["projects"])


def test_a_verifier_planted_only_in_the_legacy_column_authenticates_nothing(tmp_path):
    """A valid-looking verifier written ONLY to relay_users.key_verifier is dead on both paths.

    Why this matters: amendment 8's shadow-credential test. The legacy column is NOT NULL and
    cannot be dropped, so it still exists and still holds a value on every row. This proves
    nothing reads it any more: a correctly-computed verifier placed there — the exact thing
    that used to authenticate — grants neither a session nor a push.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "prod", "real-key", role="admin", projects=["demo"])
        # Plant a REAL, correctly-computed verifier for "shadow-key" in the retired column.
        conn = open_relay_store(db)
        conn.execute(
            "UPDATE relay_users SET key_verifier = ? WHERE name = ?",
            (key_verifier(_PEPPER, "shadow-key"), "prod"),
        )
        conn.commit()
        conn.close()

        assert _login(base_url, "shadow-key") is None                          # no session
        assert _post(base_url, _blob_for("demo"), token="shadow-key")[0] == 401  # no push
        assert _login(base_url, "real-key") is not None                        # the real one still works


def test_the_retired_rotate_endpoint_is_gone(tmp_path):
    """POST /api/users/rotate no longer exists.

    Why this matters: rotate was retired deliberately, not merely deprecated — its one-shot
    semantics open a silent-401 window on scheduled machines and can strand an operator whose
    response is lost. An endpoint that quietly still worked would keep that hazard alive.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "prod", "k", role="contributor", projects=["demo"])
        assert _admin_post(base_url, "/api/users/rotate", {"name": "prod"})[0] == 404


# --- Unit 3: password login, timing parity, and throttling --------------------------


def _password_login(base_url, name, password):
    """POST /api/login in name+password mode; return (status, cookie_or_None)."""
    data = json.dumps({"name": name, "password": password}).encode("utf-8")
    req = urllib.request.Request(
        base_url + "/api/login", data=data,
        headers={"Content-Type": "application/json", "Origin": base_url}, method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=10) as response:
            set_cookie = response.headers.get("Set-Cookie") or ""
            return response.status, (set_cookie.split(";")[0].split("=", 1)[1] if set_cookie else None)
    except urllib.error.HTTPError as exc:
        return exc.code, None


def test_password_login_works_and_a_wrong_password_is_a_generic_401(tmp_path):
    """A supervisor logs in with name+password; a wrong password is an indistinguishable 401.

    Why this matters: the whole point of the unit — a human logs in with something they know
    rather than pasting stored key material. The failure must carry no detail: the same status
    and the same body as every other failure class.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "dad", "dad-key", role="supervisor", projects=["demo"])
        assert _admin_post(
            base_url, "/api/users/password", {"name": "dad", "password": "a-real-passphrase"}
        )[0] == 200

        status, cookie = _password_login(base_url, "dad", "a-real-passphrase")
        assert status == 200 and cookie
        assert _password_login(base_url, "dad", "not-the-password") == (401, None)


def test_every_failure_class_runs_exactly_one_argon2_verification(tmp_path, monkeypatch):
    """Unknown name, no-password account, wrong password and lockout each verify exactly once.

    Why this matters: this is the timing-parity guarantee, asserted by COUNTING the
    verifications rather than by measuring wall-clock (which would be flaky under CI load).
    If an unknown name skipped the hash, its response would return measurably sooner and the
    response time would answer "does this account exist?" — the exact enumeration the shared
    generic 401 exists to prevent.
    """
    from relay import passwords

    calls = []
    real_verify = passwords.verify_password

    def counting_verify(stored_hash, password):
        calls.append(stored_hash is not None)
        return real_verify(stored_hash, password)

    monkeypatch.setattr("relay.server.verify_password", counting_verify)

    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "haspw", "k1", role="supervisor", projects=["demo"])
        _provision_user(db, "nopw", "k2", role="supervisor", projects=["demo"])
        assert _admin_post(
            base_url, "/api/users/password", {"name": "haspw", "password": "correct-pass"}
        )[0] == 200

        calls.clear()
        assert _password_login(base_url, "ghost", "x")[0] == 401       # unknown name
        assert _password_login(base_url, "nopw", "x")[0] == 401        # exists, no password
        assert _password_login(base_url, "haspw", "wrong")[0] == 401   # wrong password
        assert _password_login(base_url, "haspw", "correct-pass")[0] == 200

        # Four attempts, four verifications — no path short-circuits.
        assert len(calls) == 4
        # Only the two touching a real credential had a stored hash; the other two were
        # verified against the dummy, which is what equalises their cost.
        assert calls == [False, False, True, True]


def test_login_locks_out_after_repeated_failures_and_admin_unlock_clears_it(tmp_path):
    """Five failures lock the account; the correct password then fails; unlock restores it.

    Why this matters: passwords are guessable in a way 256-bit keys are not, so throttling is
    what makes password login safe at all. The lockout must bite even for the CORRECT password
    (otherwise it is not a lockout), and the admin unlock must fully restore access without
    changing a password the person still knows.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "dad", "dad-key", role="supervisor", projects=["demo"])
        assert _admin_post(
            base_url, "/api/users/password", {"name": "dad", "password": "right-pass"}
        )[0] == 200

        for _ in range(5):
            assert _password_login(base_url, "dad", "wrong")[0] == 401
        # Locked: even the correct password is refused, with the same generic 401.
        assert _password_login(base_url, "dad", "right-pass") == (401, None)

        assert _admin_post(base_url, "/api/users/unlock", {"name": "dad"})[0] == 200
        assert _password_login(base_url, "dad", "right-pass")[0] == 200


def test_a_successful_login_clears_the_failure_count(tmp_path):
    """Failures below the threshold are forgotten once a real login succeeds.

    Why this matters: without a reset, a person who mistypes occasionally over days would
    eventually be locked out by accumulated failures that were never an attack.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "dad", "dad-key", role="supervisor", projects=["demo"])
        _admin_post(base_url, "/api/users/password", {"name": "dad", "password": "right-pass"})

        for _ in range(4):
            assert _password_login(base_url, "dad", "wrong")[0] == 401
        assert _password_login(base_url, "dad", "right-pass")[0] == 200
        # The counter reset, so four more failures still do not lock the account.
        for _ in range(4):
            assert _password_login(base_url, "dad", "wrong")[0] == 401
        assert _password_login(base_url, "dad", "right-pass")[0] == 200


def test_a_password_never_authenticates_the_bearer_path(tmp_path):
    """A valid password presented as a Bearer token is refused.

    Why this matters: "one credential never spans both auth worlds". A password is an
    interactive credential; if it also worked as a machine token, the compartmentalisation
    this whole arc is built on would be an illusion.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "admin1", "admin-key", role="admin", projects=["demo"])
        _admin_post(base_url, "/api/users/password", {"name": "admin1", "password": "pw-secret"})

        assert _post(base_url, _blob_for("demo"), token="pw-secret")[0] == 401


def test_setting_a_password_retires_key_login_but_not_key_pushes(tmp_path):
    """Once an account has a password, its KEY stops logging in — but still pushes.

    Why this matters: open decision 3, and the endpoint of the transition. Passwords become
    THE interactive credential and keys become machine-only. The second half is the subtle
    part: retiring key LOGIN must not break the same account's machine pushes, or setting a
    password would silently break a producer.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "admin1", "admin-key", role="admin", projects=["demo"])
        assert _login(base_url, "admin-key") is not None       # key login works beforehand
        assert _post(base_url, _blob_for("demo"), token="admin-key")[0] == 201

        _admin_post(base_url, "/api/users/password", {"name": "admin1", "password": "pw-secret"})

        assert _login(base_url, "admin-key") is None            # key login retired
        assert _password_login(base_url, "admin1", "pw-secret")[0] == 200  # password works
        assert _post(base_url, _blob_for("demo"), token="admin-key")[0] == 201  # pushes unaffected


def test_key_login_keeps_working_until_a_password_is_set(tmp_path):
    """An account with no password still logs in with its key — no lockout mid-migration.

    Why this matters: the transition has to be safe. Every existing account starts without a
    password, so if key login stopped working the moment this unit deployed, everyone would be
    locked out of the dashboard at once.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "dad", "dad-key", role="supervisor", projects=["demo"])
        assert _login(base_url, "dad-key") is not None


def test_setting_a_password_logs_out_live_sessions(tmp_path):
    """A password change invalidates cookies minted before it.

    Why this matters: amendment 9 draws the line here — a machine key revocation does NOT log
    the human out, but a password change DOES. A password change is the response to a
    suspected compromise of the human's own credential, so any session opened under the old
    one must die with it.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "dad", "dad-key", role="supervisor", projects=["demo"])
        cookie = _login(base_url, "dad-key")
        assert _get(base_url, "/api/portfolio", cookie=cookie)[0] == 200

        _admin_post(base_url, "/api/users/password", {"name": "dad", "password": "new-pass"})
        assert _get(base_url, "/api/portfolio", cookie=cookie)[0] in (401, 403)


def test_a_contributor_cannot_be_given_a_password(tmp_path):
    """Setting a password on a push-only account is refused (409).

    Why this matters: the same invariant from the other direction. A contributor is a machine
    identity; giving it a password would create a credential that spans both worlds and could
    mint an interactive session for something that should only ever push.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "bot", "bot-key", role="contributor", projects=["demo"])
        assert _admin_post(
            base_url, "/api/users/password", {"name": "bot", "password": "pw"}
        )[0] == 409


def test_password_set_can_mint_one_and_shows_it_once(tmp_path):
    """Omitting the password makes the relay mint a strong one and return it.

    Why this matters: provisioning someone else's account. The admin should not have to invent
    a password, and the minted value is returned exactly once — the store keeps only its hash.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "dad", "dad-key", role="supervisor", projects=["demo"])
        status, r = _admin_post(base_url, "/api/users/password", {"name": "dad"})
        assert status == 200
        minted = r["password"]
        assert minted and len(minted) >= 20
        assert _password_login(base_url, "dad", minted)[0] == 200


def test_replacing_a_password_retires_the_old_one(tmp_path):
    """Setting a second password invalidates the first and leaves exactly one active.

    Why this matters: the one-active-password index permits a single row, so replacement must
    revoke before inserting. If the old credential stayed active the account would have two
    valid passwords, and the DB constraint would reject the write outright.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "dad", "dad-key", role="supervisor", projects=["demo"])
        _admin_post(base_url, "/api/users/password", {"name": "dad", "password": "first-pass"})
        _admin_post(base_url, "/api/users/password", {"name": "dad", "password": "second-pass"})

        assert _password_login(base_url, "dad", "first-pass")[0] == 401
        assert _password_login(base_url, "dad", "second-pass")[0] == 200
        conn = open_relay_store(db)
        active = conn.execute(
            "SELECT COUNT(*) FROM relay_credentials WHERE type='password' AND active=1"
        ).fetchone()[0]
        assert active == 1


def test_login_fails_closed_when_the_hashing_dependency_is_missing(tmp_path, monkeypatch):
    """Without argon2-cffi, password login refuses rather than degrading.

    Why this matters: the fail-closed invariant. The alternatives — falling back to a weaker
    hash, or treating every password as correct/incorrect — are respectively an invisible
    security downgrade and an undiagnosable outage. It must also stay a 401, not a 500: the
    failure class is the operator's business, not an attacker's.
    """
    from relay.passwords import PasswordsUnavailable

    def unavailable(*_a, **_k):
        raise PasswordsUnavailable("argon2-cffi is not installed")

    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "dad", "dad-key", role="supervisor", projects=["demo"])
        _admin_post(base_url, "/api/users/password", {"name": "dad", "password": "right-pass"})
        monkeypatch.setattr("relay.server.verify_password", unavailable)
        assert _password_login(base_url, "dad", "right-pass") == (401, None)


def test_a_malformed_stored_hash_is_an_auth_failure_not_a_500(tmp_path):
    """A corrupt value in the password column denies login without crashing.

    Why this matters: a 500 would leak that this account is special (its hash is broken) and
    would turn a data problem into an error page. Corruption should look exactly like a wrong
    password to the person, and be visible to the operator in the logs.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "dad", "dad-key", role="supervisor", projects=["demo"])
        _admin_post(base_url, "/api/users/password", {"name": "dad", "password": "right-pass"})
        conn = open_relay_store(db)
        conn.execute(
            "UPDATE relay_credentials SET verifier='not-an-argon2-hash' WHERE type='password'"
        )
        conn.commit()
        conn.close()

        assert _password_login(base_url, "dad", "right-pass") == (401, None)


def test_role_change_rescopes_an_account_and_logs_it_out(tmp_path):
    """Demoting an admin to supervisor applies default-deny and invalidates its live session.

    Why this matters: the exact operation planned for a real supervisor account. Two things
    must hold together — the live cookie must not outlive the authority it was minted under,
    and the demoted account must be scoped by grants (here: none, so it sees nothing until
    granted). The second is the operational trap the CLI warns about.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "dad", "dad-key", role="admin")
        cookie = _login(base_url, "dad-key")
        assert _get(base_url, "/api/portfolio", cookie=cookie)[0] == 200

        status, r = _admin_post(base_url, "/api/users/role", {"name": "dad", "role": "supervisor"})
        assert status == 200 and r["role"] == "supervisor" and r["projects"] == []

        # The pre-change cookie is dead (session_version bumped).
        assert _get(base_url, "/api/portfolio", cookie=cookie)[0] in (401, 403)
        # Re-login works, but now sees nothing until granted.
        fresh = _login(base_url, "dad-key")
        assert fresh is not None
        assert json.loads(_get(base_url, "/api/portfolio", cookie=fresh)[1])["projects"] == []


def test_role_change_rejects_an_unknown_role(tmp_path):
    """An out-of-set role is a 400 and leaves the account untouched.

    Why this matters: role is an open enum in the DB, so nothing at the storage layer would
    stop 'supervisorr' from being written — and an account with a typo'd role would silently
    match no allowlist anywhere, failing closed in a way that looks like a bug.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "dad", "dad-key", role="admin")
        assert _admin_post(base_url, "/api/users/role", {"name": "dad", "role": "wizard"})[0] == 400
        assert _login(base_url, "dad-key") is not None  # still an admin, still works


def test_rename_preserves_attributed_history(tmp_path):
    """Renaming an account keeps its past reports under the name they were pushed with.

    Why this matters: `author_name` is denormalized precisely so recorded history survives the
    account changing or being deleted. A rename that rewrote history would make the report log
    disagree with what was actually sent at the time.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "macos", "k", role="contributor", projects=["demo"])
        assert _post(base_url, _blob_for("demo"), token="k")[0] == 201

        assert _admin_post(
            base_url, "/api/users/rename", {"name": "macos", "new_name": "mac-mini"}
        )[0] == 200

        conn = open_relay_store(db)
        assert conn.execute("SELECT author_name FROM relay_reports").fetchone()[0] == "macos"
        assert get_user_by_name(conn, "mac-mini") is not None
        assert get_user_by_name(conn, "macos") is None
        conn.close()
        # The key still works — a rename is a label change, not a re-provisioning.
        assert _post(base_url, _blob_for("demo"), token="k")[0] == 201


def test_rename_to_a_taken_name_is_409(tmp_path):
    """Renaming onto an existing name is refused, not silently collapsed.

    Why this matters: names are UNIQUE and are the CLI's handle for every admin verb. Allowing
    a collision would make `revoke <name>` ambiguous at exactly the wrong moment.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "alice", "a", role="contributor", projects=["demo"])
        _provision_user(db, "bob", "b", role="contributor", projects=["demo"])
        assert _admin_post(
            base_url, "/api/users/rename", {"name": "alice", "new_name": "bob"}
        )[0] == 409


def test_delete_frees_the_name_and_drops_the_card_but_keeps_report_history(tmp_path):
    """POST /api/users/delete removes the user (name re-addable) + its live card, keeps report author.

    Why this matters: KI-31 + the history/live-state split. A deleted producer's LIVE checklist
    card goes and its name frees, but its past REPORT keeps its denormalized author_name. We push a
    report + checklist as a contributor, delete it, then confirm all three.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "prod", "prod-key", role="contributor", projects=["demo"])
        _provision_user(db, "root", "admin-key", role="admin")  # reads via the cookie API
        assert _post(base_url, _blob_for("demo"), token="prod-key")[0] == 201  # attributed report
        checklist = json.dumps(
            {"project": "demo", "checklist": [{"text": "x", "done": True}]}
        ).encode("utf-8")
        assert _post(base_url, checklist, token="prod-key", path="/checklist")[0] == 200

        status, r = _admin_post(base_url, "/api/users/delete", {"name": "prod"})
        assert status == 200 and r["deleted"] is True

        # The name is free again — re-provisioning it now succeeds (was a 409 while revoked).
        assert _admin_post(
            base_url, "/api/users", {"name": "prod", "projects": ["demo"]}
        )[0] == 201

        cookie = _login(base_url, "admin-key")
        detail = json.loads(_get(base_url, "/api/projects/demo", cookie=cookie)[1])
        assert detail["producer_checklists"] == []  # the deleted producer's live card is gone
        assert detail["reports"][0]["author_name"] == "prod"  # its report keeps the recorded name


def test_admin_verbs_404_unknown_and_require_admin_token(tmp_path):
    """Each new verb 404s an unknown name and refuses the ingest token (admin-gated)."""
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, _db):
        assert _admin_post(
            base_url, "/api/users/grant", {"name": "ghost", "projects": ["demo"]}
        )[0] == 404
        assert _admin_post(base_url, "/api/users/delete", {"name": "ghost"})[0] == 404
        assert _admin_post(base_url, "/api/users/key-add", {"name": "ghost", "label": "l"})[0] == 404
        assert _admin_post(base_url, "/api/users/key-list", {"name": "ghost"})[0] == 404
        assert _admin_post(base_url, "/api/users/role", {"name": "ghost", "role": "viewer"})[0] == 404
        assert _admin_post(base_url, "/api/users/rename", {"name": "ghost", "new_name": "g2"})[0] == 404
        for path, obj in (
            ("/api/users/grant", {"name": "x", "projects": ["demo"]}),
            ("/api/users/delete", {"name": "x"}),
            ("/api/users/key-add", {"name": "x", "label": "l"}),
            ("/api/users/key-list", {"name": "x"}),
            ("/api/users/key-revoke", {"name": "x", "id": 1}),
            ("/api/users/role", {"name": "x", "role": "viewer"}),
            ("/api/users/rename", {"name": "x", "new_name": "y"}),
        ):
            assert _admin_post(base_url, path, obj, token=_TOKEN)[0] == 401  # ingest token refused


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


# --- Unit 4a: agent accounts, operator lifecycle, and attribution badging ------------
# An agent is a machine identity (Claude Code, a CI job) that pushes on a human's behalf.
# It is a first-class account (kind='agent', role contributor, its own key) with
# `operated_by` pointing at an accountable human. These tests pin the amendment-5
# lifecycle rules — which are enforced in CODE, not by a foreign key, because this store
# never enables PRAGMA foreign_keys — and the read-time attribution join that badges an
# agent's reports. Folding agents into their operator's producer streams is Unit 4b and is
# deliberately NOT exercised here.


def _admin_users(base_url):
    """GET the admin roster (admin-token authed) and return the parsed users list."""
    status, text = _get(base_url, "/api/users", bearer=_ADMIN)
    assert status == 200, text
    return json.loads(text)["users"]


def _reader_cookie(db, base_url):
    """Log in an admin account and return its session cookie for the /api reads.

    Why:
        The provisioning routes are ADMIN-TOKEN authed, but the dashboard reads
        (/api/projects, /api/reports) are COOKIE authed — two deliberately separate
        surfaces. The attribution tests read the dashboard, so they need a real session,
        not the admin token.
    """
    _provision_user(db, "reader", "reader-key", role="admin")
    return _login(base_url, "reader-key")


def _seed_human_and_agent(db, base_url):
    """Provision a human via the admin API, then an agent operated by them.

    Returns (human_name, agent_name, agent_key) — the agent's raw key for push tests.

    Why:
        The happy path is the setup for most tests below, and going through the real
        endpoint (not the store) is the point: it proves provisioning ACCEPTS a valid
        agent, so the rejection tests that follow are contrasting against a case that
        genuinely works rather than against nothing.
    """
    _admin_post(base_url, "/api/users", {"name": "human-a", "role": "viewer"})
    status, body = _admin_post(
        base_url,
        "/api/users",
        {
            "name": "agent-a",
            "role": "contributor",
            "kind": "agent",
            "operated_by": "human-a",
            "projects": ["alpha"],
        },
    )
    assert status == 201, body
    return "human-a", "agent-a", body["key"]


def test_provisioning_an_agent_records_its_kind_and_operator(tmp_path):
    """The happy path: an agent account is created, badged as such, and echoes its operator.

    Covers the base case the whole unit rests on — without this, every rejection test
    below could pass simply because agent provisioning was broken outright.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        human, agent, key = _seed_human_and_agent(db, base_url)

        # The roster reflects what was stored, so the CLI can show kind + operator.
        by_name = {u["name"]: u for u in _admin_users(base_url)}
        assert by_name[agent]["account_kind"] == "agent"
        assert by_name[agent]["operated_by_name"] == human
        # A human account is untouched by the new fields — it reads as "human"/None, which
        # is exactly what every pre-4a row (a NULL kind) also reads as.
        assert by_name[human]["account_kind"] == "human"
        assert by_name[human]["operated_by_name"] is None


def test_agent_provisioning_rejects_each_invalid_operator(tmp_path):
    """Every amendment-5 rejection case, each with its own reason.

    These are the rules that keep `operated_by` meaningful: it must terminate at a real,
    active, accountable HUMAN. Without them a typo would store a dangling id, a revoked
    person would keep acquiring agents, and an agent->agent chain would turn "who is
    actually responsible for this?" into a graph walk.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        human, agent, _ = _seed_human_and_agent(db, base_url)

        def agent_payload(**overrides):
            payload = {"name": "new-agent", "role": "contributor", "kind": "agent"}
            payload.update(overrides)
            return payload

        # An agent with no operator at all: there would be no accountable human.
        status, body = _admin_post(base_url, "/api/users", agent_payload())
        assert status == 400 and "operated_by" in body["error"]

        # A typo'd operator name — must not silently store a dangling reference.
        status, body = _admin_post(
            base_url, "/api/users", agent_payload(operated_by="nobody")
        )
        assert status == 400 and "nobody" in body["error"]

        # A REVOKED human should not keep acquiring agents.
        _admin_post(base_url, "/api/users/revoke", {"name": human})
        status, body = _admin_post(
            base_url, "/api/users", agent_payload(operated_by=human)
        )
        assert status == 400 and "revoked" in body["error"]

        # An agent operating an agent — the chain the design forbids.
        status, body = _admin_post(
            base_url, "/api/users", agent_payload(operated_by=agent)
        )
        assert status == 400 and "human" in body["error"]

        # An interactive role on a machine account would breach "one credential never
        # spans both auth worlds" — it would give the machine a dashboard login.
        status, body = _admin_post(
            base_url,
            "/api/users",
            agent_payload(role="viewer", operated_by="human-a"),
        )
        assert status == 400 and "contributor" in body["error"]

        # And the mirror case: 'operated_by' on a HUMAN is rejected, not ignored — a
        # silently dropped field would let an admin believe they assigned an operator.
        status, body = _admin_post(
            base_url,
            "/api/users",
            {"name": "h2", "role": "viewer", "operated_by": "human-a"},
        )
        assert status == 400 and "only valid for an agent" in body["error"]


def test_an_agent_cannot_be_promoted_to_an_interactive_role(tmp_path):
    """The two-step back door is closed: add an agent, then promote it -> refused.

    Validating only at provisioning would leave `relay-user role` as a path to exactly the
    interactive machine account the invariant forbids.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _, agent, _ = _seed_human_and_agent(db, base_url)

        status, body = _admin_post(
            base_url, "/api/users/role", {"name": agent, "role": "admin"}
        )
        assert status == 400 and "contributor" in body["error"]

        # A human, by contrast, changes role freely — the guard is agent-specific and
        # must not have broken the ordinary path.
        status, _ = _admin_post(
            base_url, "/api/users/role", {"name": "human-a", "role": "supervisor"}
        )
        assert status == 200


def test_deleting_an_operator_is_blocked_until_its_agents_are_reassigned(tmp_path):
    """An operator with live agents cannot be deleted; set-operator is the escape hatch.

    `operated_by` is not a declared FK, so nothing at the DB layer would stop a delete from
    leaving agents pointing at a vanished id. This is the code-level guard, plus the
    documented way out.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        human, agent, _ = _seed_human_and_agent(db, base_url)

        status, body = _admin_post(base_url, "/api/users/delete", {"name": human})
        assert status == 409
        # The error NAMES the blocker, so the admin knows what to reassign.
        assert agent in body["error"]

        # Reassign to a second human, then the delete succeeds.
        _admin_post(base_url, "/api/users", {"name": "human-b", "role": "viewer"})
        status, _ = _admin_post(
            base_url, "/api/users/set-operator", {"name": agent, "operated_by": "human-b"}
        )
        assert status == 200
        status, _ = _admin_post(base_url, "/api/users/delete", {"name": human})
        assert status == 200


def test_set_operator_rejects_self_reference_and_non_agents(tmp_path):
    """Reassignment enforces the same rules as provisioning, plus self-reference.

    Self-reference is only reachable here (at provisioning the agent has no id yet), and it
    would create an account that is its own accountable human — a cycle with no person at
    the end of it.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        human, agent, _ = _seed_human_and_agent(db, base_url)

        status, body = _admin_post(
            base_url, "/api/users/set-operator", {"name": agent, "operated_by": agent}
        )
        assert status == 400 and "itself" in body["error"]

        # A HUMAN account has no operator to set — reassigning one is a category error.
        status, body = _admin_post(
            base_url, "/api/users/set-operator", {"name": human, "operated_by": human}
        )
        assert status == 400 and "not an agent" in body["error"]

        # An unknown agent 404s, consistent with every other named admin route.
        status, _ = _admin_post(
            base_url, "/api/users/set-operator", {"name": "ghost", "operated_by": human}
        )
        assert status == 404


def test_revoking_an_operator_does_not_revoke_its_agents(tmp_path):
    """Amendment 5's no-silent-cascade rule: each credential revokes individually.

    Revoking a person is an act about that person. Silently killing their agents' keys too
    would take down scheduled pushes nobody asked to stop, and the operator would have no
    signal that it happened.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        human, agent, agent_key = _seed_human_and_agent(db, base_url)

        _admin_post(base_url, "/api/users/revoke", {"name": human})

        # The agent's key still pushes to its granted project.
        code, _ = _post(base_url, _blob_for("alpha"), token=agent_key)
        assert code == 201


def test_an_agent_push_is_badged_with_its_kind_and_operator(tmp_path):
    """The read-time attribution join: an agent's report carries author_kind + operator.

    This is what makes agent work legible on the dashboard without losing provenance — the
    report stays attributed to the AGENT, and names the human it acted for.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        human, agent, agent_key = _seed_human_and_agent(db, base_url)
        _post(base_url, _blob_for("alpha"), token=agent_key)
        cookie = _reader_cookie(db, base_url)

        _, project = _get_json(base_url, "/api/projects/alpha", cookie=cookie)
        entry = project["reports"][0]
        assert entry["author_name"] == agent  # provenance: the AGENT pushed it
        assert entry["author_kind"] == "agent"
        assert entry["operated_by_name"] == human

        # The report detail page agrees with the timeline entry.
        _, report = _get_json(base_url, f"/api/reports/{entry['id']}", cookie=cookie)
        assert report["author_kind"] == "agent"
        assert report["operated_by_name"] == human


def test_a_human_push_and_a_legacy_push_carry_no_badge(tmp_path):
    """Non-agent reports stay exactly as they were: a human badges "human", legacy nulls.

    The null-vs-"human" distinction is load-bearing. A legacy anonymous push genuinely
    carried NO identity, so emitting "human" for it would assert an attribution the relay
    never made — and would badge unattributed history as someone's work.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _provision_user(db, "dev", "dev-key", role="contributor", projects=["alpha"])
        _post(base_url, _blob_for("alpha"), token="dev-key")
        # A legacy shared-token push carries no identity at all.
        _post(base_url, _blob_for("alpha"), token=_TOKEN)
        cookie = _reader_cookie(db, base_url)

        _, project = _get_json(base_url, "/api/projects/alpha", cookie=cookie)
        by_author = {r["author_name"]: r for r in project["reports"]}

        assert by_author["dev"]["author_kind"] == "human"
        assert by_author["dev"]["operated_by_name"] is None
        # The legacy report: no author, and therefore nothing to badge.
        assert by_author[None]["author_kind"] is None
        assert by_author[None]["operated_by_name"] is None


def test_renaming_an_operator_updates_the_badge_on_existing_reports(tmp_path):
    """The read-time join in action: a rename is correct on already-pushed reports.

    This is the property that justified resolving attribution at READ time instead of
    stamping it at write time — no backfill, one source of truth. Contrast with
    `author_name`, which is denormalized and deliberately keeps what it was written with.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        human, agent, agent_key = _seed_human_and_agent(db, base_url)
        _post(base_url, _blob_for("alpha"), token=agent_key)

        _admin_post(
            base_url, "/api/users/rename", {"name": human, "new_name": "renamed-human"}
        )
        cookie = _reader_cookie(db, base_url)

        _, project = _get_json(base_url, "/api/projects/alpha", cookie=cookie)
        entry = project["reports"][0]
        assert entry["operated_by_name"] == "renamed-human"
        # The agent's own recorded name is history and does NOT move.
        assert entry["author_name"] == agent


# --- Unit 5: the `member` role + per-project visibility (KB scoping) ------------------
#
# A member is a read-only ORG INSIDER: it reads every org-visible project with NO grant, plus
# any explicit grants on top. Two properties carry the whole unit and are tested hardest:
#   (1) it can WRITE nothing, anywhere — enforced by absence from _BEARER_ROLES and from
#       _DISCUSSION_ROLE_BY_PRINCIPAL, which is exactly why it is pinned positively here
#       rather than assumed; and
#   (2) a RESTRICTED project is indistinguishable from one that never existed (404), so
#       scoping never leaks the org's project list.


def _set_visibility(base_url, project, visibility):
    """Flip a project's visibility through the real admin route."""
    return _admin_post(
        base_url, "/api/projects/visibility", {"name": project, "visibility": visibility}
    )


def _seed_two_projects(base_url, db):
    """Seed an org-visible project and a restricted one; return a member's session cookie.

    The member gets ZERO grants on purpose — its entire read scope must come from
    visibility, which is the property under test.
    """
    _post(base_url, _blob_for("open-project"), token=_TOKEN)
    _post(base_url, _blob_for("secret-project"), token=_TOKEN)
    _set_visibility(base_url, "open-project", "org")
    _provision_user(db, "kb-member", "member-key", role="member")
    return _login(base_url, "member-key")


def test_a_member_reads_org_visible_projects_with_no_grants(tmp_path):
    """The point of the role: org-visible projects need no per-project grant.

    A viewer with zero grants sees nothing at all — that is default-deny and stays true. A
    member with zero grants sees the org's open projects, which is what makes a company-wide
    knowledge base possible without hand-granting every person every project.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        cookie = _seed_two_projects(base_url, db)

        status, portfolio = _get_json(base_url, "/api/portfolio", cookie=cookie)
        assert status == 200
        assert [p["name"] for p in portfolio["projects"]] == ["open-project"]
        # The org-visible project reads in full...
        assert _get_json(base_url, "/api/projects/open-project", cookie=cookie)[0] == 200
        # ...and the restricted one is invisible.
        assert _get_json(base_url, "/api/projects/secret-project", cookie=cookie)[0] == 404


def test_a_members_grants_stack_on_top_of_org_visibility(tmp_path):
    """An explicit grant lets a member into a RESTRICTED project too — the two sources union.

    Why this matters: visibility is a floor, not a ceiling. Someone can be an org member and
    still be brought into one confidential project without that project becoming org-wide.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        cookie = _seed_two_projects(base_url, db)
        _admin_post(
            base_url, "/api/users/grant",
            {"name": "kb-member", "projects": ["secret-project"]},
        )

        _, portfolio = _get_json(base_url, "/api/portfolio", cookie=cookie)
        assert sorted(p["name"] for p in portfolio["projects"]) == [
            "open-project", "secret-project",
        ]


def test_a_restricted_project_is_404_indistinguishable_from_one_that_never_existed(tmp_path):
    """The existence-hiding matrix: every miss looks identical to a member.

    Why this matters: if a restricted project 403'd while an unknown one 404'd, the response
    code itself would enumerate the org's private project names to anyone with a login. Every
    row below must be byte-identical.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        cookie = _seed_two_projects(base_url, db)
        # Find a report id belonging to the restricted project — a member must not be able to
        # reach a project's content by guessing a report id either.
        conn = open_relay_store(db)
        secret_report_id = history(conn, "secret-project")[0]["id"]
        conn.close()

        misses = [
            ("never-existed project", _get_json(base_url, "/api/projects/no-such", cookie=cookie)),
            ("restricted, ungranted", _get_json(base_url, "/api/projects/secret-project", cookie=cookie)),
            ("report id of a restricted project",
             _get_json(base_url, f"/api/reports/{secret_report_id}", cookie=cookie)),
            # Case and Unicode variants must not sneak past the membership check.
            ("case variant", _get_json(base_url, "/api/projects/Secret-Project", cookie=cookie)),
            ("unicode variant", _get_json(base_url, "/api/projects/secret-pr%C3%B6ject", cookie=cookie)),
        ]
        for label, (status, body) in misses:
            assert status == 404, label
            assert body == {"error": "not found"}, label


def test_a_member_cannot_write_anything_anywhere(tmp_path):
    """POSITIVE write-denial across EVERY write surface — the security core of Unit 5.

    A member's read scope is unioned into `_allowed_projects`, which is ALSO consulted on
    write paths. That union is only safe because a member can never reach a write, so this
    test asserts that directly at each surface rather than trusting the property to hold.
    If a future change adds `member` to _BEARER_ROLES or _DISCUSSION_ROLE_BY_PRINCIPAL, this
    is what fails.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        cookie = _seed_two_projects(base_url, db)

        # 1) The cookie discussion write — 403 on the role allowlist, even for a project the
        # member CAN read. Read access must not imply write access.
        status, _, _ = _post_api_json(
            base_url, "/api/discussions/open-project/items", {"body": "hi"}, cookie=cookie
        )
        assert status == 403

        # 2-4) The Bearer push endpoints: a member's key cannot even authenticate, because
        # member is absent from _BEARER_ROLES. One generic 401 on each.
        for path, payload in (
            ("/ingest", _blob_for("open-project")),
            ("/checklist", json.dumps({"project": "open-project", "checklist": []}).encode()),
            ("/disciplines", json.dumps({"project": "open-project", "disciplines": []}).encode()),
        ):
            status, _ = _post(base_url, payload, token="member-key", path=path)
            assert status == 401, path

        # 5) The Bearer discussion write — same refusal.
        status, _ = _post(
            base_url, json.dumps({"project": "open-project", "body": "hi"}).encode(),
            token="member-key", path="/api/discussions",
        )
        assert status == 401

        # 6) The admin surface is admin-TOKEN gated, so a member's cookie buys nothing there.
        # Attempting the visibility flip with no admin token is a 401.
        status, _ = _admin_post(
            base_url, "/api/projects/visibility",
            {"name": "secret-project", "visibility": "org"}, token="member-key",
        )
        assert status == 401


def test_visibility_flips_take_effect_mid_session_in_both_directions(tmp_path):
    """A flip is immediate, both ways, with no re-login — scope is re-read per request.

    Why this matters: if scope were cached on the session cookie, restricting a project would
    leave anyone already logged in still reading it until their session expired — which would
    make "restricted" a promise the relay could not actually keep.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        cookie = _seed_two_projects(base_url, db)
        assert _get_json(base_url, "/api/projects/secret-project", cookie=cookie)[0] == 404

        # Open it up: the SAME cookie now reads it.
        _set_visibility(base_url, "secret-project", "org")
        assert _get_json(base_url, "/api/projects/secret-project", cookie=cookie)[0] == 200

        # Close it again: the same cookie loses access immediately.
        _set_visibility(base_url, "secret-project", "restricted")
        assert _get_json(base_url, "/api/projects/secret-project", cookie=cookie)[0] == 404


def test_visibility_mutation_validates_against_the_real_project_universe(tmp_path):
    """A typo'd project name is a 404, not a phantom visibility row.

    Why this matters: relay_project_meta rows are upserted with no existence check and are
    never deleted, so accepting an arbitrary name here would write a visibility row for a
    project that does not exist. That is the phantom the canonical-universe check prevents.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _seed_two_projects(base_url, db)

        status, body = _set_visibility(base_url, "typo-project", "org")
        assert status == 404 and "typo-project" in body["error"]
        # Nothing was written, so nothing can later surface it.
        conn = open_relay_store(db)
        assert org_visible_projects(conn) == {"open-project"}
        conn.close()

        # And an invalid visibility value is refused outright.
        status, body = _set_visibility(base_url, "open-project", "public")
        assert status == 400 and "visibility" in body["error"]


# --- S2.2: project lifecycle (active vs. past) -----------------------------------------
#
# A project is marked past by an ADMIN, never by a producer and never by inference. These
# pin the route's guards and the end-to-end property the flag exists for: a finished project
# stops being read as if it still had deadlines.


def _set_lifecycle(base_url, project, lifecycle):
    """Mark a project past/active through the real admin route."""
    return _admin_post(
        base_url, "/api/projects/lifecycle", {"name": project, "lifecycle": lifecycle}
    )


def test_marking_a_project_past_removes_it_from_every_deadline_view(tmp_path):
    """One admin command, and the project stops reading as overdue anywhere — reversibly.

    Why this matters: this is the acceptance check for the whole increment, end to end
    through the real routes. Two projects each carry an overdue deadline. After ONE
    `lifecycle past` call — with no producer config change and no re-push — the wrapped
    project reports lifecycle "past" with zeroed urgency on Home, its page has no NEXT DUE,
    and it is gone from Scheduling's buckets AND its summary counts. The live project beside
    it is untouched throughout, which is what proves the flag is per-project and not a
    global mute. Flipping back to active restores everything.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, _db):
        overdue = [{"text": "Ship it", "done": False, "due_date": "2020-01-01"}]
        _push_checklist(base_url, "live-project", overdue)
        _push_checklist(base_url, "wrapped-project", overdue)

        # Before: both are active, both overdue, both on the timeline.
        _, portfolio = _get_json(base_url, "/api/portfolio")
        by_name = {p["name"]: p for p in portfolio["projects"]}
        assert by_name["wrapped-project"]["lifecycle"] == "active"
        assert by_name["wrapped-project"]["next_due"]["state"] == "overdue"
        _, sched = _get_json(base_url, "/api/scheduling")
        assert sched["summary"]["overdue"] == 2

        status, body = _set_lifecycle(base_url, "wrapped-project", "past")
        assert status == 200
        assert body == {"name": "wrapped-project", "lifecycle": "past"}

        # Home: past, with every urgency fact zeroed. The live project is unchanged.
        _, portfolio = _get_json(base_url, "/api/portfolio")
        by_name = {p["name"]: p for p in portfolio["projects"]}
        wrapped = by_name["wrapped-project"]
        assert wrapped["lifecycle"] == "past"
        assert wrapped["next_due"] is None
        assert wrapped["at_risk"] == 0 and wrapped["slipping"] == 0
        assert by_name["live-project"]["lifecycle"] == "active"
        assert by_name["live-project"]["next_due"]["state"] == "overdue"

        # Project page: badged past, no NEXT DUE.
        _, detail = _get_json(base_url, "/api/projects/wrapped-project")
        assert detail["lifecycle"] == "past"
        assert detail["stats"]["next_due"] is None

        # Scheduling: gone from the buckets and from the summary count.
        _, sched = _get_json(base_url, "/api/scheduling")
        sources = {r["source"]["name"] for b in sched["buckets"].values() for r in b}
        assert sources == {"live-project"}
        assert sched["summary"]["overdue"] == 1

        # Reversible: flipping back restores every derivation.
        assert _set_lifecycle(base_url, "wrapped-project", "active")[0] == 200
        _, sched = _get_json(base_url, "/api/scheduling")
        assert sched["summary"]["overdue"] == 2


def test_lifecycle_mutation_validates_its_value_and_the_project_universe(tmp_path):
    """A bad value is a 400; a typo'd project is a 404 that writes nothing.

    Why this matters: the same phantom guard the visibility route has. relay_project_meta
    rows are upserted with no existence check and never deleted, so accepting an arbitrary
    name here would write a lifecycle row for a project that does not exist.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _push_checklist(base_url, "real-project", [{"text": "A", "done": False}])

        status, body = _set_lifecycle(base_url, "real-project", "archived")
        assert status == 400 and "lifecycle" in body["error"]

        status, body = _set_lifecycle(base_url, "typo-project", "past")
        assert status == 404 and "typo-project" in body["error"]

        # Nothing was written for the phantom, and the real project is still active.
        conn = open_relay_store(db)
        assert get_project_lifecycle(conn, "typo-project") == "active"
        assert get_project_lifecycle(conn, "real-project") == "active"
        conn.close()


def test_lifecycle_mutation_is_admin_token_gated(tmp_path):
    """Neither an anonymous caller nor the ingest token may mark a project past.

    Why this matters: lifecycle is a CURATION act on the org's view of its own work, so it
    belongs to the separately-held admin credential — the same boundary every other
    provisioning route sits behind. A producer's ingest token must never reach it, which is
    what keeps "no producer path for the flag" true at the wire, not just by convention.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _push_checklist(base_url, "real-project", [{"text": "A", "done": False}])
        payload = {"name": "real-project", "lifecycle": "past"}

        assert _admin_post(base_url, "/api/projects/lifecycle", payload, token=None)[0] == 401
        assert _admin_post(base_url, "/api/projects/lifecycle", payload, token=_TOKEN)[0] == 401

        conn = open_relay_store(db)
        assert get_project_lifecycle(conn, "real-project") == "active"
        conn.close()


def test_lifecycle_changes_write_an_audit_row(tmp_path):
    """Marking a project past appends an admin-audit row naming the project and the value.

    Why this matters: lifecycle changes what the whole org sees on the dashboard, so "who
    decided this project was finished" has to be answerable later.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _push_checklist(base_url, "real-project", [{"text": "A", "done": False}])
        _set_lifecycle(base_url, "real-project", "past")

        conn = open_relay_store(db)
        rows = conn.execute(
            "SELECT actor, action, target_user, role FROM relay_admin_audit ORDER BY id"
        ).fetchall()
        conn.close()
        # The generic `role` column carries the action's value, exactly as the visibility
        # route uses it — the audit table is deliberately one shape for every admin act.
        assert [(r["actor"], r["action"], r["target_user"], r["role"]) for r in rows] == [
            ("admin-token", "set_project_lifecycle", "real-project", "past")
        ]


def test_viewer_and_supervisor_scope_is_unchanged_by_visibility(tmp_path):
    """Org-visibility is a MEMBER concept: a viewer/supervisor still sees only its grants.

    Why this matters: viewers and supervisors are SCOPED OUTSIDE participants (a family
    member, a mentor). Making a project org-visible opens it to the org's own members, and
    must not silently widen what an external supervisor can see.
    """
    with _running_relay(tmp_path, auth=_admin_auth()) as (base_url, db):
        _seed_two_projects(base_url, db)
        for name, key, role in (
            ("outside-viewer", "viewer-key", "viewer"),
            ("outside-super", "super-key", "supervisor"),
        ):
            _provision_user(db, name, key, role=role)
            cookie = _login(base_url, key)
            _, portfolio = _get_json(base_url, "/api/portfolio", cookie=cookie)
            # Zero grants ⇒ sees nothing, even though "open-project" is org-visible.
            assert portfolio["projects"] == [], role


def test_the_public_showcase_ignores_visibility(tmp_path):
    """The showcase serves its curated allowlist only — visibility neither adds nor removes.

    Why this matters: the showcase is a public, no-login surface whose contents are an
    explicit operator choice. If org-visibility leaked into it, flipping a project to 'org'
    for internal readers would publish it to the internet.
    """
    with _running_relay(
        tmp_path, auth=_admin_auth(),
        showcase=ShowcaseConfig(enabled=True, projects=(("secret-project", ""),)),
    ) as (base_url, db):
        _seed_two_projects(base_url, db)

        status, showcase = _get_json(base_url, "/api/showcase")
        assert status == 200
        # The curated (restricted!) project is shown; the org-visible one is NOT.
        assert [c["name"] for c in showcase["projects"]] == ["secret-project"]


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
        _provision_user(db, "Teammate B", "admin-key", role="admin")
        _provision_user(db, "Mum", "mum-key", role="viewer", projects=["orion"])

        _, admin_me = _get_json(base_url, "/api/me", cookie=_login(base_url, "admin-key"))
        assert admin_me["identity"] == {"name": "Teammate B", "role": "admin"}
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
        _provision_user(db, "Teammate B", "admin-key", role="admin")

        status, body, headers = _post_api_json(base_url, "/api/login", {"key": "admin-key"})
        assert status == 200
        assert body == {"ok": True, "user": {"name": "Teammate B", "role": "admin"}}
        assert _SESSION_COOKIE_NAME in (headers.get("Set-Cookie") or "")

        bad, bad_body, _ = _post_api_json(base_url, "/api/login", {"key": "wrong"})
        assert bad == 401 and bad_body == {"ok": False}


def test_api_login_minted_cookie_authenticates_me(tmp_path):
    """The cookie from /api/login authenticates a subsequent /api/me as that user."""
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        _provision_user(db, "Teammate B", "admin-key", role="admin")
        _, _body, headers = _post_api_json(base_url, "/api/login", {"key": "admin-key"})
        jar = SimpleCookie()
        jar.load(headers["Set-Cookie"])
        cookie = jar[_SESSION_COOKIE_NAME].value

        _, me = _get_json(base_url, "/api/me", cookie=cookie)
        assert me["authenticated"] is True
        assert me["identity"] == {"name": "Teammate B", "role": "admin"}


def test_api_login_rejects_foreign_origin(tmp_path):
    """A cross-origin POST /api/login is refused 403 (the CSRF guard)."""
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        _provision_user(db, "Teammate B", "admin-key", role="admin")
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


# --- AU1-R F2: the operational floor (/healthz, build provenance, request logging) ---
#
# KI-44/KI-46 recorded that the relay had no health check, no logging, and no answer to
# "what code is running?". These tests pin the three properties that make the floor real:
# /healthz answers without auth, is NOT swallowed by the SPA's catch-all index.html
# fallback, and leaks nothing about the projects on the relay.


def test_healthz_answers_unauthenticated_on_a_gated_relay(tmp_path):
    """GET /healthz returns 200 {"status": "ok", ...} with no credential, on a GATED relay.

    Why this matters: a health probe is a platform process with no session and no token. If
    /healthz sat behind the auth spine it would report "unhealthy" for a perfectly healthy
    relay, which is worse than having no check at all. The gated relay (view_token set) is
    the case that matters — an ungated loopback relay would pass trivially.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, _db):
        status, body = _get_json(base_url, "/healthz")
        assert status == 200
        assert body["status"] == "ok"


def test_healthz_is_not_swallowed_by_the_spa_fallback(tmp_path):
    """With --web-dir, /healthz still answers JSON rather than the SPA's index.html.

    Why this matters: under --web-dir ANY unmatched GET path falls through to index.html
    with a 200 (that is how client-side routing works). A health check pointed at a route
    that quietly resolved to the SPA shell would report 200 forever — including while the
    API and the store were completely broken. This is the test that keeps the route ordered
    ahead of the static fallback.
    """
    web = _build_web_dir(tmp_path)
    with _running_relay(tmp_path, web_dir=web) as (base_url, _db):
        status, body = _get_json(base_url, "/healthz")
        assert status == 200
        assert body["status"] == "ok"  # parsed as JSON, so it was not the HTML shell


def test_healthz_reveals_no_project_facts(tmp_path):
    """The /healthz body names no project and carries only status + version.

    Why this matters: /healthz is the one unauthenticated, unscoped read on the relay, so it
    is exactly where existence-hiding (a viewer must not learn a project exists) would leak
    if the payload ever grew a count or a name for "operational convenience". A report is
    ingested first so the assertion is made against a relay that actually HAS a project to
    leak.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, _db):
        _post(base_url, _blob_for("secret-skunkworks"))
        status, text = _get(base_url, "/healthz")
        assert status == 200
        assert "secret-skunkworks" not in text
        assert set(json.loads(text)) == {"status", "version"}


def test_healthz_version_reports_the_build_stamp(tmp_path, monkeypatch):
    """`version` echoes ORION_BUILD_SHA, and says "unknown" when it is unset.

    Why this matters: this is the "what code is running?" answer. Both halves are asserted
    because the failure modes differ in kind — a stale or wrong SHA would make an operator
    conclude a fix is live when it is not, whereas "unknown" is a truthful admission that
    the deploy did not pass the build arg. The value is read per request (not cached at
    import), which is what makes it patchable here.
    """
    monkeypatch.setenv("ORION_BUILD_SHA", "abc1234-dirty")
    with _running_relay(tmp_path) as (base_url, _db):
        assert _get_json(base_url, "/healthz")[1]["version"] == "abc1234-dirty"

    monkeypatch.delenv("ORION_BUILD_SHA")
    with _running_relay(tmp_path) as (base_url, _db):
        assert _get_json(base_url, "/healthz")[1]["version"] == "unknown"


def test_a_failed_request_leaves_a_log_line(tmp_path, capfd):
    """A 404 emits a stderr log line naming the method, path, and status.

    Why this matters: log_message used to be a no-op, so a relay returning 404s or 500s
    produced ZERO output — an operator debugging "the dashboard is broken" had nothing to
    grep. This is the acceptance bar "a failed request leaves a searchable log line".
    """
    with _running_relay(tmp_path) as (base_url, _db):
        capfd.readouterr()  # drain startup noise
        assert _get_json(base_url, "/no-such-route")[0] == 404
        err = capfd.readouterr().err
        assert "GET /no-such-route" in err
        assert "404" in err
        assert "[relay]" in err  # the prefix operator habits and log filters rely on


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


# --- POST /api/discussions/<project>/items: the supervisor-interaction loop (E2 Inc 5) ---
# The SPA's cookie-authed discussion write — the one user-authored write surface. Identity
# is fully server-derived (a viewer cannot post; a client-supplied author/role is ignored)
# and the project — not a report — is the thread anchor. The read folds into
# GET /api/projects/<name>.


def test_api_discussion_supervisor_post_stores_and_returns_created(tmp_path):
    """A supervisor's same-origin post → 201 as role 'supervisor', attributed to the session.

    Why this matters: the happy path of the loop's human-write surface. The supervisor is a
    scoped principal; the returned shape matches the read path (_discussion_item) so the SPA
    appends it directly. Crucially, a client-supplied author/role/author_id is IGNORED in
    favour of the server-derived identity — a poster cannot forge who they are.
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        _ingest_one(base_url)  # creates project "demo"
        uid = _provision_user(db, "Supervisor A", "supervisor-a-key", role="supervisor", projects=["demo"])
        cookie = _login(base_url, "supervisor-a-key")

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
        assert body["author_name"] == "Supervisor A"          # session identity, not the spoofed name
        assert "author_id" not in body               # internal id is not on the wire
        assert isinstance(body["id"], int) and body["created_at"]

        conn = open_relay_store(db)
        stored = discussion_items_for_project(conn, "demo")
        assert len(stored) == 1
        assert stored[0]["author_id"] == uid         # real principal id, not the spoofed 999
        assert stored[0]["author_name"] == "Supervisor A" and stored[0]["role"] == "supervisor"


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

    Why this matters: a discussion entry is attributable by definition, so there is no
    anonymous/open-loopback path — no session means 401, full stop.
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
        _provision_user(db, "Supervisor A", "supervisor-a-key", role="supervisor", projects=["demo"])
        cookie = _login(base_url, "supervisor-a-key")
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
    lives). This is the end-to-end write→read the SPA depends on, and it
    confirms the real role rides the wire (gap 7 closed for this surface).
    """
    with _running_relay(tmp_path, view_token=_VIEW) as (base_url, db):
        _ingest_one(base_url)
        _provision_user(db, "Supervisor A", "supervisor-a-key", role="supervisor", projects=["demo"])
        _provision_user(db, "Owner", "owner-key", role="admin")  # the developer/owner
        sup_cookie = _login(base_url, "supervisor-a-key")
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
            ("Supervisor A", "supervisor", "How's auth?"),
            ("Owner", "developer", "Landed."),
        ]
        assert all("author_id" not in d for d in thread)  # internal id stays off the wire


# --- GET/POST /api/discussions: the developer's Bearer machine loop (E2 Inc 5, Unit 3a) ---
# The CLI's terminal half: a Bearer pull (GET /api/discussions) + a Bearer reply that
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
            db, "demo", [("Supervisor A", "supervisor", "How's auth?"), ("Teammate B", "developer", "Landed.")]
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
            db, "demo", [("Supervisor A", "supervisor", "seen"), ("Supervisor A", "supervisor", "new")]
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
            base_url, json.dumps({"project": "demo", "author": "Teammate B", "body": "Landed."}).encode(),
            token=_TOKEN, path="/api/discussions",
        )
        assert status == 201 and isinstance(json.loads(raw)["id"], int)

        conn = open_relay_store(db)
        stored = discussion_items_for_project(conn, "demo")
        assert len(stored) == 1
        assert stored[0]["role"] == "developer"
        assert stored[0]["author_id"] is None and stored[0]["author_name"] == "Teammate B"
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


def test_api_discussion_post_identified_producer_is_attributed(tmp_path):
    """An identified contributor's reply is stamped with its REAL id + name; --as is ignored.

    Why this matters: C3 Inc 2 attribution. When the key resolves to a named producer, the
    entry carries that principal's server-derived author_id and name — unforgeable — and the
    body's `author` (the CLI's --as) is discarded, because a client can never assert its own
    identity. The 201 echoes the stored name so the CLI can report who it actually posted as.
    """
    with _running_relay(tmp_path) as (base_url, db):
        uid = _provision_user(
            db, "Teammate B", "contrib-key", role="contributor", projects=["demo"]
        )
        _ingest_one(base_url)  # project "demo" (legacy setup)
        status, raw = _post(
            base_url,
            json.dumps({"project": "demo", "author": "not-me", "body": "Landed."}).encode(),
            token="contrib-key",
            path="/api/discussions",
        )
        assert status == 201
        assert json.loads(raw)["author"] == "Teammate B"  # stored name echoed, not "not-me"

        conn = open_relay_store(db)
        stored = discussion_items_for_project(conn, "demo")[0]
        assert stored["role"] == "developer"
        assert stored["author_id"] == uid  # real, server-derived id
        assert stored["author_name"] == "Teammate B"  # principal's name, --as ignored


def test_api_discussion_post_identified_out_of_scope_is_404(tmp_path):
    """A contributor replying to an ungranted project 404s, indistinguishable from missing.

    Why this matters: the discussion write is scope-checked like every other producer write —
    an out-of-scope thread is refused before it is even known to exist.
    """
    with _running_relay(tmp_path) as (base_url, db):
        _provision_user(db, "mac", "contrib-key", role="contributor", projects=["demo"])
        _ingest_project(base_url, "secret-proj")  # exists, but not granted to this contributor
        status, _ = _post(
            base_url,
            json.dumps({"project": "secret-proj", "body": "peek"}).encode(),
            token="contrib-key",
            path="/api/discussions",
        )
        assert status == 404
        conn = open_relay_store(db)
        assert discussion_items_for_project(conn, "secret-proj") == []


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


# --- POST /disciplines + the project page's "Working agreements" (Unit 5) --------
# /disciplines upserts a project's observed principles WITHOUT a report (like
# /checklist); a pushed set then surfaces on GET /api/projects/:name as the
# "disciplines" field the project page's "Working agreements" section renders. The
# standalone GET /api/disciplines cross-project view retired in Unit 5.


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
        assert project_disciplines(conn, "demo")["cards"] == [_disc("Local-first", scope="global")]
        assert list_projects(conn) == []  # no report row was created


def test_disciplines_push_dual_writes_for_identified_but_not_legacy(tmp_path):
    """An identified push writes BOTH the aggregate and the producer's own row; legacy → aggregate only.

    Why this matters: the C3 Inc 2.5 storage-now-display-later promise, end to end. An identified
    contributor's disciplines are dual-written (aggregate + per-producer) so provenance is captured
    the moment it exists (it can't be backfilled); a legacy anonymous push has no identity, so it
    lands in the aggregate only — never a per-producer row. There is no display surface yet, so we
    inspect the store directly after each push.
    """
    with _running_relay(tmp_path) as (base_url, db):
        _provision_user(db, "Teammate B", "b-key", role="contributor", projects=["demo"])

        # Identified push (the contributor's own key) → aggregate AND that producer's row.
        assert _push_disciplines(base_url, "demo", [_disc("Local-first")], token="b-key")[0] == 200
        conn = open_relay_store(db)
        assert project_disciplines(conn, "demo")["cards"] == [_disc("Local-first")]  # aggregate
        producer = producer_disciplines_for(conn, "demo")
        assert [p["author_name"] for p in producer] == ["Teammate B"]
        assert producer[0]["disciplines"] == [_disc("Local-first")]
        conn.close()

        # Legacy push (shared ingest token) on another project → aggregate only, no producer row.
        assert _push_disciplines(base_url, "legacy-proj", [_disc("Observe")], token=_TOKEN)[0] == 200
        conn = open_relay_store(db)
        assert project_disciplines(conn, "legacy-proj")["cards"] == [_disc("Observe")]  # aggregate written
        assert producer_disciplines_for(conn, "legacy-proj") == []  # no per-producer row


def test_disciplines_push_wrong_token_is_401(tmp_path):
    """A bad Bearer token is rejected 401 and stores nothing.

    Why this matters: /disciplines is a machine push on the ingest credential — a wrong
    token must not write.
    """
    with _running_relay(tmp_path) as (base_url, db):
        status, _ = _push_disciplines(base_url, "demo", [_disc("X")], token="wrong")
        assert status == 401
        conn = open_relay_store(db)
        assert project_disciplines(conn, "demo") is None


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
        assert project_disciplines(conn, "demo") is None


def test_pushed_disciplines_surface_on_the_project_page(tmp_path):
    """A pushed discipline set appears on GET /api/projects/:name as the "disciplines" field.

    Why this matters: this is the Unit 5 end-to-end read the "Working agreements" section
    renders. Every card shows regardless of the model's scope (global + project alike), the
    scope enum is dropped from the wire card, and the freshness stamp rides along. The
    project must also have a report to exist (a disciplines-only project 404s — the
    standalone cross-project view that once showed those retired with this slice).
    """
    with _running_relay(tmp_path) as (base_url, db):
        _provision_user(db, "root", "admin-key", role="admin")  # reads via the cookie SPA API
        assert _post(base_url, _blob_for("demo"), token=_TOKEN)[0] == 201  # project now exists
        _push_disciplines(
            base_url,
            "demo",
            [_disc("Local-first", scope="global"), _disc("Observe", scope="project")],
        )

        cookie = _login(base_url, "admin-key")
        code, body = _get(base_url, "/api/projects/demo", cookie=cookie)
        assert code == 200
        disciplines = json.loads(body)["disciplines"]
        # Both cards present, scope dropped; the section renders them all regardless of scope.
        assert disciplines["cards"] == [
            {"title": "Local-first", "why": "why", "source": "CLAUDE.md"},
            {"title": "Observe", "why": "why", "source": "CLAUDE.md"},
        ]
        # The freshness stamp is a non-empty ISO timestamp (the relay's receive time).
        assert disciplines["updated_at"]

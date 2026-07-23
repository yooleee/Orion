# =============================================================================
# tests/test_relay_delivery.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the relay push's request shape (verbatim body, Bearer
#                  header, content type), success handling, and failure translation
#                  — WITHOUT real network calls.
# Role in project: The relay push is C1's outbound seam. It must POST the exact
#                  serialized blob with the right auth header and turn every failure
#                  into DeliveryError so the CLI can report a non-fatal relay error.
# Test approach: urllib.request.urlopen is monkeypatched to capture the request and
#                return a canned response (or raise) — mirrors test_delivery.py.
# =============================================================================

import io
import json
import urllib.error

import pytest

from orion.delivery import DeliveryError
from orion.delivery.relay import (
    create_user,
    list_users,
    post_discussion,
    pull_discussions,
    push,
    push_checklist,
    revoke_user,
)


class _FakeResponse:
    """Context-manager stand-in for urlopen's return value.

    Why:
        urlopen is used as `with urlopen(...) as response:`; the fake must support
        the context-manager protocol and expose `.status` like the real object.
    """

    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# A stand-in for serialize_blob's output. The push must treat this as opaque bytes
# and send it unchanged — so the test uses an exact string and asserts byte equality
# rather than re-parsing, which is the whole point of "verbatim".
_BLOB_JSON = '{"body":"Shipped the seam.","project":"demo"}'


def test_successful_push_sends_blob_verbatim_with_bearer(monkeypatch):
    """A 201 counts as success; the body is the blob string as-is with Bearer auth.

    Why this matters: the relay ingest returns 201 Created, and the dashboard
    renders from the structured blob — so the body must be the exact serialize_blob
    output (no re-serialization that could reorder keys) and the request must carry
    the Authorization: Bearer header the ingest authenticates against.
    """
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        # Decode back to a string and compare to the input verbatim — this proves
        # the body was sent unchanged, not re-encoded through json.dumps.
        captured["body"] = request.data.decode("utf-8")
        captured["content_type"] = request.headers.get("Content-type")
        # urllib title-cases header keys, so "Authorization" is stored as such.
        captured["authorization"] = request.headers.get("Authorization")
        captured["user_agent"] = request.headers.get("User-agent")
        return _FakeResponse(201)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    push(_BLOB_JSON, "https://relay.test/ingest", "s3cr3t-token")

    assert captured["url"] == "https://relay.test/ingest"
    # Verbatim: the exact contract bytes, untouched.
    assert captured["body"] == _BLOB_JSON
    assert captured["content_type"] == "application/json"
    # The token is sent as a Bearer credential, not embedded in the URL.
    assert captured["authorization"] == "Bearer s3cr3t-token"
    # A descriptive, non-default UA (consistency with the chat senders).
    assert captured["user_agent"] is not None
    assert "Orion" in captured["user_agent"]


# --- KI-35: the tri-state due_soon_days on the checklist carrier ---------------------
# The transport is where "absent" vs "explicit null" is actually decided, so the three
# states are pinned here against the real request bytes, not a mock's kwargs.


def _capture_checklist_payload(monkeypatch, **kwargs):
    """Run push_checklist against a fake urlopen and return the decoded JSON payload."""
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = request.data.decode("utf-8")
        return _FakeResponse(200)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    push_checklist(
        "https://relay.test/ingest", "demo", [{"text": "Wire it", "done": False}],
        "s3cr3t-token", **kwargs,
    )
    return json.loads(captured["body"])


def test_push_checklist_omits_due_soon_days_when_unconfigured(monkeypatch):
    """With no horizon configured, the key is absent from the payload entirely.

    Why this matters: absence is the relay's "leave the stored value alone" signal
    (KI-35). A producer that does not configure the horizon must send NO key, so it can
    never disturb a value another producer set — this is the wire half of the fix.
    """
    payload = _capture_checklist_payload(monkeypatch)
    assert "due_soon_days" not in payload


def test_push_checklist_sends_explicit_null_when_clearing(monkeypatch):
    """`clear_due_soon_days=True` puts a literal JSON null on the wire.

    Why this matters: clearing is now an explicit act, and its only carrier is the
    null. Asserting on the parsed payload (key PRESENT, value None) is what separates a
    clear from an omission — the two are indistinguishable if you only check `.get()`.
    """
    payload = _capture_checklist_payload(monkeypatch, clear_due_soon_days=True)
    assert "due_soon_days" in payload
    assert payload["due_soon_days"] is None


def test_push_checklist_refuses_to_clear_and_set_at_once(monkeypatch):
    """Passing both a horizon and the clear flag is a caller bug, raised not silently resolved.

    Why this matters: the two arguments express contradictory intent, and picking a
    winner silently would make the wire depend on an arbitrary precedence rule. The CLI
    never lets them combine, so this guards the library surface against a future caller.
    """
    with pytest.raises(ValueError):
        _capture_checklist_payload(
            monkeypatch, due_soon_days=14, clear_due_soon_days=True
        )


# --- The tri-state About on the checklist carrier (mirrors due_soon_days, KI-35 rule) --


def test_push_checklist_omits_about_when_unconfigured(monkeypatch):
    """With no About passed, the `about` key is absent from the payload entirely.

    Why this matters: absence is the relay's "leave the stored About alone" signal, the
    same rule as due_soon_days — a producer without an about_file must send NO key so it
    can never clobber an About another producer set.
    """
    payload = _capture_checklist_payload(monkeypatch)
    assert "about" not in payload


def test_push_checklist_sends_about_when_set(monkeypatch):
    """A non-None `about` is sent as a plain string under the `about` key.

    Why this matters: this is the set path of the tri-state — the value the relay stores
    as the project's About line.
    """
    payload = _capture_checklist_payload(monkeypatch, about="A progress-update tool.")
    assert payload["about"] == "A progress-update tool."


def test_push_checklist_sends_explicit_null_when_clearing_about(monkeypatch):
    """`clear_about=True` puts a literal JSON null on the wire.

    Why this matters: clearing About is an explicit act whose only carrier is the null
    (key PRESENT, value None) — indistinguishable from an omission if you only check
    `.get()`, which is exactly the KI-35 distinction.
    """
    payload = _capture_checklist_payload(monkeypatch, clear_about=True)
    assert "about" in payload
    assert payload["about"] is None


def test_push_checklist_refuses_to_clear_and_set_about_at_once(monkeypatch):
    """Passing both an About value and clear_about is a caller bug, raised not resolved.

    Why this matters: the two express contradictory intent; picking a winner silently
    would make the wire depend on an arbitrary precedence. The CLI never combines them,
    so this guards the library surface.
    """
    with pytest.raises(ValueError):
        _capture_checklist_payload(monkeypatch, about="X", clear_about=True)


def test_non_2xx_status_becomes_delivery_error(monkeypatch):
    """A non-2xx response status is translated into DeliveryError.

    Why this matters: the caller (cli._relay_push) catches DeliveryError to apply
    the fail-soft policy. A surprising status that came back as a normal response
    (not an HTTPError) must still be treated as a failure, not silently accepted.
    """
    def fake_urlopen(request, timeout=None):
        return _FakeResponse(302)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(DeliveryError):
        push(_BLOB_JSON, "https://relay.test/ingest", "tok")


def test_http_error_becomes_delivery_error(monkeypatch):
    """A 4xx/5xx (HTTPError) — e.g. 401 bad token — is translated into DeliveryError.

    Why this matters: a token mismatch (401) or malformed payload (400) arrives as
    an HTTPError; it must surface as a reported, non-fatal relay failure rather than
    crashing the run with a raw urllib exception.
    """
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 401, "Unauthorized", hdrs=None, fp=None
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(DeliveryError):
        push(_BLOB_JSON, "https://relay.test/ingest", "wrong-token")


def test_connection_error_becomes_delivery_error(monkeypatch):
    """A network failure (URLError) — e.g. relay not running — becomes DeliveryError.

    Why this matters: the relay server may simply be down; an unreachable endpoint
    must be the same uniform, non-fatal DeliveryError as an HTTP error so a stopped
    relay never blocks a delivered report.
    """
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(DeliveryError):
        push(_BLOB_JSON, "https://relay.test/ingest", "tok")


# --- Shared test double for response-parsing clients (pull_discussions, admin API) ---


class _FakeReadResponse:
    """Context-manager stand-in for urlopen whose .read() returns canned bytes.

    Why:
        The response-parsing clients (pull_discussions, the admin API) read and decode
        the body (unlike push, which only checks .status), so the fake must support
        `with urlopen(...) as r: r.read()` and hand back the bytes the test wants parsed.
    """

    def __init__(self, body_bytes):
        self._body = body_bytes

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


# --- E2 Inc 5: pull_discussions + post_discussion (the developer's CLI loop) ------


def test_pull_discussions_derives_url_sends_query_and_bearer(monkeypatch):
    """pull_discussions derives /api/discussions, carries project+since_id, parses the body.

    Why this matters: the developer's read half — one configured (ingest) URL, the read
    endpoint derived from it, both params escaped, the Bearer token sent, and the JSON
    returned verbatim for the CLI to render + watermark.
    """
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["authorization"] = request.headers.get("Authorization")
        body = json.dumps(
            {"discussions": [{"id": 4, "role": "supervisor", "author_name": "Supervisor A",
                              "body": "How's auth?"}], "latest_id": 4}
        ).encode("utf-8")
        return _FakeReadResponse(body)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = pull_discussions("https://relay.test/ingest", "s3cr3t", "my proj", 2)

    assert captured["url"].startswith("https://relay.test/api/discussions?")
    assert "project=my+proj" in captured["url"] and "since_id=2" in captured["url"]
    assert captured["authorization"] == "Bearer s3cr3t"
    assert result["latest_id"] == 4
    assert result["discussions"][0]["role"] == "supervisor"


def test_post_discussion_sends_payload_and_returns_id(monkeypatch):
    """post_discussion POSTs {project, body, author} with Bearer and returns the new id.

    Why this matters: the developer's write half. The body must carry exactly the three
    fields (role is NOT sent — the relay fixes it), and the parsed {id} is handed back so
    the CLI can echo it.
    """
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.method
        captured["authorization"] = request.headers.get("Authorization")
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _FakeReadResponse(json.dumps({"id": 11}).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = post_discussion("https://relay.test/ingest", "tok", "demo", "Landed.", "Teammate B")

    assert captured["url"] == "https://relay.test/api/discussions"
    assert captured["method"] == "POST"
    assert captured["authorization"] == "Bearer tok"
    # Exactly the three fields; no client-set role/author_id.
    assert captured["payload"] == {"project": "demo", "body": "Landed.", "author": "Teammate B"}
    assert result == {"id": 11}


def test_post_discussion_http_error_becomes_delivery_error(monkeypatch):
    """A 4xx on the reply (e.g. 404 unknown project / 401 bad token) becomes DeliveryError."""
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", hdrs=None, fp=None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(DeliveryError):
        post_discussion("https://relay.test/ingest", "tok", "ghost", "x", "")


# --- C3 admin API client: create_user / list_users / revoke_user -----------------
#
# The provisioning client backing `relay-user`. These mirror the push/pull tests:
# urlopen is monkeypatched so we assert the derived admin URL, the SEPARATE admin
# Bearer token, the request method/body, and that a relay error is surfaced (with its
# JSON {"error": ...} detail lifted) as DeliveryError — all without a real relay.


def test_create_user_posts_to_derived_admin_url_with_admin_bearer(monkeypatch):
    """create_user derives /api/users from the ingest URL and POSTs name/role/projects.

    Why this matters: the caller passes the one configured (ingest) URL; the admin client
    must derive the /api/users endpoint from it, send the SEPARATE admin token as Bearer,
    POST the fields as JSON, and return the parsed response (which carries the one-time key).
    """
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.method
        captured["authorization"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.data.decode("utf-8"))
        body = json.dumps(
            {"id": 1, "name": "alice", "role": "viewer", "projects": ["demo"], "key": "RAWKEY"}
        ).encode("utf-8")
        return _FakeReadResponse(body)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = create_user(
        "https://relay.test/ingest", "admin-secret", "alice", "viewer", ["demo"]
    )

    # The admin URL is derived from the ingest URL (path replaced, host kept).
    assert captured["url"] == "https://relay.test/api/users"
    assert captured["method"] == "POST"
    assert captured["authorization"] == "Bearer admin-secret"  # the ADMIN token
    assert captured["body"] == {"name": "alice", "role": "viewer", "projects": ["demo"]}
    # The parsed response (including the one-time key) is returned to the caller.
    assert result["key"] == "RAWKEY"


def test_create_user_http_error_lifts_relay_error_message(monkeypatch):
    """A relay 4xx with a JSON {"error": ...} body surfaces that detail in the DeliveryError.

    Why this matters: a CLI failure must be actionable — a duplicate name (409) should read
    as "a user named 'alice' already exists", not a bare "HTTP 409". We confirm the lifted
    message includes both the status and the relay's reason.
    """
    def fake_urlopen(request, timeout=None):
        body = io.BytesIO(b'{"error": "a user named \'alice\' already exists"}')
        raise urllib.error.HTTPError(
            request.full_url, 409, "Conflict", hdrs=None, fp=body
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(DeliveryError) as exc:
        create_user("https://relay.test/ingest", "admin-secret", "alice", "viewer", [])
    msg = str(exc.value)
    assert "409" in msg and "already exists" in msg


def test_list_users_gets_derived_admin_url_with_admin_bearer(monkeypatch):
    """list_users GETs the derived /api/users with the admin Bearer and returns the roster.

    Why this matters: the read half of the admin client must hit the same derived endpoint
    with the admin token and a GET (no body), and parse the {"users": [...]} response.
    """
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.method
        captured["authorization"] = request.headers.get("Authorization")
        captured["has_body"] = request.data is not None
        body = json.dumps({"users": [{"name": "alice", "role": "viewer"}]}).encode("utf-8")
        return _FakeReadResponse(body)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = list_users("https://relay.test/ingest", "admin-secret")

    assert captured["url"] == "https://relay.test/api/users"
    assert captured["method"] == "GET"
    assert captured["has_body"] is False  # a GET sends no body
    assert captured["authorization"] == "Bearer admin-secret"
    assert result == {"users": [{"name": "alice", "role": "viewer"}]}


def test_revoke_user_posts_to_derived_revoke_url(monkeypatch):
    """revoke_user POSTs the name to the derived /api/users/revoke with the admin Bearer.

    Why this matters: revoke is a privileged state change on its own endpoint; the client
    must target /api/users/revoke (not /api/users), send the name, and use the admin token.
    """
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.method
        captured["authorization"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeReadResponse(json.dumps({"name": "alice", "revoked": True}).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = revoke_user("https://relay.test/ingest", "admin-secret", "alice")

    assert captured["url"] == "https://relay.test/api/users/revoke"
    assert captured["method"] == "POST"
    assert captured["authorization"] == "Bearer admin-secret"
    assert captured["body"] == {"name": "alice"}
    assert result == {"name": "alice", "revoked": True}


def test_admin_connection_error_becomes_delivery_error(monkeypatch):
    """A network failure on an admin call (relay down) becomes DeliveryError.

    Why this matters: an unreachable relay during provisioning must surface as a clean,
    catchable DeliveryError the CLI reports and exits 1 on — not a raw traceback.
    """
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(DeliveryError):
        list_users("https://relay.test/ingest", "admin-secret")

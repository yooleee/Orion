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

import urllib.error

import pytest

from orion.delivery import DeliveryError
from orion.delivery.relay import push


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

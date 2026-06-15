# =============================================================================
# tests/test_delivery.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the Discord sender's request shape, success/failure
#                  handling, and length truncation — WITHOUT real network calls.
# Role in project: Delivery is the only outbound step; it must POST the right JSON
#                  and translate every failure into DeliveryError so the CLI can
#                  report a failed recipient and decide on state advancement.
# Test approach: urllib.request.urlopen is monkeypatched to capture the request
#                and return a canned response (or raise) — no HTTP happens.
# =============================================================================

import json
import urllib.error

import pytest

from orion.delivery import DeliveryError
from orion.delivery.discord import _DISCORD_CONTENT_LIMIT, send


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


def test_successful_post_sends_content_json(monkeypatch):
    """A 204 response counts as success and the body is the message as JSON.

    Why this matters: Discord returns 204 on success and expects a JSON body with
    a `content` field; getting either wrong means messages silently never arrive.
    """
    captured = {}

    def fake_urlopen(request, timeout=None):
        # Record what was sent so we can assert on it.
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["content_type"] = request.headers.get("Content-type")
        # urllib title-cases header keys, so "User-Agent" is stored as "User-agent".
        captured["user_agent"] = request.headers.get("User-agent")
        return _FakeResponse(204)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    send("Hello supervisor", "https://discord.test/webhook")

    assert captured["url"] == "https://discord.test/webhook"
    assert captured["body"] == {"content": "Hello supervisor"}
    assert captured["content_type"] == "application/json"
    # A descriptive, non-default User-Agent is REQUIRED: Discord's edge 403s the
    # default Python-urllib UA. This pins the header so we never regress to a
    # silently-broken delivery path.
    assert captured["user_agent"] is not None
    assert "Orion" in captured["user_agent"]
    assert "Python-urllib" not in captured["user_agent"]


def test_http_error_becomes_delivery_error(monkeypatch):
    """A 4xx/5xx (HTTPError) is translated into DeliveryError.

    Why this matters: the CLI catches DeliveryError per recipient; a raw
    urllib exception would crash the run instead of reporting one failed
    recipient.
    """
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 400, "Bad Request", hdrs=None, fp=None
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(DeliveryError):
        send("msg", "https://discord.test/webhook")


def test_connection_error_becomes_delivery_error(monkeypatch):
    """A network failure (URLError) is translated into DeliveryError.

    Why this matters: same uniform handling for unreachable webhooks as for HTTP
    errors.
    """
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(DeliveryError):
        send("msg", "https://discord.test/webhook")


def test_long_message_is_truncated(monkeypatch):
    """A message over Discord's limit is truncated before sending.

    Why this matters: Discord rejects content over 2000 chars; truncating avoids a
    hard failure on a long (but already-redacted) report.
    """
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(204)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    send("A" * 5000, "https://discord.test/webhook")

    assert len(captured["body"]["content"]) <= _DISCORD_CONTENT_LIMIT

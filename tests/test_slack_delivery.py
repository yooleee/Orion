# =============================================================================
# tests/test_slack_delivery.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the Slack sender's request shape, success/failure
#                  handling, and length truncation — WITHOUT real network calls.
# Role in project: Slack is the Phase-3 second channel; like Discord, it must POST
#                  the right JSON and translate every failure into DeliveryError so
#                  the CLI handles a failed Slack recipient uniformly.
# Test approach: urllib.request.urlopen is monkeypatched to capture the request
#                and return a canned response (or raise) — no HTTP happens. Mirrors
#                test_delivery.py so the two channels are held to the same contract.
# =============================================================================

import json
import urllib.error

import pytest

from orion.delivery import DeliveryError
from orion.delivery.slack import _SLACK_TEXT_LIMIT, send


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


def test_successful_post_sends_text_json(monkeypatch):
    """A 200 response counts as success and the body is the message as {"text": …}.

    Why this matters: Slack returns 200 on success and expects a JSON body with a
    `text` field (NOT Discord's `content`); getting either wrong means messages
    silently never arrive in the Slack channel.
    """
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["content_type"] = request.headers.get("Content-type")
        return _FakeResponse(200)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    send("Hello supervisor", "https://hooks.slack.test/services/X")

    assert captured["url"] == "https://hooks.slack.test/services/X"
    # The field is `text`, the Slack-specific difference from Discord's `content`.
    assert captured["body"] == {"text": "Hello supervisor"}
    assert captured["content_type"] == "application/json"


def test_http_error_becomes_delivery_error(monkeypatch):
    """A 4xx/5xx (HTTPError) is translated into DeliveryError.

    Why this matters: the CLI catches DeliveryError per recipient; a raw urllib
    exception would crash the run instead of reporting one failed recipient.
    """
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 400, "invalid_payload", hdrs=None, fp=None
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(DeliveryError):
        send("msg", "https://hooks.slack.test/services/X")


def test_connection_error_becomes_delivery_error(monkeypatch):
    """A network failure (URLError) is translated into DeliveryError.

    Why this matters: same uniform handling for an unreachable Slack webhook as
    for an HTTP error, so the CLI never sees a raw urllib exception.
    """
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(DeliveryError):
        send("msg", "https://hooks.slack.test/services/X")


def test_long_message_is_truncated(monkeypatch):
    """A message over Slack's text limit is truncated before sending.

    Why this matters: a pathologically long (but already-redacted) report should
    degrade to a truncated message, not a hard failure.
    """
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(200)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    send("A" * (_SLACK_TEXT_LIMIT + 1000), "https://hooks.slack.test/services/X")

    assert len(captured["body"]["text"]) <= _SLACK_TEXT_LIMIT

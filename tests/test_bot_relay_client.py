# =============================================================================
# tests/test_bot_relay_client.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the bot→relay comment POST's request shape (derived
#                  URL, Bearer header, JSON body), success handling, and failure
#                  translation to DeliveryError — WITHOUT real network calls.
# Role in project: post_comment is the bot's outbound seam. It must hit the relay's
#                  POST /api/comments with the right auth and body (the exact shape
#                  the endpoint built in checkpoint A consumes), and turn every
#                  failure into DeliveryError so the shell can fail-soft.
# Test approach: urllib.request.urlopen is monkeypatched to capture the request and
#                return a canned response (or raise) — mirrors test_relay_delivery.py.
# =============================================================================

import json
import urllib.error

import pytest

from orion.bot.relay_client import post_comment
from orion.delivery import DeliveryError


class _FakeResponse:
    """Context-manager stand-in for urlopen's return value (mirrors the other tests).

    Why:
        urlopen is used as `with urlopen(...) as response:`, so the fake must support
        the context-manager protocol and expose `.status` like the real object.
    """

    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_successful_post_derives_url_and_sends_bearer_json(monkeypatch):
    """A 201 is success; the URL is derived to /api/comments with Bearer + JSON body.

    Why this matters: the single configured [relay].url points at ".../ingest"; the
    comment client must derive ".../api/comments" from it (so one config value serves
    push, pull, and this write) and send the {project, author, body} JSON the endpoint
    validates, authenticated with the shared Bearer token.
    """
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["content_type"] = request.headers.get("Content-type")
        captured["authorization"] = request.headers.get("Authorization")
        captured["user_agent"] = request.headers.get("User-agent")
        return _FakeResponse(201)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    post_comment(
        "https://relay.test/ingest", "s3cr3t-token", "demo", "Alex", "From Slack."
    )

    # The ingest URL's path is replaced wholesale by the root-relative /api/comments.
    assert captured["url"] == "https://relay.test/api/comments"
    assert captured["body"] == {"project": "demo", "author": "Alex", "body": "From Slack."}
    assert captured["content_type"] == "application/json"
    assert captured["authorization"] == "Bearer s3cr3t-token"
    assert "Orion" in captured["user_agent"]


def test_url_derivation_ignores_configured_path_segment(monkeypatch):
    """The comment URL is root-relative, so any configured ingest path is replaced.

    Why this matters: a self-hoster might configure the relay url with a different
    path; the derivation must always land on the host's /api/comments regardless,
    matching how pull_comments derives its read URL.
    """
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        return _FakeResponse(201)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    post_comment("https://relay.test/some/other/path", "t", "demo", "", "hi")
    assert captured["url"] == "https://relay.test/api/comments"


def test_non_2xx_status_becomes_delivery_error(monkeypatch):
    """A non-2xx response status (returned, not raised) is translated to DeliveryError.

    Why this matters: the shell catches DeliveryError to fail-soft. A surprising
    status that comes back as a normal response must still be treated as a failure,
    not silently accepted as a stored comment.
    """
    def fake_urlopen(request, timeout=None):
        return _FakeResponse(200 - 1)  # 199: not a 2xx

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(DeliveryError):
        post_comment("https://relay.test/ingest", "t", "demo", "Alex", "hi")


@pytest.mark.parametrize("code", [400, 401, 404, 500])
def test_http_error_becomes_delivery_error(monkeypatch, code):
    """4xx/5xx (raised as HTTPError) become DeliveryError carrying the code.

    Why this matters: the endpoint can reject with 401 (bad token), 400 (bad body),
    or 404 (no report yet). Each arrives as an HTTPError and must surface as a
    reported, non-fatal DeliveryError rather than crashing the bot's event loop.
    """
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, code, "boom", hdrs=None, fp=None
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(DeliveryError, match=str(code)):
        post_comment("https://relay.test/ingest", "t", "demo", "Alex", "hi")


def test_connection_error_becomes_delivery_error(monkeypatch):
    """A URLError (relay down / DNS / timeout) becomes DeliveryError.

    Why this matters: an offline relay must not take the bot down. A transport-level
    failure is translated to the same fail-soft DeliveryError as an HTTP rejection,
    so the shell logs it and keeps listening.
    """
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(DeliveryError, match="Could not reach relay"):
        post_comment("https://relay.test/ingest", "t", "demo", "Alex", "hi")

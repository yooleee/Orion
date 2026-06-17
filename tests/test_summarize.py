# =============================================================================
# tests/test_summarize.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the Anthropic summarizer backend's call shape,
#                  share-level prompting, configured-model pass-through, and error
#                  wrapping — WITHOUT making a real Anthropic API call.
# Role in project: AnthropicSummarizer is the default backend behind the B4
#                  Summarizer seam — the one place Orion talks to Claude. These
#                  tests pin that it sends what it's given, uses the CONFIGURED
#                  model, honors the share level, and converts API failures into
#                  SummarizerError so the CLI can fail closed (abort, don't advance
#                  state).
# Test approach: a fake client is injected (dependency injection). It records the
#                kwargs it was called with and returns a canned response, so we
#                test our logic, not Anthropic's service.
# =============================================================================

import json
import urllib.error
from types import SimpleNamespace

import pytest

from orion import summarize
from orion.summarize import AnthropicSummarizer, LocalSummarizer, SummarizerError


class _FakeMessages:
    """Stand-in for client.messages — records the call and returns canned text.

    Why:
        Lets a test assert exactly what we sent (model, max_tokens, system,
        messages) and control the response, with no network and no API key.
    """

    def __init__(self, reply_text="A concise progress summary.", error=None):
        self.reply_text = reply_text
        self.error = error
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        if self.error is not None:
            raise self.error
        # Mirror the real response shape: .content is a list of typed blocks.
        block = SimpleNamespace(type="text", text=self.reply_text)
        return SimpleNamespace(content=[block])


class _FakeClient:
    """Minimal anthropic.Anthropic stand-in exposing only .messages."""

    def __init__(self, messages):
        self.messages = messages


def _anthropic(client, model="claude-haiku-4-5"):
    """Build an AnthropicSummarizer over a fake client (test helper).

    Args:
        client: A _FakeClient stand-in for anthropic.Anthropic.
        model: The model id to configure (defaults to Haiku).

    Returns:
        An AnthropicSummarizer ready to .summarize().

    Why:
        Every test constructs the backend the same way; centralizing it keeps each
        test to the one thing it asserts (DRY) and mirrors how cli._build_summarizer
        injects the client + configured model.
    """
    return AnthropicSummarizer(client, model)


def test_returns_model_summary_text():
    """summarize() returns the text Claude produced.

    Why this matters: this string becomes the report body; if we fail to extract
    it from the response blocks, the report would be empty.
    """
    client = _FakeClient(_FakeMessages(reply_text="Shipped the git collector."))
    out = _anthropic(client).summarize("git activity here", "high_level")
    assert out == "Shipped the git collector."


def test_sends_the_given_text_and_configured_model():
    """The provided (already-redacted) text is sent, using the CONFIGURED model.

    Why this matters: confirms the activity text reaches the API as the user
    message, and — the heart of B4 — that the model the config chose is the model
    actually requested. We pass a non-default id to prove it is not hardcoded.
    """
    fake = _FakeMessages()
    client = _FakeClient(fake)
    _anthropic(client, model="claude-sonnet-4-6").summarize(
        "DISTINCTIVE_ACTIVITY_TEXT", "high_level"
    )

    # The configured model flows through to the API call (no hardcoded constant).
    assert fake.last_kwargs["model"] == "claude-sonnet-4-6"
    # The activity text is in the user message content.
    messages = fake.last_kwargs["messages"]
    assert any("DISTINCTIVE_ACTIVITY_TEXT" in m["content"] for m in messages)
    # max_tokens is bounded (a progress summary is short).
    assert fake.last_kwargs["max_tokens"] <= 2048


def test_share_level_changes_the_system_prompt():
    """high_level and detailed produce different system prompts.

    Why this matters: the share level is the privacy dial; the two levels must
    actually instruct the model differently (high_level = outcomes only,
    detailed = more specifics), or the dial does nothing.
    """
    fake_high = _FakeMessages()
    _anthropic(_FakeClient(fake_high)).summarize("x", "high_level")
    fake_detailed = _FakeMessages()
    _anthropic(_FakeClient(fake_detailed)).summarize("x", "detailed")

    assert fake_high.last_kwargs["system"] != fake_detailed.last_kwargs["system"]


def test_api_error_is_wrapped_as_summarizer_error():
    """An Anthropic APIError becomes a SummarizerError.

    Why this matters: the CLI must be able to catch summarizer failures
    specifically and abort the run BEFORE sending anything — and crucially
    without advancing state, so a retry re-reports the same delta. A raw API
    exception leaking out would bypass that fail-closed handling.
    """
    import anthropic

    # APIConnectionError is a concrete anthropic.APIError subclass; construct one
    # with a dummy request to simulate a network failure.
    err = anthropic.APIConnectionError(request=SimpleNamespace())
    client = _FakeClient(_FakeMessages(error=err))

    with pytest.raises(SummarizerError):
        _anthropic(client).summarize("x", "high_level")


def test_empty_model_reply_raises():
    """An empty/whitespace summary from the model is treated as an error.

    Why this matters: sending an empty report is useless and confusing; better to
    fail closed than deliver a blank update. (The CLI also guards this, but the
    summarizer refusing to return empty is defense in depth.)
    """
    client = _FakeClient(_FakeMessages(reply_text="   "))
    with pytest.raises(SummarizerError):
        _anthropic(client).summarize("x", "high_level")


# --- LocalSummarizer (OpenAI-compatible endpoint over stdlib urllib) ----------
#
# The local backend talks HTTP, so instead of a client object we patch
# urllib.request.urlopen (the same function the delivery modules use). A fake
# response stands in for the endpoint, so nothing leaves the machine and no server
# is required. We patch it ON the summarize module's urllib so only this code path
# is affected.


class _FakeResponse:
    """Context-manager stand-in for an HTTP response with a canned body.

    Why:
        urllib.request.urlopen returns an object used as `with ... as r: r.read()`.
        This mimics exactly that surface so LocalSummarizer's reading code runs
        unchanged against a body we control.
    """

    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _chat_response(content) -> bytes:
    """Encode an OpenAI-compatible chat-completions reply with the given content.

    Args:
        content: The assistant message content to embed (a str, or non-str to
            simulate a malformed reply).

    Returns:
        The JSON body as bytes, as urlopen().read() would return.

    Why:
        Every happy-path test needs the same nested {"choices":[{"message":...}]}
        shape; building it once keeps each test to the field it cares about (DRY).
    """
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")


def test_local_summarizer_posts_openai_shape_and_returns_content(monkeypatch):
    """The local backend POSTs the OpenAI chat shape and returns the reply.

    Why this matters: this is the proof the seam is real with a second, totally
    different transport. We pin the appended /chat/completions path, the configured
    model, that the activity text rides as the user message, AND that the shared
    security system prompt ("report OUTCOMES, not code") is sent as the system
    message — the redaction-contract must-hold for every backend.
    """
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data)
        captured["has_auth"] = request.has_header("Authorization")
        return _FakeResponse(_chat_response("Local summary here."))

    monkeypatch.setattr(summarize.urllib.request, "urlopen", fake_urlopen)

    out = LocalSummarizer("http://localhost:11434/v1", "llama3.1").summarize(
        "DISTINCTIVE_ACTIVITY", "high_level"
    )

    assert out == "Local summary here."
    # The chat-completions path is appended to the configured base_url.
    assert captured["url"] == "http://localhost:11434/v1/chat/completions"
    assert captured["body"]["model"] == "llama3.1"
    messages = captured["body"]["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    # The (already-redacted) activity is the user message.
    assert messages[1]["content"] == "DISTINCTIVE_ACTIVITY"
    # The shared security prompt reaches the local backend too.
    assert "OUTCOMES" in messages[0]["content"]
    # No Authorization header when no api_key was configured.
    assert captured["has_auth"] is False


def test_local_summarizer_sends_bearer_when_api_key_set(monkeypatch):
    """An api_key is sent as a Bearer Authorization header.

    Why this matters: the few OpenAI-compatible endpoints that require a key use
    Bearer auth; we pin that a configured key is attached, and (above) that none is
    sent when there is no key — so a local server that needs none is never blocked.
    """
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["auth"] = request.get_header("Authorization")
        return _FakeResponse(_chat_response("ok"))

    monkeypatch.setattr(summarize.urllib.request, "urlopen", fake_urlopen)

    LocalSummarizer("http://x/v1", "m", api_key="secret-token").summarize("x", "high_level")
    assert captured["auth"] == "Bearer secret-token"


def test_local_summarizer_unreachable_endpoint_raises(monkeypatch):
    """A connection failure (server down) becomes a SummarizerError.

    Why this matters: a local server that isn't running must fail closed — abort
    the run before sending and without advancing state — exactly like an Anthropic
    network failure, not crash with a raw URLError.
    """

    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(summarize.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(SummarizerError):
        LocalSummarizer("http://localhost:11434/v1", "llama3.1").summarize("x", "high_level")


def test_local_summarizer_http_error_raises(monkeypatch):
    """A non-2xx HTTP response (e.g. unknown model) becomes a SummarizerError.

    Why this matters: a 4xx/5xx from the endpoint is a real failure, not a summary;
    it must fail closed rather than be treated as report content.
    """

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError("http://x", 500, "Server Error", {}, None)

    monkeypatch.setattr(summarize.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(SummarizerError):
        LocalSummarizer("http://x/v1", "m").summarize("x", "high_level")


def test_local_summarizer_unexpected_shape_raises(monkeypatch):
    """A reply missing the choices/message fields becomes a SummarizerError.

    Why this matters: a non-OpenAI-shaped body (a different server, an error JSON)
    must not silently produce a blank or wrong report — we fail closed on a shape
    we don't recognize.
    """

    def fake_urlopen(request, timeout=None):
        return _FakeResponse(json.dumps({"unexpected": "shape"}).encode("utf-8"))

    monkeypatch.setattr(summarize.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(SummarizerError):
        LocalSummarizer("http://x/v1", "m").summarize("x", "high_level")


def test_local_summarizer_empty_content_raises(monkeypatch):
    """A whitespace-only reply is treated as an error (shared empty-check).

    Why this matters: same defense-in-depth as the Anthropic backend — an empty
    summary is useless, so _require_nonempty rejects it for every backend.
    """

    def fake_urlopen(request, timeout=None):
        return _FakeResponse(_chat_response("   "))

    monkeypatch.setattr(summarize.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(SummarizerError):
        LocalSummarizer("http://x/v1", "m").summarize("x", "high_level")

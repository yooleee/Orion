# =============================================================================
# tests/test_summarize.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the summarizer's call shape, share-level prompting,
#                  and error wrapping — WITHOUT making a real Anthropic API call.
# Role in project: summarize_raw is the one place Orion talks to Claude. These
#                  tests pin that it sends what it's given, honors the share
#                  level, and converts API failures into SummarizerError so the
#                  CLI can fail closed (abort, don't advance state).
# Test approach: a fake client is injected (dependency injection). It records the
#                kwargs it was called with and returns a canned response, so we
#                test our logic, not Anthropic's service.
# =============================================================================

from types import SimpleNamespace

import pytest

from orion.summarize import SummarizerError, summarize_raw


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


def test_returns_model_summary_text():
    """summarize_raw returns the text Claude produced.

    Why this matters: this string becomes the report body; if we fail to extract
    it from the response blocks, the report would be empty.
    """
    client = _FakeClient(_FakeMessages(reply_text="Shipped the git collector."))
    out = summarize_raw("git activity here", "high_level", client=client)
    assert out == "Shipped the git collector."


def test_sends_the_given_text_and_correct_model():
    """The provided (already-redacted) text is sent, using the Haiku model.

    Why this matters: confirms we use the configured lightweight model and that
    the activity text actually reaches the API as the user message.
    """
    fake = _FakeMessages()
    client = _FakeClient(fake)
    summarize_raw("DISTINCTIVE_ACTIVITY_TEXT", "high_level", client=client)

    assert fake.last_kwargs["model"] == "claude-haiku-4-5"
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
    summarize_raw("x", "high_level", client=_FakeClient(fake_high))
    fake_detailed = _FakeMessages()
    summarize_raw("x", "detailed", client=_FakeClient(fake_detailed))

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
        summarize_raw("x", "high_level", client=client)


def test_empty_model_reply_raises():
    """An empty/whitespace summary from the model is treated as an error.

    Why this matters: sending an empty report is useless and confusing; better to
    fail closed than deliver a blank update. (The CLI also guards this, but the
    summarizer refusing to return empty is defense in depth.)
    """
    client = _FakeClient(_FakeMessages(reply_text="   "))
    with pytest.raises(SummarizerError):
        summarize_raw("x", "high_level", client=client)

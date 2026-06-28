# =============================================================================
# tests/test_extract.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the disciplines extractor seam (E2 Inc 4 slice 4b) —
#                  the JSON parsing/validation that turns a model reply into
#                  Disciplines, and the Anthropic backend over a FAKE client.
# Role in project: This is the producer-side LLM step for disciplines. If parsing
#                  is wrong, malformed model output corrupts the dashboard; if the
#                  backend trusts the model's `source`, the "observed · <doc>" claim
#                  becomes a lie. These tests pin both. No real API is ever called.
# =============================================================================

from types import SimpleNamespace

import anthropic
import pytest

from orion.extract import (
    AnthropicDisciplineExtractor,
    Discipline,
    ExtractError,
    _parse_disciplines,
)


# --- Fake Anthropic client (mirrors tests/test_summarize.py's pattern) -----------
# The backend takes an injected client; a fake records the call and returns a canned
# reply, so we test the backend's parsing/error handling with no network, no API key.


class _FakeMessages:
    """Stand-in for client.messages — records kwargs and returns canned text."""

    def __init__(self, reply_text="[]", error=None):
        self.reply_text = reply_text
        self.error = error
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        if self.error is not None:
            raise self.error
        block = SimpleNamespace(type="text", text=self.reply_text)
        return SimpleNamespace(content=[block])


class _FakeClient:
    """Minimal anthropic.Anthropic stand-in carrying a fake `messages`."""

    def __init__(self, messages):
        self.messages = messages


def _extractor(reply_text="[]", error=None, model="claude-haiku-4-5"):
    """Build an AnthropicDisciplineExtractor over a fake client (test helper)."""
    return AnthropicDisciplineExtractor(_FakeClient(_FakeMessages(reply_text, error)), model)


# --- _parse_disciplines: the validation contract ---------------------------------


def test_parse_valid_array_stamps_caller_source():
    """A well-formed array parses, and `source` is the CALLER's value, not the model's.

    Why this matters: the "observed · <doc>" footer is only honest if the source is
    deterministic and caller-stamped. Even if a model tried to supply its own source,
    the parser ignores it and stamps the value we pass in.
    """
    raw = (
        '[{"title": "Untrusted text is inert", "why": "Rendered as plain text.", '
        '"scope": "global", "source": "MODEL_TRIED_TO_SET_THIS"}]'
    )
    out = _parse_disciplines(raw, "design/README.md")
    assert out == (
        Discipline(
            title="Untrusted text is inert",
            why="Rendered as plain text.",
            scope="global",
            source="design/README.md",
        ),
    )


def test_parse_skips_malformed_items_but_keeps_valid_ones():
    """Individual bad cards are skipped (fail-soft); valid ones survive.

    Why this matters: one malformed card from the model must not discard a doc's whole
    extraction. We drop items missing a field, with a blank field, or an unknown scope.
    """
    raw = (
        "["
        '{"title": "Good", "why": "reason", "scope": "project"},'  # valid
        '{"title": "", "why": "reason", "scope": "global"},'  # blank title
        '{"title": "No why", "scope": "global"},'  # missing why
        '{"title": "Bad scope", "why": "r", "scope": "elsewhere"},'  # bad scope
        '"not even an object",'  # wrong type
        '{"title": "AlsoGood", "why": "r2", "scope": "global"}'  # valid
        "]"
    )
    out = _parse_disciplines(raw, "CLAUDE.md")
    assert [d.title for d in out] == ["Good", "AlsoGood"]
    assert all(d.source == "CLAUDE.md" for d in out)


def test_parse_empty_array_is_valid():
    """An empty array means 'no principles stated' — a legitimate answer, not an error.

    Why this matters: a doc with no stated principles must extract to nothing without
    raising, so it simply contributes no cards.
    """
    assert _parse_disciplines("[]", "CLAUDE.md") == ()


def test_parse_tolerates_a_code_fence():
    """A ```json ... ``` fence (which some models add) is stripped before parsing.

    Why this matters: robustness against a common model habit, so a correct answer
    wrapped in a fence is not thrown away as 'non-JSON'.
    """
    raw = '```json\n[{"title": "T", "why": "w", "scope": "global"}]\n```'
    out = _parse_disciplines(raw, "CLAUDE.md")
    assert [d.title for d in out] == ["T"]


def test_parse_non_array_raises():
    """A non-array response is a contract break and raises ExtractError.

    Why this matters: a JSON object (not array) or prose means the model and our
    contract disagree — that is a doc-level failure the collector should fail soft on,
    not silently treat as zero principles.
    """
    with pytest.raises(ExtractError):
        _parse_disciplines('{"title": "T"}', "CLAUDE.md")
    with pytest.raises(ExtractError):
        _parse_disciplines("Here are the principles:", "CLAUDE.md")


# --- AnthropicDisciplineExtractor: the backend over the fake client --------------


def test_backend_returns_parsed_disciplines():
    """The backend extracts the text block, parses it, and stamps the source.

    Why this matters: this is the end-to-end backend path with a canned reply — it
    must produce validated Disciplines carrying the caller's source.
    """
    reply = '[{"title": "Local-first", "why": "Runs on your machine.", "scope": "global"}]'
    out = _extractor(reply).extract("doc text", source="CLAUDE.md")
    assert out == (
        Discipline(
            title="Local-first",
            why="Runs on your machine.",
            scope="global",
            source="CLAUDE.md",
        ),
    )


def test_backend_sends_doc_as_user_message():
    """The (already-redacted) doc text is sent as the user message.

    Why this matters: confirms the backend passes the doc to the model as content (the
    caller's redaction is what protects it), and uses the configured model id.
    """
    ext = _extractor('[]', model="claude-haiku-4-5")
    ext.extract("REDACTED DOC BODY", source="CLAUDE.md")
    kwargs = ext._client.messages.last_kwargs
    assert kwargs["model"] == "claude-haiku-4-5"
    assert kwargs["messages"] == [{"role": "user", "content": "REDACTED DOC BODY"}]


def test_backend_wraps_api_error():
    """An Anthropic APIError becomes an ExtractError so the collector can fail soft.

    Why this matters: the collector catches ExtractError to skip one failing doc; a
    leaked SDK exception would instead abort the whole snapshot.
    """
    err = anthropic.APIError("boom", request=None, body=None)
    with pytest.raises(ExtractError):
        _extractor(error=err).extract("doc", source="CLAUDE.md")

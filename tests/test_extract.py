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
    SKILLS_MAX_TOKENS,
    AnthropicDisciplineExtractor,
    AnthropicSkillExtractor,
    Discipline,
    ExtractError,
    Skill,
    VocabSkill,
    _parse_attribution,
    _parse_disciplines,
    _parse_skills,
    _parse_vocab,
)


# --- Fake Anthropic client (mirrors tests/test_summarize.py's pattern) -----------
# The backend takes an injected client; a fake records the call and returns a canned
# reply, so we test the backend's parsing/error handling with no network, no API key.


class _FakeMessages:
    """Stand-in for client.messages — records kwargs and returns canned text.

    `stop_reason` defaults to "end_turn" (a complete reply); pass "max_tokens" to simulate
    a TRUNCATED response, which the skills extractor must treat as a failure.
    """

    def __init__(self, reply_text="[]", error=None, stop_reason="end_turn"):
        self.reply_text = reply_text
        self.error = error
        self.stop_reason = stop_reason
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        if self.error is not None:
            raise self.error
        block = SimpleNamespace(type="text", text=self.reply_text)
        return SimpleNamespace(content=[block], stop_reason=self.stop_reason)


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


# --- _parse_skills: the skills validation contract (E2 Inc 4 slice 4c) ------------
# Skills are stricter on IDENTITY (name + category required) and LENIENT on the
# secondary fields (weight clamps, signals filter), so one odd field degrades a card
# rather than dropping a real competency. These pin that asymmetry.


def _skill_extractor(reply_text="[]", error=None, model="claude-haiku-4-5"):
    """Build an AnthropicSkillExtractor over a fake client (reuses the doc fakes)."""
    return AnthropicSkillExtractor(_FakeClient(_FakeMessages(reply_text, error)), model)


def test_parse_skills_valid_array():
    """A well-formed array parses into Skills with all fields preserved.

    Why this matters: the baseline contract — the wire shape the relay merge depends on
    is exactly {name, category, evidence, weight, signals}.
    """
    raw = (
        '[{"name": "Python stdlib backends", "category": "Backend", '
        '"evidence": "Built the relay.", "weight": 3, "signals": ["git", "docs"]}]'
    )
    out = _parse_skills(raw)
    assert out == (
        Skill(
            name="Python stdlib backends",
            category="Backend",
            evidence="Built the relay.",
            weight=3,
            signals=("git", "docs"),
        ),
    )


def test_parse_skills_clamps_weight_and_filters_signals():
    """An out-of-range weight clamps and unknown signal kinds are dropped.

    Why this matters: the secondary fields must degrade, not corrupt — a weight of 9
    becomes the max tooth height (not an oversized one), and a bogus signal never
    reaches the SPA's provenance display. Signals come back in SKILL_SIGNALS order.
    """
    raw = (
        '[{"name": "X", "category": "Backend", "evidence": "", '
        '"weight": 9, "signals": ["bogus", "docs", "git"]}]'
    )
    out = _parse_skills(raw)
    assert out[0].weight == 3  # clamped to SKILL_WEIGHT_MAX
    assert out[0].signals == ("git", "docs")  # filtered + reordered to canonical order


def test_parse_skills_defaults_missing_weight_and_signals():
    """A card with no weight/signals still parses (min weight, empty signals).

    Why this matters: identity is enough to draw a tooth — a missing secondary field
    must not drop a real skill.
    """
    raw = '[{"name": "X", "category": "Backend", "evidence": "ev"}]'
    out = _parse_skills(raw)
    assert out[0].weight == 1
    assert out[0].signals == ()


def test_parse_skills_drops_cards_missing_identity():
    """Cards missing name or category are skipped; valid ones survive.

    Why this matters: a tooth with no name or no group is unusable; a single malformed
    card must not abort the whole extraction.
    """
    raw = (
        '[{"category": "Backend", "evidence": "no name"},'
        ' {"name": "Has name", "evidence": "no category"},'
        ' {"name": "Good", "category": "Frontend", "evidence": "ok", "weight": 2, "signals": []}]'
    )
    out = _parse_skills(raw)
    assert [s.name for s in out] == ["Good"]


def test_parse_skills_non_array_raises():
    """A non-array reply is a contract break and raises ExtractError.

    Why this matters: a single bad card is skipped, but a wholesale contract break
    (object, prose) must surface so the CLI aborts rather than pushing garbage.
    """
    with pytest.raises(ExtractError):
        _parse_skills('{"name": "X"}')
    with pytest.raises(ExtractError):
        _parse_skills("Here are the skills:")


def test_skills_backend_sends_bundle_and_parses():
    """The redacted evidence bundle is sent as the user message and the reply parsed.

    Why this matters: confirms the backend passes the bundle to the configured model
    and returns parsed Skills, with no real API call.
    """
    reply = '[{"name": "A", "category": "Backend", "evidence": "e", "weight": 1, "signals": ["git"]}]'
    ext = _skill_extractor(reply, model="claude-haiku-4-5")
    out = ext.extract("REDACTED EVIDENCE BUNDLE")
    kwargs = ext._client.messages.last_kwargs
    assert kwargs["model"] == "claude-haiku-4-5"
    assert kwargs["messages"] == [{"role": "user", "content": "REDACTED EVIDENCE BUNDLE"}]
    assert [s.name for s in out] == ["A"]


def test_skills_backend_wraps_api_error():
    """An Anthropic APIError becomes an ExtractError (so the CLI aborts without pushing).

    Why this matters: a leaked SDK exception would crash; the wrapped error is what the
    CLI catches to leave the relay's prior skills intact.
    """
    err = anthropic.APIError("boom", request=None, body=None)
    with pytest.raises(ExtractError):
        _skill_extractor(error=err).extract("bundle")


# --- _parse_vocab: the pass-1 controlled-vocabulary contract (global rework) --------


def test_parse_vocab_valid_and_dedupes_by_casefold():
    """A pass-1 reply parses to VocabSkills, collapsing case-variant duplicate names.

    Why this matters: pass 1 is the controlled vocabulary pass 2 selects from, so it must
    be duplicate-free. The casefold de-dup is the safety net behind the prompt's
    global-dedup instruction — if the model still emits two spellings, only the first
    survives, so a project can never be split across both.
    """
    raw = (
        '[{"name": "Bayesian inference", "category": "ML / NLP"},'
        ' {"name": "bayesian inference", "category": "Foundations & theory"},'
        ' {"name": "Python backends", "category": "Backend"}]'
    )
    out = _parse_vocab(raw)
    assert out == (
        VocabSkill(name="Bayesian inference", category="ML / NLP"),  # first spelling wins
        VocabSkill(name="Python backends", category="Backend"),
    )


def test_parse_vocab_skips_malformed_and_raises_on_non_array():
    """Items missing a name/category are skipped; a non-array reply is a contract break.

    Why this matters: one bad entry must not poison the vocabulary, but a wholesale
    contract break (object/prose) must surface so the sync aborts rather than attributing
    against garbage.
    """
    raw = '[{"category": "Backend"}, {"name": "Good", "category": "Backend"}, {"name": ""}]'
    assert [v.name for v in _parse_vocab(raw)] == ["Good"]
    with pytest.raises(ExtractError):
        _parse_vocab('{"name": "X"}')


# --- _parse_attribution: the pass-2 attribution contract (global rework) ------------


_VOCAB = (
    VocabSkill(name="Bayesian inference", category="ML / NLP"),
    VocabSkill(name="Python backends", category="Backend"),
)


def test_parse_attribution_binds_canonical_name_and_category_from_vocab():
    """A returned card takes its name + category from the matched vocabulary entry.

    Why this matters: this is what guarantees the SAME competency carries the SAME name in
    every project, so the relay's casefold merge collapses it. Even if pass 2 cases the
    name differently, the stored card uses the vocabulary's canonical spelling/category
    (the model's own `category`, if any, is ignored).
    """
    raw = (
        '[{"name": "bayesian inference", "weight": 2, "evidence": "Built the prior.", '
        '"signals": ["git", "docs"], "category": "MODEL_TRIED_TO_SET_THIS"}]'
    )
    out = _parse_attribution(raw, _VOCAB)
    assert out == (
        Skill(
            name="Bayesian inference",  # canonical spelling from the vocab, not the reply's case
            category="ML / NLP",  # from the vocab, not the model's category field
            evidence="Built the prior.",
            weight=2,
            signals=("git", "docs"),
        ),
    )


def test_parse_attribution_drops_names_not_in_vocab():
    """A returned skill whose name is not in the vocabulary is DROPPED.

    Why this matters: the controlled-vocabulary guard — pass 2 must not invent a skill the
    global pass did not canonicalize, or it would reintroduce the per-project
    near-duplicates the rework exists to remove. Only the in-vocab card survives.
    """
    raw = (
        '[{"name": "Python backends", "weight": 3, "evidence": "Relay.", "signals": ["git"]},'
        ' {"name": "Invented skill", "weight": 3, "evidence": "Nope.", "signals": ["git"]}]'
    )
    out = _parse_attribution(raw, _VOCAB)
    assert [s.name for s in out] == ["Python backends"]


def test_parse_attribution_clamps_weight_and_raises_on_non_array():
    """Secondary fields degrade (weight clamps); a non-array reply raises.

    Why this matters: like _parse_skills, identity (an in-vocab name) is enough to keep a
    card, with weight clamped rather than the card dropped; a contract break still aborts.
    """
    raw = '[{"name": "Python backends", "weight": 99, "evidence": "", "signals": []}]'
    assert _parse_attribution(raw, _VOCAB)[0].weight == 3  # clamped to SKILL_WEIGHT_MAX
    with pytest.raises(ExtractError):
        _parse_attribution("not json array", _VOCAB)


# --- two-pass backend: model wiring + the truncation guard --------------------------


def test_extract_vocab_and_attribute_use_the_larger_budget():
    """Both passes call the model with SKILLS_MAX_TOKENS and parse their replies.

    Why this matters: the two-pass calls emit more than a single summary, so they must use
    the larger token budget; and attribute must render the vocabulary into the user message
    so the model selects from it.
    """
    vocab_reply = '[{"name": "Python backends", "category": "Backend"}]'
    ext = _skill_extractor(vocab_reply)
    vocab = ext.extract_vocab("### Project: orion\n\n...evidence...")
    assert [v.name for v in vocab] == ["Python backends"]
    assert ext._client.messages.last_kwargs["max_tokens"] == SKILLS_MAX_TOKENS

    attr_reply = '[{"name": "Python backends", "weight": 2, "evidence": "Relay.", "signals": ["git"]}]'
    ext2 = _skill_extractor(attr_reply)
    out = ext2.attribute("bundle for one project", vocab)
    assert [s.name for s in out] == ["Python backends"]
    # The vocabulary is rendered into the user message so the model can select from it.
    sent = ext2._client.messages.last_kwargs["messages"][0]["content"]
    assert "Python backends" in sent
    assert "bundle for one project" in sent


def test_truncated_response_raises_rather_than_parsing_a_partial():
    """A reply that stopped on the token ceiling is treated as a failure, not parsed.

    Why this matters: a valid-but-TRUNCATED JSON array could silently drop a project's
    skills (or whole projects), which the push would then store as a clobbering full-state.
    Raising ExtractError makes the sync abort and leave the relay's prior skills intact.
    """
    # A reply that would parse fine, but the model says it ran out of room.
    reply = '[{"name": "Python backends", "category": "Backend"}]'
    ext = AnthropicSkillExtractor(
        _FakeClient(_FakeMessages(reply, stop_reason="max_tokens")), "claude-sonnet-4-6"
    )
    with pytest.raises(ExtractError):
        ext.extract_vocab("portfolio evidence")

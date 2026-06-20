# =============================================================================
# tests/test_bot_core.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the bot's PURE decision logic (decide_forward) — the
#                  drop/forward rules that encode the inbound threat model and the
#                  channel→project routing — with no network, no slack-bolt, no
#                  asyncio. Plain IncomingMessage values and a dict map.
# Role in project: This logic is the bot's security boundary (loop prevention,
#                  configured-channels-only, untrusted-text handling) and its
#                  routing. Pinning it here means the async shell can stay a thin,
#                  lightly-tested glue layer.
# =============================================================================

from orion.bot.core import (
    MAX_AUTHOR_CHARS,
    MAX_COMMENT_BODY_CHARS,
    ForwardDecision,
    IncomingMessage,
    decide_forward,
)

# A channel→project map reused across tests: one mapped channel ("C_MAPPED").
_MAP = {"C_MAPPED": "demo"}


def _msg(
    *,
    channel_id="C_MAPPED",
    is_bot=False,
    has_subtype=False,
    author="Alex",
    text="Looks good!",
):
    """Build an IncomingMessage with sensible 'would-forward' defaults.

    Why:
        Most tests vary ONE field to exercise a single guard; defaulting the rest to
        a valid forwardable message keeps each test stating only what it changes.
    """
    return IncomingMessage(
        channel_id=channel_id,
        is_bot=is_bot,
        has_subtype=has_subtype,
        author=author,
        text=text,
    )


def test_human_message_in_mapped_channel_is_forwarded():
    """A plain human message in a configured channel forwards with mapped fields.

    Why this matters: this is the happy path — the supervisor's reply must be routed
    to the channel's project, with the body stripped and the author carried through.
    Everything else in this file is a reason NOT to forward; this proves the positive
    case actually produces the comment we POST.
    """
    decision = decide_forward(_msg(text="  Ship it.  "), _MAP)
    assert decision == ForwardDecision.to("demo", "Alex", "Ship it.")


def test_bot_or_webhook_author_is_dropped():
    """A message flagged as bot/webhook-authored is dropped (loop prevention).

    Why this matters: Orion delivers its OWN reports via an incoming webhook, which
    arrive flagged this way. Without this drop the bot would treat its own delivered
    report as a supervisor comment and relay it back — an infinite loop. This is the
    single most important guard.
    """
    decision = decide_forward(_msg(is_bot=True), _MAP)
    assert decision.forward is False
    assert "bot" in decision.reason


def test_message_with_subtype_is_dropped():
    """A message carrying an event subtype (edit/delete/join) is dropped.

    Why this matters: the smallest slice forwards only plain new messages. Edits,
    deletes, and channel-join notices all arrive with a subtype and are out of scope
    — acting on them would relay phantom or duplicate comments.
    """
    decision = decide_forward(_msg(has_subtype=True), _MAP)
    assert decision.forward is False
    assert "subtype" in decision.reason


def test_unconfigured_channel_is_dropped():
    """A message in a channel not in the map is dropped (access control).

    Why this matters: configured-channels-only is the core access rule — a DM or an
    unrelated channel the bot can see must never be forwarded. Only channels an
    operator explicitly bound to a project are acted on.
    """
    decision = decide_forward(_msg(channel_id="C_OTHER"), _MAP)
    assert decision.forward is False
    assert "unconfigured" in decision.reason


def test_empty_or_whitespace_body_is_dropped():
    """A message whose text is empty or only whitespace is dropped.

    Why this matters: a comment must carry text. An attachment-only post (empty text)
    or a whitespace-only message is not a reply worth relaying, and the relay would
    reject it anyway — so we drop it locally first.
    """
    decision = decide_forward(_msg(text="   "), _MAP)
    assert decision.forward is False
    assert "empty" in decision.reason


def test_over_length_body_is_dropped():
    """A body longer than the cap is dropped rather than truncated or sent.

    Why this matters: the bot enforces the relay's body cap client-side, so an
    over-long message becomes a clean local drop instead of a wasted round-trip the
    relay 400s. We send one character over the limit to land exactly on the boundary.
    """
    decision = decide_forward(_msg(text="x" * (MAX_COMMENT_BODY_CHARS + 1)), _MAP)
    assert decision.forward is False
    assert "cap" in decision.reason


def test_body_exactly_at_cap_is_forwarded():
    """A body exactly at the cap length is forwarded (the boundary is inclusive).

    Why this matters: off-by-one guards cut both ways — a message AT the limit is
    valid and must pass, only one OVER is dropped. This pins the boundary so a later
    refactor can't silently turn '<=' into '<'.
    """
    body = "x" * MAX_COMMENT_BODY_CHARS
    decision = decide_forward(_msg(text=body), _MAP)
    assert decision.forward is True
    assert decision.body == body


def test_long_author_is_truncated_on_forward():
    """An over-long author display name is capped to MAX_AUTHOR_CHARS on forward.

    Why this matters: the author is free text from the platform; an unusually long
    display name could trip the relay's author cap. Truncating here keeps the forward
    valid while still attaching whatever name prefix is available.
    """
    decision = decide_forward(_msg(author="N" * (MAX_AUTHOR_CHARS + 50)), _MAP)
    assert decision.forward is True
    assert decision.author == "N" * MAX_AUTHOR_CHARS


def test_guard_order_bot_beats_unconfigured_channel():
    """A bot message in an UNCONFIGURED channel is dropped as a bot, not as a channel.

    Why this matters: the guards run cheapest/most-decisive first, and the order is
    part of the contract — loop prevention is checked before channel routing, so a
    bot message is always recognized as such regardless of where it lands. Pinning
    the reason proves the order, not just the drop.
    """
    decision = decide_forward(_msg(is_bot=True, channel_id="C_OTHER"), _MAP)
    assert decision.forward is False
    assert "bot" in decision.reason

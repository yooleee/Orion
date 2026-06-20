# =============================================================================
# tests/test_bot_slack.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the Slack shell's GLUE — event normalization
#                  (_to_incoming), author resolution (_resolve_author), and the
#                  decide-then-relay path (_process_event) — without a live Socket
#                  Mode connection and without requiring slack-bolt.
# Role in project: slack_bot.py is the thin shell over the pure core. Its glue is
#                  made testable by importing slack-bolt LAZILY (only inside run_bot),
#                  so these functions run on a stock install with fake event dicts and
#                  a fake client/poster. The actual Bolt wiring in run_bot is the
#                  untested-by-unit shell; we only pin that it raises a clear error
#                  when the optional dependency is absent.
# =============================================================================

import importlib.util

import pytest

from orion.bot.core import MAX_AUTHOR_CHARS
from orion.bot.slack_bot import (
    _process_event,
    _resolve_author,
    _to_incoming,
    run_bot,
)
from orion.config import ConfigError
from orion.delivery import DeliveryError

# Whether slack-bolt is importable in this environment. The run_bot missing-dependency
# test only makes sense when it is NOT installed (otherwise run_bot would try to build
# a real App and connect). The core/glue tests below never need it.
_HAS_SLACK_BOLT = importlib.util.find_spec("slack_bolt") is not None

_MAP = {"C_MAPPED": "demo"}


class _FakeClient:
    """A stand-in Slack WebClient exposing only users_info, for author resolution.

    Args:
        profile: the dict to return under ["user"]["profile"], or None to raise (to
            exercise the fail-soft fallback to the raw user id).

    Why:
        _resolve_author calls client.users_info(user=...) and reads the profile. A tiny
        fake lets us drive both the success and failure shapes without slack_sdk.
    """

    def __init__(self, profile=None, raise_on_call=False):
        self._profile = profile
        self._raise = raise_on_call

    def users_info(self, user):
        if self._raise:
            raise RuntimeError("slack api boom")
        return {"user": {"profile": self._profile or {}}}


# --- _to_incoming: Slack event dict → the core's neutral IncomingMessage ---------


def test_to_incoming_plain_message():
    """A plain human message maps to a forwardable IncomingMessage.

    Why this matters: the normal case must carry channel and text through and flag
    neither bot nor subtype, so the core will forward it. Author is intentionally left
    empty (Slack events carry only a user id, resolved separately).
    """
    msg = _to_incoming({"channel": "C_MAPPED", "user": "U1", "text": "hello"})
    assert msg.channel_id == "C_MAPPED"
    assert msg.text == "hello"
    assert msg.is_bot is False
    assert msg.has_subtype is False
    assert msg.author == ""


def test_to_incoming_bot_id_flags_is_bot():
    """An event with a bot_id is flagged is_bot (loop prevention).

    Why this matters: Orion delivers its own reports via an incoming webhook, which
    arrive with a bot_id. Folding that into is_bot is what lets the core drop them so
    the bot never relays its own report back.
    """
    msg = _to_incoming({"channel": "C_MAPPED", "bot_id": "B123", "text": "report"})
    assert msg.is_bot is True


def test_to_incoming_bot_message_subtype_flags_is_bot():
    """The bot_message subtype also flags is_bot, not merely has_subtype.

    Why this matters: some bot posts carry subtype == "bot_message" without a bot_id.
    Treating that as is_bot means it is dropped as a bot (the first, most specific
    guard) rather than incidentally as a subtype.
    """
    msg = _to_incoming(
        {"channel": "C_MAPPED", "subtype": "bot_message", "text": "x"}
    )
    assert msg.is_bot is True
    assert msg.has_subtype is True


def test_to_incoming_edit_subtype_flags_has_subtype():
    """A message edit (subtype message_changed) sets has_subtype.

    Why this matters: the smallest slice forwards only plain new messages; an edit must
    be droppable via has_subtype so we don't relay phantom/duplicate comments.
    """
    msg = _to_incoming(
        {"channel": "C_MAPPED", "subtype": "message_changed", "text": "x"}
    )
    assert msg.is_bot is False
    assert msg.has_subtype is True


def test_to_incoming_missing_fields_default_safely():
    """An event missing channel/text degrades to empty strings, not a KeyError.

    Why this matters: Slack payloads vary; the normalizer must never crash on an absent
    field. Empty channel/text simply lead the core to drop the message.
    """
    msg = _to_incoming({})
    assert msg.channel_id == ""
    assert msg.text == ""


# --- _resolve_author: user id → display name (fail-soft) -------------------------


def test_resolve_author_prefers_display_name():
    """A profile with a display_name resolves to it.

    Why this matters: a readable author is the point — the dashboard should show the
    supervisor's name, not an opaque "U07…" id.
    """
    client = _FakeClient(profile={"display_name": "Alex", "real_name": "Alex Doe"})
    assert _resolve_author(client, "U1") == "Alex"


def test_resolve_author_falls_back_to_real_name_then_id():
    """An empty display_name falls back to real_name, then to the id.

    Why this matters: not every Slack profile sets a display_name; real_name is the next
    best readable label, and the raw id is the last resort so the author is never blank.
    """
    assert _resolve_author(_FakeClient(profile={"real_name": "Sam"}), "U2") == "Sam"
    assert _resolve_author(_FakeClient(profile={}), "U3") == "U3"


def test_resolve_author_failsoft_on_api_error():
    """A users.info failure falls back to the user id rather than raising.

    Why this matters: author resolution is cosmetic — a missing scope or a network blip
    must never drop the comment or crash the listener. The id is a fine stand-in.
    """
    assert _resolve_author(_FakeClient(raise_on_call=True), "U4") == "U4"


def test_resolve_author_empty_user_id_is_empty():
    """An absent user id resolves to an empty author (no API call needed).

    Why this matters: some events have no user; the resolver short-circuits to "" rather
    than calling the API with an empty id.
    """
    assert _resolve_author(_FakeClient(), "") == ""


# --- _process_event: decide, then relay on a forward -----------------------------


def _recording_poster():
    """Return (poster, calls) where poster records its args instead of POSTing.

    Why:
        _process_event's observable effect is the relay POST; a recording fake lets us
        assert exactly what would be sent without any network.
    """
    calls = []

    def poster(relay_url, token, project, author, body):
        calls.append((relay_url, token, project, author, body))

    return poster, calls


def test_process_event_forwards_human_message():
    """A human message in a mapped channel POSTs a comment with resolved fields.

    Why this matters: the end-to-end glue — a real reply resolves the author, routes to
    the channel's project, and relays the stripped body to the configured relay url/token.
    """
    poster, calls = _recording_poster()
    client = _FakeClient(profile={"display_name": "Alex"})
    _process_event(
        {"channel": "C_MAPPED", "user": "U1", "text": "  Ship it.  "},
        client,
        _MAP,
        "https://relay.test/ingest",
        "tok",
        poster=poster,
        author_resolver=_resolve_author,
    )
    assert calls == [("https://relay.test/ingest", "tok", "demo", "Alex", "Ship it.")]


def test_process_event_drops_bot_message_without_posting():
    """A bot-authored message is dropped — the poster is never called.

    Why this matters: this is loop prevention at the glue level — Orion's own delivered
    report (a webhook/bot message) must not trigger a relay POST.
    """
    poster, calls = _recording_poster()
    _process_event(
        {"channel": "C_MAPPED", "bot_id": "B1", "text": "a report"},
        _FakeClient(),
        _MAP,
        "https://relay.test/ingest",
        "tok",
        poster=poster,
    )
    assert calls == []


def test_process_event_drops_unconfigured_channel():
    """A message in an unmapped channel is dropped — no POST.

    Why this matters: configured-channels-only access control, proven through the glue:
    a channel the operator never bound to a project produces no comment.
    """
    poster, calls = _recording_poster()
    _process_event(
        {"channel": "C_OTHER", "user": "U1", "text": "hi"},
        _FakeClient(profile={"display_name": "Alex"}),
        _MAP,
        "https://relay.test/ingest",
        "tok",
        poster=poster,
    )
    assert calls == []


def test_process_event_failsoft_on_delivery_error():
    """A DeliveryError from the poster is swallowed (the listener keeps running).

    Why this matters: a relay outage or auth failure on one reply must not crash the
    always-on bot. _process_event catches DeliveryError and returns normally, so the
    next event is still handled.
    """
    def failing_poster(relay_url, token, project, author, body):
        raise DeliveryError("relay down")

    # Must not raise out of _process_event.
    _process_event(
        {"channel": "C_MAPPED", "user": "U1", "text": "hi"},
        _FakeClient(profile={"display_name": "Alex"}),
        _MAP,
        "https://relay.test/ingest",
        "tok",
        poster=failing_poster,
    )


def test_process_event_caps_long_author():
    """An over-long resolved author is truncated before the POST.

    Why this matters: a pathological display name must not trip the relay's author cap;
    the shell caps it (the relay re-caps server-side as a safety net).
    """
    poster, calls = _recording_poster()
    long_name = "N" * (MAX_AUTHOR_CHARS + 25)
    _process_event(
        {"channel": "C_MAPPED", "user": "U1", "text": "hi"},
        _FakeClient(profile={"display_name": long_name}),
        _MAP,
        "https://relay.test/ingest",
        "tok",
        poster=poster,
    )
    assert calls[0][3] == "N" * MAX_AUTHOR_CHARS


# --- run_bot: the dependency boundary --------------------------------------------


@pytest.mark.skipif(
    _HAS_SLACK_BOLT, reason="slack-bolt is installed; the missing-dep path can't run"
)
def test_run_bot_without_slack_bolt_raises_configerror():
    """When slack-bolt is absent, run_bot raises a ConfigError pointing at the extra.

    Why this matters: the optional dependency is required only to actually start the
    bot. A user who runs `orion bot` without installing it must get a clear, actionable
    message (install orion[slack-bot]), not a raw ImportError — and cmd_bot turns this
    ConfigError into a clean exit 1.
    """
    with pytest.raises(ConfigError, match="slack-bolt"):
        run_bot("xoxb-x", "xapp-x", {"C1": "demo"}, "https://relay.test/ingest", "tok")

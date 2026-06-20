# =============================================================================
# bot/slack_bot.py
# -----------------------------------------------------------------------------
# Responsible for: The always-on Slack listener — the thin "shell" that holds a
#                  Socket Mode connection, normalizes each inbound message event
#                  into the pure core's IncomingMessage, asks decide_forward what to
#                  do, and (on a forward) posts the comment to the relay.
# Role in project: This is the ONLY module that touches slack-bolt, and it imports
#                  it LAZILY (inside run_bot), so importing this module — or the CLI
#                  — never requires the optional dependency. All the branching logic
#                  lives in core.py (pure, fully tested); everything here is glue.
# Why slack-bolt (Socket Mode): a bot must hold a live connection to receive events.
#                  Socket Mode is an OUTBOUND WebSocket (no public inbound endpoint to
#                  host or secure — unlike the Events API HTTP path), the smallest,
#                  most secure inbound posture. Bolt owns the Socket Mode handshake,
#                  heartbeat, and reconnect — the whole reason to take the dependency
#                  instead of hand-rolling a fragile WebSocket loop. Bolt's sync App +
#                  SocketModeHandler run on a background thread and block on .start(),
#                  so there is NO asyncio in this codebase — it fits the sync core and
#                  the relay's serve_forever() pattern.
# Stage-bound note: shipping slack-bolt as an optional, lazily-imported extra is the
#                  smallest-slice shape. As the bot becomes load-bearing this is
#                  expected to graduate to a first-class integration; the seams (the
#                  pure core, the relay client, the platform-neutral config) keep that
#                  additive. See plans/build-the-native-bots-slice-squishy-key.md.
# =============================================================================

from __future__ import annotations

import sys

from orion.bot.core import MAX_AUTHOR_CHARS, IncomingMessage, decide_forward
from orion.bot.relay_client import post_comment
from orion.config import ConfigError
from orion.delivery import DeliveryError


def _to_incoming(event: dict) -> IncomingMessage:
    """Normalize a Slack `message` event dict into the pure core's IncomingMessage.

    Args:
        event: The Slack event payload (a dict). Relevant keys: `channel` (the
            channel id), `text` (the message body), `user` (the author's user id),
            `bot_id` (present when a bot/app authored it), and `subtype` (present on
            non-plain messages: edits, deletes, joins, and bot_message).

    Returns:
        An IncomingMessage with the platform specifics flattened into the core's
        neutral shape. `author` is left empty here — Slack's event carries only a
        user id, and resolving it to a display name needs a separate API call we make
        ONLY when we are actually going to forward (see _process_event).

    Why:
        Keeping this normalization in the shell means the pure core never imports or
        sees a Slack type. `is_bot` folds the two ways Slack marks automation —
        `bot_id` set, or subtype == "bot_message" — into the single loop-prevention
        signal the core checks first (Orion delivers its OWN reports via an incoming
        webhook, which arrive flagged this way). `has_subtype` is any subtype at all,
        so message edits/deletes/joins are dropped by the core's second guard.
    """
    subtype = event.get("subtype")
    is_bot = bool(event.get("bot_id")) or subtype == "bot_message"
    return IncomingMessage(
        channel_id=event.get("channel", ""),
        is_bot=is_bot,
        has_subtype=subtype is not None,
        author="",  # resolved lazily on forward (Slack events carry only a user id)
        text=event.get("text", "") or "",
    )


def _resolve_author(client, user_id: str) -> str:
    """Resolve a Slack user id to a human display name, falling back to the id.

    Args:
        client: A Slack WebClient (Bolt injects it into the listener). Only its
            `users_info` method is used.
        user_id: The Slack user id from the event (e.g. "U07ABC123"), or "" if absent.

    Returns:
        The user's display name (or real name), else the raw user id if the lookup
        fails or returns nothing. Never raises.

    Why:
        A Slack message event names its author only by an opaque id; an unreadable
        "U07ABC123 said…" on the dashboard undermines the whole point of the feature,
        so we resolve a friendly name via one users.info call (needs the `users:read`
        scope). The lookup is cosmetic, so ANY failure (network, missing scope,
        unexpected shape) falls back to the id rather than dropping the comment — the
        broad except is deliberate and justified here (we do not import slack_sdk's
        error type, and a name lookup must never take the bot down or lose a reply).
    """
    if not user_id:
        return ""
    try:
        profile = client.users_info(user=user_id)["user"]["profile"]
        return profile.get("display_name") or profile.get("real_name") or user_id
    except Exception:  # noqa: BLE001 - cosmetic lookup must fail soft to the user id
        return user_id


def _process_event(
    event: dict,
    client,
    channel_map: dict[str, str],
    relay_url: str,
    relay_token: str,
    *,
    poster=post_comment,
    author_resolver=_resolve_author,
) -> IncomingMessage:
    """Decide on one Slack message event and, if warranted, relay it as a comment.

    Args:
        event: The Slack `message` event dict.
        client: The Slack WebClient (used only to resolve the author display name).
        channel_map: channel_id → project name, from the [bot] config.
        relay_url: The relay's configured URL (the [relay] table's url).
        relay_token: The relay's Bearer token.
        poster: The function that POSTs a comment to the relay. Injected so tests can
            substitute a fake; defaults to relay_client.post_comment.
        author_resolver: The function that turns a user id into a display name.
            Injected for the same reason; defaults to _resolve_author.

    Returns:
        The normalized IncomingMessage (mainly for tests/logging) — but the side
        effect (a relay POST on a forward) is the point.

    Why:
        This is the whole glue path, factored OUT of the Bolt listener so it is
        testable with a plain dict event and a fake client/poster — no slack-bolt and
        no live connection needed. The pure core makes the forward/drop decision; this
        function only performs the I/O the decision implies. The author is resolved
        AFTER the decision so the (rate-limited) users.info call happens only for
        messages we actually forward, never for the bot/edit/unconfigured traffic the
        core drops. A relay failure is caught and logged, never propagated — one bad
        POST (relay down, token wrong) must not crash the listener (fail-soft, the same
        policy the rest of Orion's delivery uses).
    """
    msg = _to_incoming(event)
    decision = decide_forward(msg, channel_map)
    if not decision.forward:
        return msg

    # Resolve the friendly author name only now that we know we will forward. Cap it
    # to the relay's author limit (the relay re-caps server-side as a safety net).
    author = author_resolver(client, event.get("user", ""))[:MAX_AUTHOR_CHARS]
    try:
        poster(relay_url, relay_token, decision.project, author, decision.body)
        print(
            f"[bot] relayed a reply to project {decision.project!r}", file=sys.stderr
        )
    except DeliveryError as exc:
        # Fail-soft: a single failed relay POST is reported and dropped, not raised.
        print(f"[bot] could not relay a reply: {exc}", file=sys.stderr)
    return msg


def run_bot(
    bot_token: str,
    app_token: str,
    channel_map: dict[str, str],
    relay_url: str,
    relay_token: str,
) -> None:
    """Run the Slack Socket Mode listener until interrupted (the blocking entry point).

    Args:
        bot_token: The Slack bot token (xoxb-…), authenticating the WebClient.
        app_token: The Slack app-level token (xapp-…) Socket Mode uses to open its
            outbound WebSocket.
        channel_map: channel_id → project name (the [bot] bindings as a dict).
        relay_url: The relay's configured URL (write target).
        relay_token: The relay's Bearer token.

    Returns:
        None. Blocks handling events until Ctrl-C, then returns cleanly.

    Raises:
        ConfigError: when slack-bolt is not installed — pointing the user at the
            optional extra. This is the one place the dependency is required, and it
            is imported lazily so every other code path is unaffected by its absence.

    Why:
        This is the thin adapter `orion bot` calls. It builds a Bolt App, registers a
        single `message` listener that hands each event to the tested _process_event,
        and starts Socket Mode (which blocks, owning its own background thread,
        heartbeat, and reconnect). Construction failures (a bad token) and Ctrl-C are
        the operator's concern; the per-message fail-soft lives in _process_event.
    """
    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError as exc:
        raise ConfigError(
            "the Slack bot needs the optional dependency `slack-bolt`. Install it "
            "with: pip install orion[slack-bot]"
        ) from exc

    app = App(token=bot_token)

    # One listener for plain channel messages. Bolt injects `event` (the payload) and
    # `client` (a WebClient) by parameter name; the real work is in _process_event.
    @app.event("message")
    def _on_message(event, client):  # noqa: ANN001 - Bolt-provided arguments
        _process_event(event, client, channel_map, relay_url, relay_token)

    print(
        f"[bot] starting Slack Socket Mode listener; watching {len(channel_map)} "
        f"channel(s)",
        file=sys.stderr,
    )
    handler = SocketModeHandler(app, app_token)
    try:
        # Blocks until interrupted; Bolt manages the WebSocket, heartbeat, reconnect.
        handler.start()
    except KeyboardInterrupt:
        # Ctrl-C is a clean stop, not an error — mirrors relay.serve()'s handling.
        print("[bot] shutting down.", file=sys.stderr)

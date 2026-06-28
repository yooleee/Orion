# =============================================================================
# bot/core.py
# -----------------------------------------------------------------------------
# Responsible for: The PURE decision logic of the chat bot — given a normalized
#                  inbound message and the channel→project map, decide whether to
#                  forward it to the relay as a comment, and with what fields.
# Role in project: This is to the bot what relay/api.py is to the relay server:
#                  all the branching logic, none of the I/O. It has NO network, NO
#                  asyncio, and NO slack-bolt import, so the whole threat-model and
#                  routing logic is unit-testable with plain dicts. The async Bolt
#                  shell (slack_bot.py) normalizes a platform event into an
#                  IncomingMessage, calls decide_forward, and acts on the result.
# Assumptions: The caller (the shell) is responsible for normalizing a
#              platform-specific event into IncomingMessage — this module never
#              sees a Slack/Discord type, which keeps it platform-neutral (the same
#              core serves Slack now and Discord later).
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass

# Per-field caps on a forwarded comment. These MIRROR relay/server.py's
# MAX_COMMENT_BODY_CHARS / MAX_AUTHOR_CHARS, but are duplicated here DELIBERATELY:
# orion/ and relay/ share no code by design (see relay/store.py's header — it
# duplicates _BUSY_TIMEOUT_SECONDS for the same reason), and the relay endpoint
# re-enforces these caps server-side anyway, so any drift is harmless (the bot is
# merely slightly stricter or laxer than the relay, never a correctness hole). We
# enforce them client-side too so an over-long message becomes a clean local drop
# instead of a wasted network round-trip that the relay would 400.
MAX_COMMENT_BODY_CHARS = 4_000
MAX_AUTHOR_CHARS = 200


@dataclass(frozen=True)
class IncomingMessage:
    """A chat message normalized off a platform event, for the pure core to judge.

    Args:
        channel_id: The platform's channel identifier as a STRING. Slack channel
            ids look like "C07ABC123" (not integers), and a string also keeps this
            type platform-neutral for a future Discord backend (whose ids are big
            ints — stringified at the shell boundary).
        is_bot: True when the message was authored by a bot or an incoming webhook
            (Slack: event has `bot_id` or subtype == "bot_message"). This is the
            loop-prevention signal — Orion delivers its OWN reports via webhook, so
            this flag is what stops the bot from treating a delivered report as a
            supervisor reply.
        has_subtype: True when the platform event carried any `subtype` (edits,
            deletes, joins, etc.). The smallest slice acts only on plain new
            messages, so any subtype is dropped.
        author: The supervisor's display name (or a stand-in id) — a free-text
            label, NOT an authenticated identity (that is C3). Capped on forward.
        text: The message body. Treated as an OPAQUE string — never parsed as a
            command (structure, when it comes, will be slash commands, not parsed
            free text).

    Why:
        Normalizing the platform event into this small frozen value at the shell
        boundary means the core never imports or sees a Slack/Discord type. The
        core's logic stays pure and trivially testable (construct one of these from
        a literal), and swapping or adding a platform is a shell-only change.
    """

    channel_id: str
    is_bot: bool
    has_subtype: bool
    author: str
    text: str


@dataclass(frozen=True)
class ForwardDecision:
    """The result of judging an IncomingMessage: forward it, or drop it (with why).

    Args:
        forward: True to relay this message as a comment; False to ignore it.
        reason: When dropped, a short machine/debug explanation (e.g. "bot author",
            "unconfigured channel"). Empty when forwarding. Used only for optional
            debug logging — never shown to a user.
        project: When forwarding, the project the comment attaches to (resolved from
            the channel→project map). Empty string when dropped.
        author: When forwarding, the capped author label to send. Empty when dropped
            (and legitimately empty when the message had no author).
        body: When forwarding, the stripped comment text to send. Empty when dropped.

    Why:
        One frozen result type with a boolean discriminator keeps decide_forward a
        pure function returning a value (no exceptions for the expected "just ignore
        this" case, which is the common path — most channel traffic is not a reply
        we forward). The drop `reason` makes the logic observable in tests and logs
        without changing behavior. The two constructors below make each call site
        read as either "drop because X" or "forward to project P".
    """

    forward: bool
    reason: str = ""
    project: str = ""
    author: str = ""
    body: str = ""

    @classmethod
    def drop(cls, reason: str) -> "ForwardDecision":
        """Build a 'do not forward' decision carrying a debug reason.

        Args:
            reason: Why the message is being ignored (for optional logging).

        Returns:
            A ForwardDecision with forward=False and the given reason.

        Why:
            Named constructor so the guard clauses in decide_forward read as
            `return ForwardDecision.drop("bot author")` — intent-revealing, and the
            empty forward fields are filled by the dataclass defaults in one place.
        """
        return cls(forward=False, reason=reason)

    @classmethod
    def to(cls, project: str, author: str, body: str) -> "ForwardDecision":
        """Build a 'forward this comment' decision with its destination and fields.

        Args:
            project: The project the comment attaches to.
            author: The (already capped) author label.
            body: The (already stripped) comment text.

        Returns:
            A ForwardDecision with forward=True and the comment fields set.

        Why:
            The positive counterpart to drop(): keeps the happy-path return in
            decide_forward a single readable line and guarantees the forward fields
            are always set together.
        """
        return cls(forward=True, project=project, author=author, body=body)


def decide_forward(
    msg: IncomingMessage, channel_project_map: dict[str, str]
) -> ForwardDecision:
    """Decide whether a chat message should be relayed as a report comment.

    Args:
        msg: The normalized inbound message (see IncomingMessage).
        channel_project_map: Map of channel id → project name, from the [bot]
            config. A message in a channel NOT in this map is ignored.

    Returns:
        A ForwardDecision: either forward (with project/author/body ready to POST)
        or drop (with a reason). This function performs NO I/O — the caller does the
        actual relay POST when forward is True.

    Why:
        This single pure function encodes the bot's entire inbound threat model as a
        linear sequence of guards (cheapest/most-decisive first), so the security
        rules are reviewable and testable in one place rather than scattered through
        an async event handler. The order matters: drop non-human and unconfigured
        traffic BEFORE looking at content, so the common, ignorable case is cheap and
        no untrusted text is processed unnecessarily.
    """
    # 1) Loop prevention + ignore other automations. Orion delivers reports via an
    # incoming webhook, which arrives flagged as a bot/webhook author — without this
    # the bot would treat its own delivered report as a supervisor comment.
    if msg.is_bot:
        return ForwardDecision.drop("bot or webhook author")

    # 2) Ignore message edits/deletes/joins/etc. The smallest slice forwards only
    # plain new messages; any event subtype is out of scope.
    if msg.has_subtype:
        return ForwardDecision.drop("message has a subtype")

    # 3) Only act on channels explicitly mapped to a project. This is the core
    # access control: an unconfigured channel (a DM, an unrelated channel the bot
    # happens to see) is never forwarded.
    project = channel_project_map.get(msg.channel_id)
    if project is None:
        return ForwardDecision.drop("unconfigured channel")

    # 4) A comment must carry text. Strip first so a whitespace-only message (or an
    # attachment-only post with empty text) counts as empty — mirrors the relay's own
    # body validation.
    body = msg.text.strip()
    if not body:
        return ForwardDecision.drop("empty message body")

    # 5) Enforce the body cap locally so an over-long message is a clean drop here
    # rather than a round-trip the relay rejects with a 400. (The relay re-checks.)
    if len(body) > MAX_COMMENT_BODY_CHARS:
        return ForwardDecision.drop("message body over the length cap")

    # Forward. The author is a free-text label, capped to the relay's limit so an
    # unusually long display name can't trip the endpoint's author cap.
    return ForwardDecision.to(project, msg.author[:MAX_AUTHOR_CHARS], body)

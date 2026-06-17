# =============================================================================
# compose.py
# -----------------------------------------------------------------------------
# Responsible for: Turning a ReportBlob into the final, channel-ready message
#                  text that gets previewed and sent.
# Role in project: The last formatting step before delivery. Phase 1 emits plain
#                  Markdown, which renders fine in Discord. The `channel` argument
#                  exists now so Phase 3 (Slack) is a new branch here, not a
#                  rewrite of the pipeline.
# =============================================================================

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from orion.report import ReportBlob

# Per-platform text ceilings, enforced HERE (compose owns rendering; delivery is
# pure transport that POSTs whatever payload it is handed). Discord rejects
# `content` over 2000 chars; Slack's practical `text` ceiling is ~40k. We truncate
# with a visible marker rather than let the API reject an over-long (already
# redacted) report. Splitting across messages is a possible future improvement
# (see docs/known-issues.md KI-2); truncation is the simplest correct behavior.
_DISCORD_CONTENT_LIMIT = 2000
_SLACK_TEXT_LIMIT = 40000
_TRUNCATION_MARKER = "\n… [truncated]"

# --- Discord embed limits ---
# Discord rejects the WHOLE message if an embed exceeds any single one of these,
# so we check them up front and fall back to a plain message if a report is too
# big (see _discord_embed_fits). A short label (title / field name) is hard-capped
# with _cap; the per-field VALUE limit is what a long section can blow past.
_DISCORD_TITLE_LIMIT = 256
_DISCORD_DESC_LIMIT = 4096
_DISCORD_FIELD_NAME_LIMIT = 256
_DISCORD_FIELD_VALUE_LIMIT = 1024
_DISCORD_MAX_FIELDS = 25
_DISCORD_EMBED_TOTAL_LIMIT = 6000
# Cosmetic accent stripe on the embed card (Discord "blurple").
_DISCORD_EMBED_COLOR = 0x5865F2

# --- Slack Block Kit limits ---
# Slack rejects a message with more than 50 blocks or a section whose text runs
# past 3000 chars; the header block is plain_text and caps at 150.
_SLACK_HEADER_LIMIT = 150
_SLACK_SECTION_TEXT_LIMIT = 3000
_SLACK_MAX_BLOCKS = 50


@dataclass(frozen=True)
class ComposedMessage:
    """A channel-ready message: the JSON body to POST plus a faithful preview.

    Args:
        payload: The exact JSON body delivery will POST to the channel's webhook
            (today: {"content": …} for Discord, {"text": …} for Slack). Delivery
            does not reshape it — what is here is what is sent.
        preview: A human-readable text rendering of that payload, shown at the
            preview-before-send gate. It is derived from the same payload so the
            human sees exactly the text that will leave the machine.

    Why:
        B3 moves Orion off the "a message is one string" model toward structured
        payloads (Discord embeds / Slack Block Kit). Bundling the payload and its
        preview in one object means the bytes that get sent and the bytes the human
        approves can never drift apart per channel — the preview-before-send
        guarantee stays trustworthy. In this checkpoint the payload is still the
        plain {"content"/"text": str} shape, so nothing on the wire changes yet;
        the richer renderers slot in behind this same return type next.
    """

    payload: dict
    preview: str


def compose(blob: ReportBlob, channel: str) -> ComposedMessage:
    """Format a ReportBlob into a channel-ready payload + preview.

    Args:
        blob: The report to render.
        channel: The destination channel ("discord" or "slack").

    Returns:
        A ComposedMessage carrying the JSON payload to POST and a faithful text
        preview of it.

    Why:
        A single entry point per channel keeps formatting decisions in one place.
        Each channel gets its richer native rendering — a Discord embed, a Slack
        Block Kit message — built from the blob's per-signal sections. If a report
        is too big for that channel's structured limits we fall back to the plain
        message shape (see the per-channel builders). The preview is rendered from
        whichever payload we end up with, so it is faithful by construction.
    """
    if channel == "slack":
        payload = _slack_payload(blob)
    else:
        # Discord, and the defensive default for an unknown channel (config
        # validation restricts `channel` upstream, so this degrades gracefully).
        payload = _discord_payload(blob)
    return ComposedMessage(payload=payload, preview=_render_preview(payload))


def _sections_for(blob: ReportBlob) -> list[tuple[str, str]]:
    """The (title, body) sections to render for a blob.

    Args:
        blob: The report being composed.

    Returns:
        The blob's per-signal sections, or a single ("Update", body) section when
        it has none.

    Why:
        A `report` carries one section per enabled signal; an `intake` push has
        only a single body and no sections. Falling back to one "Update" section
        lets both render through the same per-section code path, so the structured
        renderers never need a special case.
    """
    if blob.sections:
        return list(blob.sections)
    return [("Update", blob.body)]


def _discord_payload(blob: ReportBlob) -> dict:
    """Build a Discord embed payload, falling back to plain content if it won't fit.

    Args:
        blob: The report to render.

    Returns:
        {"embeds": [embed]} when the embed satisfies Discord's size limits, else
        {"content": …} with the plain Markdown message (truncated).

    Why:
        An embed — a titled, colored card with one field per signal — is the richer
        Discord rendering. Discord caps embed sizes and rejects the whole message
        if any cap is exceeded, so for an oversized report we degrade to today's
        plain message rather than send an invalid payload: a complete report beats
        a rejected one. We do NOT also set `content` alongside `embeds` (Discord
        would render both, duplicating the report); the embed stands alone.
    """
    embed = {
        "title": _cap(f"Progress update — {blob.project}", _DISCORD_TITLE_LIMIT),
        # The friendly date sits under the title; italic via Markdown in the embed.
        "description": _cap(f"_{_format_timestamp(blob.generated_at)}_", _DISCORD_DESC_LIMIT),
        "color": _DISCORD_EMBED_COLOR,
        "fields": [
            {"name": _cap(title, _DISCORD_FIELD_NAME_LIMIT), "value": body, "inline": False}
            for title, body in _sections_for(blob)
        ],
    }
    if _discord_embed_fits(embed):
        return {"embeds": [embed]}
    return {"content": _truncate(_format_markdown(blob), _DISCORD_CONTENT_LIMIT)}


def _discord_embed_fits(embed: dict) -> bool:
    """Whether an embed satisfies every Discord size limit.

    Args:
        embed: The embed dict built by _discord_payload.

    Returns:
        True only if the embed is within all of Discord's caps.

    Why:
        Discord rejects the entire message if any single limit is exceeded, so we
        check up front and let the caller choose the plain fallback. The per-field
        VALUE cap (1024) is the one a long section typically blows past; the title
        and field names are already hard-capped, so they can't.
    """
    fields = embed["fields"]
    if len(fields) > _DISCORD_MAX_FIELDS:
        return False
    if any(len(f["value"]) > _DISCORD_FIELD_VALUE_LIMIT for f in fields):
        return False
    if len(embed["description"]) > _DISCORD_DESC_LIMIT:
        return False
    total = (
        len(embed["title"])
        + len(embed["description"])
        + sum(len(f["name"]) + len(f["value"]) for f in fields)
    )
    return total <= _DISCORD_EMBED_TOTAL_LIMIT


def _slack_payload(blob: ReportBlob) -> dict:
    """Build a Slack Block Kit payload, falling back to plain text if it won't fit.

    Args:
        blob: The report to render.

    Returns:
        {"blocks": [...], "text": fallback} when the blocks satisfy Slack's limits,
        else {"text": …} with the plain mrkdwn message (truncated).

    Why:
        Block Kit — a header block, a context line for the date, and one section
        block per signal — is the richer Slack rendering. We ALWAYS include a
        `text` field: Slack uses it for notifications and accessibility, and it is
        NOT shown in place of the blocks, so it is a true fallback (not a
        duplicate). If the report exceeds Block Kit limits we send that plain text
        on its own instead.
    """
    fallback = _truncate(_format_slack(blob), _SLACK_TEXT_LIMIT)
    blocks: list[dict] = [
        {
            "type": "header",
            # A header block is plain_text (no mrkdwn); Slack renders it large/bold
            # natively, so it carries no asterisks.
            "text": {
                "type": "plain_text",
                "text": _cap(f"Progress update — {blob.project}", _SLACK_HEADER_LIMIT),
            },
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"_{_format_timestamp(blob.generated_at)}_"}
            ],
        },
    ]
    for index, (title, body) in enumerate(_sections_for(blob)):
        # A divider between sections (not before the first) gives a clean break.
        if index > 0:
            blocks.append({"type": "divider"})
        # Section body is translated to mrkdwn (its `**bold**` -> `*bold*`); the
        # title becomes a bold mrkdwn line above it.
        section_text = f"*{title}*\n{_to_slack_mrkdwn(body)}"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": section_text}})

    if _slack_blocks_fit(blocks):
        return {"blocks": blocks, "text": fallback}
    return {"text": fallback}


def _slack_blocks_fit(blocks: list[dict]) -> bool:
    """Whether a Slack block list satisfies Block Kit's limits.

    Args:
        blocks: The block list built by _slack_payload.

    Returns:
        True only if within Slack's caps (≤50 blocks, ≤3000 chars per section).

    Why:
        Slack rejects the message if either cap is exceeded; checking up front lets
        the caller fall back to plain text instead of getting an API error.
    """
    if len(blocks) > _SLACK_MAX_BLOCKS:
        return False
    return all(
        len(b["text"]["text"]) <= _SLACK_SECTION_TEXT_LIMIT
        for b in blocks
        if b.get("type") == "section"
    )


def _cap(text: str, limit: int) -> str:
    """Hard-cap a short label (title / field name / header) to a limit.

    Args:
        text: The label text.
        limit: The maximum allowed length.

    Returns:
        The text unchanged if within the limit, else cut to fit with an ellipsis.

    Why:
        Titles, field names, and the Slack header are short labels with small
        limits; unlike a message body, a plain cut with a single ellipsis is
        enough (no need for _truncate's multi-line marker).
    """
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _render_preview(payload: dict) -> str:
    """Render a faithful, readable text preview from a delivery payload.

    Args:
        payload: The exact JSON payload that will be POSTed (embed, blocks, or a
            plain content/text fallback).

    Returns:
        A readable text rendering built from the payload's own text fields.

    Why:
        The payload is now JSON, but preview-before-send must still show the human
        exactly the TEXT that will leave the machine, so the human can catch a
        leak. Building the preview by reading the payload's own fields means it can
        never diverge from what is sent. Only whitespace is added for readability —
        no text content the payload doesn't contain. Handles both the rich shapes
        and the plain fallbacks.
    """
    if "embeds" in payload:
        lines: list[str] = []
        for embed in payload["embeds"]:
            if embed.get("title"):
                lines.append(embed["title"])
            if embed.get("description"):
                lines.append(embed["description"])
            for field in embed.get("fields", []):
                lines.append("")  # blank separator (presentation only)
                lines.append(field["name"])
                lines.append(field["value"])
        return "\n".join(lines)
    if "blocks" in payload:
        lines = []
        for block in payload["blocks"]:
            kind = block.get("type")
            if kind in ("header", "section"):
                lines.append(block["text"]["text"])
            elif kind == "context":
                lines.extend(el.get("text", "") for el in block.get("elements", []))
            elif kind == "divider":
                lines.append("—")
        return "\n".join(lines)
    # Plain fallback payloads.
    return payload.get("content") or payload.get("text") or ""


def _truncate(message: str, limit: int) -> str:
    """Shorten a message to fit a channel's text limit, with a visible marker.

    Args:
        message: The full rendered message text.
        limit: The channel's maximum allowed length for that text field.

    Returns:
        The message unchanged if within the limit, otherwise cut to fit with a
        truncation marker appended.

    Why:
        One shared helper (parameterized by limit) keeps Discord's and Slack's
        truncation identical and testable, and reserves room for the marker so the
        result never itself exceeds the limit. The body is already redacted, so
        truncation is safe — just lossy.
    """
    if len(message) <= limit:
        return message
    keep = limit - len(_TRUNCATION_MARKER)
    return message[:keep] + _TRUNCATION_MARKER


def _format_markdown(blob: ReportBlob) -> str:
    """Render a ReportBlob as a plain Markdown progress update.

    Args:
        blob: The report to render.

    Returns:
        A Markdown string: a header line, the date, then the body.

    Why:
        A small, predictable header gives the supervisor context (which project,
        when) without exposing internals. The body is already redacted and
        audience-ready, so it is included verbatim.
    """
    header = f"**Progress update — {blob.project}**"
    date_line = f"_{_format_timestamp(blob.generated_at)}_"
    return f"{header}\n{date_line}\n\n{blob.body}"


def _format_slack(blob: ReportBlob) -> str:
    """Render a ReportBlob as a Slack mrkdwn progress update.

    Args:
        blob: The report to render.

    Returns:
        A Slack-mrkdwn string: a bold header line, an italic date line, then the
        body with its Markdown translated to Slack's dialect.

    Why:
        Slack's mrkdwn is not Discord's Markdown: bold is a single asterisk
        (`*b*`, not `**b**`) and there are no `#`/`##` headers — Slack would show
        those characters literally. The header/date here are built directly in
        Slack form, and the body (which carries `## ` section titles from merge
        and possibly `**bold**` from the LLM) is run through the translator so it
        renders, rather than leaking raw `#`/`**` into the channel. The date line
        uses single underscores, which mean italic in BOTH dialects, so it needs
        no translation.
    """
    header = f"*Progress update — {blob.project}*"
    date_line = f"_{_format_timestamp(blob.generated_at)}_"
    body = _to_slack_mrkdwn(blob.body)
    return f"{header}\n{date_line}\n\n{body}"


# A Markdown ATX header line: 1–6 leading "#" then the title text. Matched per
# line (MULTILINE) so each section header in the body becomes a Slack bold line.
_MD_HEADER_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
# Markdown bold: a **double-asterisk** span. Non-greedy so adjacent bolds on one
# line don't merge into a single match.
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _to_slack_mrkdwn(text: str) -> str:
    """Translate the structural Markdown Orion emits into Slack mrkdwn.

    Args:
        text: The report body in Discord-flavored Markdown.

    Returns:
        The body with `# / ## …` header lines turned into Slack bold lines and
        `**bold**` spans turned into Slack `*bold*`.

    Why:
        This is deliberately NOT a general Markdown→mrkdwn converter — it handles
        exactly the two constructs Orion produces: the `## ` section titles that
        merge.py emits, and the `**bold**` the LLM summary may contain (see
        docs/known-issues.md KI for the scope limit). Headers are converted first
        (a line-level rewrite), then inline bold; the order is safe because a
        converted header becomes single-asterisk bold, which the double-asterisk
        bold pattern no longer matches. Bullets (`- item`) are left as-is — Slack
        renders them acceptably and there is no value in rewriting them.
    """
    # Header lines first: "## Code activity" -> "*Code activity*".
    converted = _MD_HEADER_RE.sub(r"*\1*", text)
    # Then inline bold: "**done**" -> "*done*".
    converted = _MD_BOLD_RE.sub(r"*\1*", converted)
    return converted


def _format_timestamp(iso_timestamp: str) -> str:
    """Render a canonical ISO 8601 timestamp as a human-friendly date line.

    Args:
        iso_timestamp: An ISO 8601 string as stored on the ReportBlob, e.g.
            "2026-06-15T01:32:53+00:00".

    Returns:
        A friendly string like "June 15, 2026 · 1:32 AM UTC". If the input cannot
        be parsed, the original string is returned unchanged.

    Why:
        The blob keeps the timestamp canonical (sortable, portable, what the state
        DB stores); presentation belongs here in compose. A human supervisor reads
        "June 15, 2026 · 1:32 AM UTC" far more easily than the raw ISO form. We
        build the 12-hour clock from components rather than using strftime's
        no-leading-zero flags (%-I on Linux/Mac vs %#I on Windows) so the output is
        identical on every platform — this is open-source and runs anywhere. The
        try/except is a deliberate safety net: a malformed timestamp must never
        turn a formatting detail into a failed report on the pre-send path.
    """
    try:
        dt = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        # Not a parseable ISO timestamp — degrade gracefully to the raw value
        # rather than raising in the delivery path.
        return iso_timestamp

    # Date part: full month name, no-leading-zero day, full year — e.g. "June 15, 2026".
    date_part = f"{dt.strftime('%B')} {dt.day}, {dt.year}"

    # 12-hour time built explicitly (portable across platforms):
    #   hour % 12 maps 0->12 and 13->1, etc.; `or 12` turns a 0 result into 12
    #   so both midnight and noon read as "12".
    hour_12 = dt.hour % 12 or 12
    meridiem = "AM" if dt.hour < 12 else "PM"
    time_part = f"{hour_12}:{dt.minute:02d} {meridiem}"  # minute zero-padded to 2 digits

    # Timezone label: Phase 1 timestamps are UTC; %Z yields "UTC" for a UTC-aware
    # datetime. Fall back to a literal "UTC" if the platform returns an empty name.
    tz_part = dt.strftime("%Z") or "UTC"

    return f"{date_part} · {time_part} {tz_part}"

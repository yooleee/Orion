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
from datetime import datetime

from orion.report import ReportBlob


def compose(blob: ReportBlob, channel: str) -> str:
    """Format a ReportBlob into message text for the target channel.

    Args:
        blob: The report to render.
        channel: The destination channel ("discord" or "slack").

    Returns:
        The composed message string in that channel's flavor of Markdown.

    Why:
        A single entry point per channel keeps formatting decisions in one place.
        Discord and Slack both take a single text field but use *different*
        Markdown dialects (Discord renders `## h` and `**b**`; Slack renders
        `*b*` for bold and ignores `##`), so each gets its own branch keyed on
        `channel`. The signature and the caller stay unchanged — the caller just
        passes each recipient's channel.
    """
    if channel == "discord":
        return _format_markdown(blob)
    if channel == "slack":
        return _format_slack(blob)
    # Defensive default: unknown channels still get a sane message rather than an
    # error, since config validation already restricts `channel` upstream.
    return _format_markdown(blob)


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

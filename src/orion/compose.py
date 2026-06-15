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

from datetime import datetime

from orion.report import ReportBlob


def compose(blob: ReportBlob, channel: str) -> str:
    """Format a ReportBlob into message text for the target channel.

    Args:
        blob: The report to render.
        channel: The destination channel ("discord" in Phase 1).

    Returns:
        The composed message string.

    Why:
        A single entry point per channel keeps formatting decisions in one place.
        For now every supported channel uses the same Markdown rendering (Discord
        accepts Markdown); when Slack arrives it gets its own branch keyed on
        `channel`, leaving this signature and the caller unchanged.
    """
    # Phase 1: Discord is the only channel, and Markdown renders natively there.
    # The branch is explicit so the seam is visible even with one case.
    if channel == "discord":
        return _format_markdown(blob)
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

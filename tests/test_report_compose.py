# =============================================================================
# tests/test_report_compose.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying ReportBlob assembly and Markdown composition.
# Role in project: These are the lane-agnostic glue between the pipeline and
#                  delivery; they must carry participants/metadata faithfully and
#                  render the body without dropping it.
# =============================================================================

from pathlib import Path

from orion import __version__
from orion.compose import (
    _DISCORD_CONTENT_LIMIT,
    _SLACK_TEXT_LIMIT,
    ComposedMessage,
    compose,
)
from orion.config import ProjectConfig, Recipient
from orion.report import build_report

# The display time zone compose() now requires (KI-20). The CLI passes
# config.display_timezone; these tests pass the same default explicitly so the
# structural assertions are unaffected and the timestamp tests pin the Pacific
# rendering. A dedicated test below exercises a non-default ("UTC") override.
_TZ = "America/Los_Angeles"


def _project():
    """A small ProjectConfig with two recipients for assembly tests.

    Why:
        Two recipients verifies that participants are collected in order and that
        the blob doesn't assume a single "me".
    """
    return ProjectConfig(
        name="demo",
        repo_path=Path("/tmp/demo"),
        share_level="high_level",
        collectors=("git",),
        recipients=(
            Recipient(name="Alex", channel="discord", webhook_env_var="ORION_W_ALEX"),
            Recipient(name="Sam", channel="discord", webhook_env_var="ORION_W_SAM"),
        ),
    )


def test_build_report_populates_metadata():
    """build_report carries project, participants, marker, and version.

    Why this matters: this metadata is what makes the blob portable to a future
    shared service and what couples the report to the state marker that gets
    advanced after send.
    """
    blob = build_report(
        _project(),
        body="Shipped the collector.",
        lane="raw",
        generated_at="2026-06-14T00:00:00+00:00",
    )
    assert blob.project == "demo"
    assert blob.participants == ("Alex", "Sam")
    assert blob.lane == "raw"
    assert blob.body == "Shipped the collector."
    assert blob.orion_version == __version__


def test_build_report_defaults_sections_to_empty():
    """A build_report call without sections leaves blob.sections empty.

    Why this matters: `intake` and every pre-B3 call site builds a report from a
    single body with no per-signal sections. The field must default to an empty
    tuple so those callers keep working unchanged and compose can treat "no
    sections" as "render the flat body as one section."
    """
    blob = build_report(
        _project(),
        body="A pushed update.",
        lane="structured",
        generated_at="2026-06-14T00:00:00+00:00",
    )
    assert blob.sections == ()


def test_build_report_carries_sections_in_order():
    """build_report carries the (title, body) sections it is given, in order.

    Why this matters: B3's richer rendering builds Slack Block Kit / Discord
    embeds from these sections, so they must survive assembly verbatim and in
    display order rather than being flattened away into the body string.
    """
    sections = (("Code activity", "Shipped the seam."), ("Completed tasks", "Did X."))
    blob = build_report(
        _project(),
        body="## Code activity\nShipped the seam.\n\n## Completed tasks\nDid X.",
        lane="raw",
        generated_at="2026-06-14T00:00:00+00:00",
        sections=sections,
    )
    assert blob.sections == sections


def test_compose_includes_project_and_body():
    """The composed Discord message contains the project name and the body.

    Why this matters: the supervisor needs to know which project the update is for
    and, obviously, the update itself — dropping either makes the message useless.
    """
    blob = build_report(
        _project(),
        body="Wired the pipeline end to end.",
        lane="raw",
        generated_at="2026-06-14T00:00:00+00:00",
    )
    message = compose(blob, "discord", _TZ).preview
    assert "demo" in message
    assert "Wired the pipeline end to end." in message


def test_compose_renders_friendly_timestamp_in_display_zone():
    """The date line is the human-friendly form, rendered in the configured zone.

    Why this matters: the supervisor reads this header; "June 14, 2026 · 6:32 PM
    PDT" is far clearer than "2026-06-15T01:32:53+00:00". With the default Pacific
    zone (KI-20), the stored UTC instant 01:32 is shown as the previous evening in
    California (DST → PDT), so this pins BOTH the zone conversion and the portable
    12-hour formatting (no leading zero on the hour). It also guards against a
    regression back to raw ISO — or back to UTC, the pre-KI-20 behavior.
    """
    blob = build_report(
        _project(),
        body="Body.",
        lane="raw",
        generated_at="2026-06-15T01:32:53+00:00",
    )
    message = compose(blob, "discord", _TZ).preview
    # 01:32 UTC on the 15th is 18:32 on the 14th in Pacific (PDT, UTC-7 in June).
    assert "June 14, 2026" in message            # date rolled back into Pacific
    assert "6:32 PM PDT" in message              # 12-hour, no leading zero, PDT label
    assert "UTC" not in message                  # no longer the UTC rendering
    assert "2026-06-15T01:32:53" not in message  # raw ISO must NOT appear


def test_compose_timestamp_honors_a_non_default_timezone():
    """A non-default display_timezone ("UTC") renders that zone's wall-clock + label.

    Why this matters: KI-20 made the display zone configurable, defaulting to Pacific
    to match the dashboard. A user with non-Pacific recipients can set "UTC" (or any
    IANA zone); this pins that the formatter actually honors the passed zone rather
    than hardcoding one — the same 01:32 UTC instant now reads as "1:32 AM UTC".
    """
    blob = build_report(
        _project(),
        body="Body.",
        lane="raw",
        generated_at="2026-06-15T01:32:53+00:00",
    )
    message = compose(blob, "discord", "UTC").preview
    assert "June 15, 2026" in message  # no zone shift in UTC
    assert "1:32 AM UTC" in message     # the explicit UTC override


def test_compose_malformed_timestamp_degrades_gracefully():
    """An unparseable timestamp falls back to the raw value instead of raising.

    Why this matters: compose runs in the pre-send path; a formatting error must
    never abort a report. A non-ISO string should pass through untouched.
    """
    blob = build_report(
        _project(),
        body="Body.",
        lane="raw",
        generated_at="not-a-timestamp",
    )
    message = compose(blob, "discord", _TZ).preview
    assert "not-a-timestamp" in message  # degraded gracefully, no exception


def _blob(body, sections=()):
    """Build a blob with the given body (and optional sections) for rendering tests.

    Why: B3 renders from the blob's per-signal `sections`; passing them lets a test
    exercise multi-section embeds/blocks. Omitting them (the intake case) leaves the
    flat `body` to be rendered as one section.
    """
    return build_report(
        _project(),
        body=body,
        lane="structured",
        generated_at="2026-06-15T01:32:53+00:00",
        sections=sections,
    )


def test_compose_returns_rich_payload_per_channel():
    """compose returns an embed for Discord and Block Kit + text fallback for Slack.

    Why this matters: B3's whole point — each channel gets its native structured
    payload, not a plain string. The preview, built FROM that payload, still
    surfaces the project and body so the human sees what will be sent.
    """
    discord = compose(_blob("Body."), "discord", _TZ)
    slack = compose(_blob("Body."), "slack", _TZ)
    assert isinstance(discord, ComposedMessage)
    assert "embeds" in discord.payload
    assert "blocks" in slack.payload and "text" in slack.payload  # blocks + fallback
    assert "demo" in discord.preview and "Body." in discord.preview
    assert "demo" in slack.preview and "Body." in slack.preview


def test_discord_payload_has_one_field_per_section():
    """A Discord embed carries one field per signal section, no plain `content`.

    Why this matters: the structured look is one titled field per signal; and we
    must NOT also send `content` (Discord would render both, duplicating the
    report).
    """
    composed = compose(
        _blob("ignored", sections=(("Code activity", "Did X."), ("Completed tasks", "Did Y."))),
        "discord",
        _TZ,
    )
    embed = composed.payload["embeds"][0]
    assert [f["name"] for f in embed["fields"]] == ["Code activity", "Completed tasks"]
    assert embed["title"] == "Progress update — demo"
    assert "content" not in composed.payload


def test_discord_embed_keeps_markdown_in_field_values():
    """A section body's `**bold**` is preserved in the embed field value.

    Why this matters: Discord renders Markdown inside embed field values, so the
    body's bold must survive verbatim (unlike Slack, which needs translation).
    """
    composed = compose(
        _blob("ignored", sections=(("Code activity", "Shipped the **structured lane**."),)),
        "discord",
        _TZ,
    )
    field = composed.payload["embeds"][0]["fields"][0]
    assert field["name"] == "Code activity"
    assert "**structured lane**" in field["value"]


def test_slack_payload_has_header_context_and_section_blocks():
    """Slack Block Kit has a header, a context date line, and a section per signal.

    Why this matters: this is the structured Slack rendering — a header block for
    the project, a context line for the date, and one section block per signal with
    a divider between them; plus the `text` notification fallback.
    """
    composed = compose(
        _blob("ignored", sections=(("Code activity", "Did X."), ("Completed tasks", "Did Y."))),
        "slack",
        _TZ,
    )
    types = [b["type"] for b in composed.payload["blocks"]]
    assert types[0] == "header"          # project header (plain_text)
    assert "context" in types            # the date line
    assert types.count("section") == 2   # one section per signal
    assert "divider" in types            # separating the two sections
    assert "text" in composed.payload    # notification fallback


def test_slack_section_title_is_single_asterisk_bold():
    """A Slack section title is `*bold*` (single asterisk), not Discord's `**`.

    Why this matters: Slack mrkdwn bold is one asterisk; the section title must be
    in Slack's dialect, and the header block (plain_text) carries no asterisks.
    """
    composed = compose(_blob("Body.", sections=(("Code activity", "Body."),)), "slack", _TZ)
    assert "Progress update — demo" in composed.preview  # header (plain_text)
    assert "*Code activity*" in composed.preview
    assert "**Code activity**" not in composed.preview
    assert "Body." in composed.preview


def test_slack_translates_inline_bold_in_section_body():
    """`**bold**` in a section body becomes `*bold*` for Slack.

    Why this matters: the LLM summary may contain `**bold**`; left untranslated it
    would show the asterisks literally in Slack, so the body is run through the
    mrkdwn translator before going into a section block.
    """
    composed = compose(
        _blob("ignored", sections=(("Code activity", "Shipped the **structured lane**."),)),
        "slack",
        _TZ,
    )
    assert "*structured lane*" in composed.preview
    assert "**structured lane**" not in composed.preview


def test_intake_blob_renders_single_update_section():
    """A blob with no sections renders its flat body as one "Update" section.

    Why this matters: `intake` pushes a single body and carries no sections; it
    must still render richly — one section/field labeled "Update" — through the same
    path as a multi-section report.
    """
    composed = compose(_blob("A pushed update."), "discord", _TZ)
    fields = composed.payload["embeds"][0]["fields"]
    assert [f["name"] for f in fields] == ["Update"]
    assert fields[0]["value"] == "A pushed update."


def test_discord_overflow_falls_back_to_plain_content():
    """A section too big for an embed falls back to a plain, truncated `content`.

    Why this matters: Discord rejects an oversized embed; rather than fail, compose
    degrades to today's plain message (truncated to the limit) — a complete report
    beats a rejected one. The preview reflects whatever was actually built.
    """
    composed = compose(_blob("A" * 5000), "discord", _TZ)
    assert "embeds" not in composed.payload                     # too big -> fallback
    assert len(composed.payload["content"]) <= _DISCORD_CONTENT_LIMIT
    assert composed.preview == composed.payload["content"]


def test_slack_overflow_falls_back_to_plain_text():
    """A section too big for Block Kit falls back to a plain, truncated `text`.

    Why this matters: same graceful degradation for Slack's per-section limit — the
    POSTed `text` must fit, and the preview must equal what is sent.
    """
    composed = compose(_blob("A" * (_SLACK_TEXT_LIMIT + 1000)), "slack", _TZ)
    assert "blocks" not in composed.payload                     # too big -> fallback
    assert len(composed.payload["text"]) <= _SLACK_TEXT_LIMIT
    assert composed.preview == composed.payload["text"]

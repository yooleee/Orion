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
from orion.compose import compose
from orion.config import ProjectConfig, Recipient
from orion.report import build_report


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
        source_marker="abc123",
        generated_at="2026-06-14T00:00:00+00:00",
    )
    assert blob.project == "demo"
    assert blob.participants == ("Alex", "Sam")
    assert blob.lane == "raw"
    assert blob.source_marker == "abc123"
    assert blob.body == "Shipped the collector."
    assert blob.orion_version == __version__


def test_compose_includes_project_and_body():
    """The composed Discord message contains the project name and the body.

    Why this matters: the supervisor needs to know which project the update is for
    and, obviously, the update itself — dropping either makes the message useless.
    """
    blob = build_report(
        _project(),
        body="Wired the pipeline end to end.",
        lane="raw",
        source_marker="abc123",
        generated_at="2026-06-14T00:00:00+00:00",
    )
    message = compose(blob, "discord")
    assert "demo" in message
    assert "Wired the pipeline end to end." in message


def test_compose_renders_friendly_timestamp():
    """The date line is the human-friendly form, not the raw ISO string.

    Why this matters: the supervisor reads this header; "June 15, 2026 · 1:32 AM
    UTC" is far clearer than "2026-06-15T01:32:53+00:00". This also pins the
    portable 12-hour formatting (no leading zero on the hour, UTC label) so a
    refactor can't silently regress the rendering back to raw ISO.
    """
    blob = build_report(
        _project(),
        body="Body.",
        lane="raw",
        source_marker="abc123",
        generated_at="2026-06-15T01:32:53+00:00",
    )
    message = compose(blob, "discord")
    assert "June 15, 2026" in message            # spelled-out date
    assert "1:32 AM UTC" in message              # 12-hour, no leading zero, UTC label
    assert "2026-06-15T01:32:53" not in message  # raw ISO must NOT appear


def test_compose_malformed_timestamp_degrades_gracefully():
    """An unparseable timestamp falls back to the raw value instead of raising.

    Why this matters: compose runs in the pre-send path; a formatting error must
    never abort a report. A non-ISO string should pass through untouched.
    """
    blob = build_report(
        _project(),
        body="Body.",
        lane="raw",
        source_marker="abc123",
        generated_at="not-a-timestamp",
    )
    message = compose(blob, "discord")
    assert "not-a-timestamp" in message  # degraded gracefully, no exception


def _blob(body):
    """Build a blob with the given body for channel-rendering tests."""
    return build_report(
        _project(),
        body=body,
        lane="structured",
        source_marker="",
        generated_at="2026-06-15T01:32:53+00:00",
    )


def test_slack_header_uses_single_asterisk_bold():
    """The Slack message's header is *bold* (single asterisk), not **bold**.

    Why this matters: Slack mrkdwn bold is one asterisk; Discord's `**…**` would
    render literally in Slack, so the header must be in Slack's dialect.
    """
    message = compose(_blob("Body."), "slack")
    assert "*Progress update — demo*" in message
    assert "**Progress update" not in message  # not the Discord double-asterisk
    assert "Body." in message


def test_slack_translates_section_headers_to_bold_lines():
    """`## Section` titles become `*Section*` and no `##` survives in Slack.

    Why this matters: merge.py emits `## ` section titles for the report body;
    Slack renders `##` literally, so they must be converted to bold lines.
    """
    message = compose(_blob("## Code activity\nDid the work."), "slack")
    assert "*Code activity*" in message
    assert "##" not in message  # the literal header marker must not leak through


def test_slack_translates_inline_bold():
    """`**bold**` in the body becomes `*bold*` for Slack.

    Why this matters: the LLM summary may contain `**bold**`; left untranslated it
    would show the asterisks literally in Slack.
    """
    message = compose(_blob("Shipped the **structured lane** today."), "slack")
    assert "*structured lane*" in message
    assert "**structured lane**" not in message


def test_discord_rendering_is_unchanged_by_slack_support():
    """Discord still gets `##` headers and `**bold**` — no cross-contamination.

    Why this matters: adding the Slack branch must not alter Discord output; the
    two dialects are kept fully separate.
    """
    body = "## Code activity\nShipped the **structured lane**."
    message = compose(_blob(body), "discord")
    assert "## Code activity" in message       # Discord renders ATX headers
    assert "**structured lane**" in message     # Discord double-asterisk bold
    assert "**Progress update — demo**" in message

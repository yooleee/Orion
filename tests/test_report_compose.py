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

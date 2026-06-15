# =============================================================================
# tests/test_intake.py
# -----------------------------------------------------------------------------
# Responsible for: End-to-end verification of the `orion intake` command — the
#                  pushed/hand-written update path (the Phase-6 skill's entry point).
# Role in project: Intake is the structured lane in its purest form: body in,
#                  redact -> preview -> deliver, NO collector / NO LLM / NO marker.
#                  These tests pin that it sends, redacts, gates on preview, and
#                  never touches the markers table.
# What's mocked: discord_send (no network) and input() (scripted preview). Config,
#                  secrets, state, redaction all run for real. summarize_raw is
#                  not mocked because intake must never call it.
# =============================================================================

import pytest

from orion import cli
from orion.state import get_marker, open_state


def _write_config(tmp_path):
    """Write a minimal orion.toml. repo_path is required by config but unused by
    intake (intake runs no collectors), so it can point anywhere."""
    toml = tmp_path / "orion.toml"
    toml.write_text(
        """
        state_db = "state.sqlite3"

        [projects.demo]
        repo_path = "/tmp/unused-by-intake"

          [[projects.demo.recipients]]
          name = "Alex"
          channel = "discord"
          webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """
    )
    return toml


@pytest.fixture
def env_and_mocks(monkeypatch):
    """Set env vars and capture mocked delivery; script preview separately.

    Why: intake needs the webhook env var and a mocked sender; centralizing it
    keeps each test to the body/answer it cares about (DRY).
    """
    monkeypatch.setenv("ORION_DISCORD_WEBHOOK_ALEX", "https://discord.test/webhook")
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(cli, "discord_send", lambda message, url: sent.append((message, url)))
    return {"sent": sent, "monkeypatch": monkeypatch}


def _answer(monkeypatch, value):
    """Script the preview confirm prompt to return `value`."""
    monkeypatch.setattr("builtins.input", lambda prompt="": value)


def test_message_flag_sends(tmp_path, env_and_mocks):
    """An update passed via --message is previewed and sent.

    Why this matters: this is the primary intake path — a body in, a message out,
    no collector or LLM involved.
    """
    mp = env_and_mocks["monkeypatch"]
    toml = _write_config(tmp_path)

    _answer(mp, "y")
    code = cli.main(
        ["intake", "demo", "--config", str(toml), "-m", "Finished the structured lane."]
    )
    assert code == 0

    sent = env_and_mocks["sent"]
    assert len(sent) == 1
    message, url = sent[0]
    assert "Finished the structured lane." in message
    assert url == "https://discord.test/webhook"


def test_stdin_body_sends(tmp_path, env_and_mocks, monkeypatch):
    """With no --message, the body is read from stdin (skill/pipe use case).

    Why this matters: the Phase-6 skill (and a shell pipe) feed the summary in on
    stdin; this path must work without the flag.
    """
    mp = env_and_mocks["monkeypatch"]
    toml = _write_config(tmp_path)

    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("Pushed from a session summary."))
    _answer(mp, "y")
    code = cli.main(["intake", "demo", "--config", str(toml)])
    assert code == 0
    assert "Pushed from a session summary." in env_and_mocks["sent"][0][0]


def test_empty_body_is_refused(tmp_path, env_and_mocks):
    """An empty/whitespace body is refused with a non-zero exit and no send.

    Why this matters: there is nothing to report; sending a blank update would be
    noise (and confusing to a supervisor).
    """
    mp = env_and_mocks["monkeypatch"]
    toml = _write_config(tmp_path)

    code = cli.main(["intake", "demo", "--config", str(toml), "-m", "   "])
    assert code == 1
    assert env_and_mocks["sent"] == []


def test_secret_in_body_is_redacted_before_send(tmp_path, env_and_mocks):
    """A secret in the pushed body is scrubbed before delivery.

    Why this matters: a pushed update is untrusted text just like a diff; the same
    redaction guarantee must apply so a leaked key never reaches the channel.
    """
    mp = env_and_mocks["monkeypatch"]
    toml = _write_config(tmp_path)

    body = "Shipped it. Oops: AKIAIOSFODNN7EXAMPLE slipped in."
    _answer(mp, "y")
    code = cli.main(["intake", "demo", "--config", str(toml), "-m", body])
    assert code == 0

    message, _ = env_and_mocks["sent"][0]
    assert "AKIAIOSFODNN7EXAMPLE" not in message


def test_declined_preview_sends_nothing(tmp_path, env_and_mocks):
    """Answering 'n' at the preview sends nothing and exits 0.

    Why this matters: preview-before-send is the human gate on intake too; a push
    is not exempt from the trust step.
    """
    mp = env_and_mocks["monkeypatch"]
    toml = _write_config(tmp_path)

    _answer(mp, "n")
    code = cli.main(["intake", "demo", "--config", str(toml), "-m", "An update."])
    assert code == 0
    assert env_and_mocks["sent"] == []


def test_intake_routes_to_both_channels(tmp_path, env_and_mocks):
    """A pushed update reaches a Discord and a Slack recipient, each formatted.

    Why this matters: intake uses the same routing path as report, so a push must
    fan out to every channel with the right rendering — Discord plain, Slack mrkdwn.
    """
    mp = env_and_mocks["monkeypatch"]
    toml = tmp_path / "orion.toml"
    toml.write_text(
        """
        state_db = "state.sqlite3"

        [projects.demo]
        repo_path = "/tmp/unused-by-intake"

          [[projects.demo.recipients]]
          name = "Alex"
          channel = "discord"
          webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"

          [[projects.demo.recipients]]
          name = "Sam"
          channel = "slack"
          webhook_env_var = "ORION_SLACK_WEBHOOK_SAM"
        """
    )
    mp.setenv("ORION_SLACK_WEBHOOK_SAM", "https://hooks.slack.test/services/Y")
    slack_sent: list[tuple[str, str]] = []
    mp.setattr(cli, "slack_send", lambda message, url: slack_sent.append((message, url)))

    _answer(mp, "y")
    code = cli.main(["intake", "demo", "--config", str(toml), "-m", "Pushed update."])
    assert code == 0

    # Both channels received the body, each via its own webhook.
    assert "Pushed update." in env_and_mocks["sent"][0][0]
    assert env_and_mocks["sent"][0][1] == "https://discord.test/webhook"
    assert len(slack_sent) == 1
    assert "Pushed update." in slack_sent[0][0]
    assert slack_sent[0][1] == "https://hooks.slack.test/services/Y"


def test_intake_does_not_touch_markers(tmp_path, env_and_mocks):
    """A successful intake advances NO collector marker.

    Why this matters: intake is a push, not a delta — it must not create or move a
    marker (which would corrupt a later `report` run's delta math). We also prove
    intake works for a never-reported project (no markers exist beforehand).
    """
    mp = env_and_mocks["monkeypatch"]
    toml = _write_config(tmp_path)

    _answer(mp, "y")
    code = cli.main(["intake", "demo", "--config", str(toml), "-m", "An update."])
    assert code == 0

    # Inspect the same state DB the command used (resolved next to the config).
    conn = open_state(tmp_path / "state.sqlite3")
    assert get_marker(conn, "demo", "git") is None
    assert get_marker(conn, "demo", "tasks") is None
    assert get_marker(conn, "demo", "notes") is None

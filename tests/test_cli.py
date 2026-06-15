# =============================================================================
# tests/test_cli.py
# -----------------------------------------------------------------------------
# Responsible for: End-to-end verification of the report pipeline orchestration,
#                  against a REAL temporary git repo, with only the LLM call and
#                  the network POST mocked out.
# Role in project: cli.py is the orchestrator; these tests pin the wiring, the
#                  preview/confirm gate, the "advance state only after a
#                  successful send" rule, and — critically — that a secret which
#                  somehow reaches the body is still redacted before sending
#                  (defense in depth at the pipeline level).
# What's mocked and why:
#   - summarize_raw: so no Anthropic API call / key is needed; we control the body.
#   - discord_send: so no network call; we record what would have been sent.
#   - input(): so the preview confirm is scripted (y / n).
# Everything else (config, state, git collection, redaction) runs for real.
# =============================================================================

import subprocess

import pytest

from orion import cli


def _run(repo, *args):
    """Run a git command in a test repo, raising on failure."""
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_repo(tmp_path):
    """Create a git repo with one commit and return its path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init", "-q")
    _run(repo, "config", "user.email", "t@example.com")
    _run(repo, "config", "user.name", "Tester")
    (repo / "feature.py").write_text("def f():\n    return 1\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "Add feature")
    return repo


def _write_config(tmp_path, repo):
    """Write an orion.toml in tmp_path pointing at the repo. Returns its path."""
    toml = tmp_path / "orion.toml"
    toml.write_text(
        f"""
        state_db = "state.sqlite3"

        [projects.demo]
        repo_path = "{repo}"
        share_level = "high_level"
        collectors = ["git"]

          [[projects.demo.recipients]]
          name = "Alex"
          channel = "discord"
          webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """
    )
    return toml


def _write_config_collectors(tmp_path, repo, collectors, *, tasks_file=None, notes_file=None):
    """Write an orion.toml enabling a chosen set of collectors.

    Args:
        tmp_path: per-test temp dir.
        repo: path to the git repo (used by the git collector).
        collectors: list of collector names to enable.
        tasks_file / notes_file: optional paths to wire in for those collectors.

    Why:
        CP6 tests exercise different collector combinations; this keeps each test
        to the combination it cares about instead of repeating TOML (DRY).
    """
    lines = [
        'state_db = "state.sqlite3"',
        "",
        "[projects.demo]",
        f'repo_path = "{repo}"',
        'share_level = "high_level"',
        f"collectors = {collectors!r}".replace("'", '"'),
    ]
    if tasks_file is not None:
        lines.append(f'tasks_file = "{tasks_file}"')
    if notes_file is not None:
        lines.append(f'notes_file = "{notes_file}"')
    lines += [
        "",
        "  [[projects.demo.recipients]]",
        '  name = "Alex"',
        '  channel = "discord"',
        '  webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"',
    ]
    toml = tmp_path / "orion.toml"
    toml.write_text("\n".join(lines) + "\n")
    return toml


def _write_dual_channel_config(tmp_path, repo):
    """Write a config with one Discord recipient and one Slack recipient.

    Why:
        Phase 3 routing tests need a project whose recipients span both channels,
        so we can assert each gets its own format via its own sender.
    """
    toml = tmp_path / "orion.toml"
    toml.write_text(
        f"""
        state_db = "state.sqlite3"

        [projects.demo]
        repo_path = "{repo}"
        share_level = "high_level"
        collectors = ["git"]

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
    return toml


@pytest.fixture
def env_and_mocks(monkeypatch):
    """Set required env vars and capture mocked LLM + delivery.

    Returns:
        A dict with a `sent` list that records (message, url) tuples that the
        (mocked) Discord sender was asked to deliver.

    Why:
        Centralizes the three mocks every CLI test needs so each test only scripts
        the input answer and asserts on outcomes (DRY).
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ORION_DISCORD_WEBHOOK_ALEX", "https://discord.test/webhook")

    sent: list[tuple[str, str]] = []

    # Default summary echoes nothing sensitive; individual tests can override.
    monkeypatch.setattr(cli, "summarize_raw", lambda text, level, *, client: "Made progress.")
    monkeypatch.setattr(cli, "discord_send", lambda message, url: sent.append((message, url)))

    return {"sent": sent, "monkeypatch": monkeypatch}


def _answer(monkeypatch, value):
    """Script the preview confirm prompt to return `value`."""
    monkeypatch.setattr("builtins.input", lambda prompt="": value)


def test_full_run_sends_and_advances_state(tmp_path, env_and_mocks):
    """A confirmed run sends the report, then a second run reports no activity.

    Why this matters: this is the core MVP behavior — the whole pipeline produces
    a message, delivery happens, state advances, and the delta logic makes the
    immediate re-run a no-op. It exercises config, state, git, redaction,
    compose, preview, deliver, and advance together.
    """
    mp = env_and_mocks["monkeypatch"]
    repo = _make_repo(tmp_path)
    toml = _write_config(tmp_path, repo)

    _answer(mp, "y")
    code = cli.main(["report", "demo", "--config", str(toml)])
    assert code == 0

    sent = env_and_mocks["sent"]
    assert len(sent) == 1
    message, url = sent[0]
    assert "Made progress." in message            # the body made it into the message
    assert url == "https://discord.test/webhook"  # delivered to the recipient's webhook

    # Second run: state advanced, so there is nothing new to report.
    sent.clear()
    code2 = cli.main(["report", "demo", "--config", str(toml)])
    assert code2 == 0
    assert sent == []  # nothing sent the second time


def test_declined_preview_sends_nothing_and_keeps_state(tmp_path, env_and_mocks):
    """Answering 'n' at the preview sends nothing and leaves state unchanged.

    Why this matters: preview-before-send is the human gate; declining must be
    safe and must NOT advance state, so the same activity is still reportable.
    """
    mp = env_and_mocks["monkeypatch"]
    repo = _make_repo(tmp_path)
    toml = _write_config(tmp_path, repo)

    _answer(mp, "n")
    code = cli.main(["report", "demo", "--config", str(toml)])
    assert code == 0
    assert env_and_mocks["sent"] == []  # nothing sent

    # State unchanged -> a confirmed run still has activity to report.
    _answer(mp, "y")
    code2 = cli.main(["report", "demo", "--config", str(toml)])
    assert code2 == 0
    assert len(env_and_mocks["sent"]) == 1


def test_secret_in_body_is_redacted_before_send(tmp_path, env_and_mocks):
    """A secret leaked into the LLM body is scrubbed by redaction pass 2.

    Why this matters: this is the defense-in-depth guarantee at the pipeline
    level — even if the model (the weakest layer) echoes a secret, the second
    redaction pass must remove it before the message is delivered.
    """
    mp = env_and_mocks["monkeypatch"]
    repo = _make_repo(tmp_path)
    toml = _write_config(tmp_path, repo)

    # Simulate the model leaking an AWS key into its summary.
    leak = "Progress, and oops the key AKIAIOSFODNN7EXAMPLE slipped in."
    mp.setattr(cli, "summarize_raw", lambda text, level, *, client: leak)

    _answer(mp, "y")
    code = cli.main(["report", "demo", "--config", str(toml)])
    assert code == 0

    message, _ = env_and_mocks["sent"][0]
    assert "AKIAIOSFODNN7EXAMPLE" not in message  # the secret was redacted before send


def test_unknown_project_errors(tmp_path, env_and_mocks):
    """Reporting an unknown project fails cleanly with a non-zero exit.

    Why this matters: a typo'd project name should be a clear error, not a crash
    or a silent no-op.
    """
    repo = _make_repo(tmp_path)
    toml = _write_config(tmp_path, repo)
    code = cli.main(["report", "nope", "--config", str(toml)])
    assert code == 1
    assert env_and_mocks["sent"] == []


def test_structured_only_run_never_calls_the_llm(tmp_path, env_and_mocks):
    """A tasks-only project sends a report without ever invoking the summarizer.

    Why this matters: this is THE Phase-2 guarantee — structured signals skip the
    LLM entirely. We prove it by making summarize_raw raise: if the orchestrator
    routed the structured lane through the model, the run would error instead of
    sending. (No git collector is enabled, so nothing should reach the LLM.)
    """
    mp = env_and_mocks["monkeypatch"]
    repo = _make_repo(tmp_path)  # exists but git collector is not enabled
    tasks = tmp_path / "TODO.md"
    tasks.write_text("- [x] Ship the structured lane\n- [ ] Slack next\n")
    toml = _write_config_collectors(tmp_path, repo, ["tasks"], tasks_file="TODO.md")

    # Any call to the summarizer is a bug on this path.
    def _boom(text, level, *, client):
        raise AssertionError("summarize_raw must not be called on the structured lane")

    mp.setattr(cli, "summarize_raw", _boom)

    _answer(mp, "y")
    code = cli.main(["report", "demo", "--config", str(toml)])
    assert code == 0

    message, _ = env_and_mocks["sent"][0]
    assert "## Completed tasks" in message
    assert "Ship the structured lane" in message


def test_git_and_tasks_merge_into_one_send(tmp_path, env_and_mocks):
    """Git + tasks active in one run produce a single, two-section message.

    Why this matters: the merge step must combine the LLM-summarized git section
    and the passed-through tasks section into ONE delivered message, not two.
    """
    mp = env_and_mocks["monkeypatch"]
    repo = _make_repo(tmp_path)
    tasks = tmp_path / "TODO.md"
    tasks.write_text("- [x] Wire the structured lane\n")
    toml = _write_config_collectors(
        tmp_path, repo, ["git", "tasks"], tasks_file="TODO.md"
    )

    mp.setattr(cli, "summarize_raw", lambda text, level, *, client: "Refactored things.")

    _answer(mp, "y")
    code = cli.main(["report", "demo", "--config", str(toml)])
    assert code == 0

    sent = env_and_mocks["sent"]
    assert len(sent) == 1  # one merged message, not one per collector
    message, _ = sent[0]
    assert "## Code activity" in message and "Refactored things." in message
    assert "## Completed tasks" in message and "Wire the structured lane" in message


def test_per_collector_markers_advance_independently(tmp_path, env_and_mocks):
    """After a combined run, a later tasks-only change reports tasks alone.

    Why this matters: each collector tracks its own marker. Once git is reported,
    a run where only the checklist changed must report just the new task — proving
    git's marker stayed advanced while tasks advanced separately.
    """
    mp = env_and_mocks["monkeypatch"]
    repo = _make_repo(tmp_path)
    tasks = tmp_path / "TODO.md"
    tasks.write_text("- [x] First task\n")
    toml = _write_config_collectors(
        tmp_path, repo, ["git", "tasks"], tasks_file="TODO.md"
    )
    mp.setattr(cli, "summarize_raw", lambda text, level, *, client: "Git work.")

    # First run: both signals fire.
    _answer(mp, "y")
    assert cli.main(["report", "demo", "--config", str(toml)]) == 0
    first_message, _ = env_and_mocks["sent"][0]
    assert "## Code activity" in first_message and "## Completed tasks" in first_message

    # Now change ONLY the checklist — no new commit.
    env_and_mocks["sent"].clear()
    tasks.write_text("- [x] First task\n- [x] Second task\n")

    _answer(mp, "y")
    assert cli.main(["report", "demo", "--config", str(toml)]) == 0
    sent = env_and_mocks["sent"]
    assert len(sent) == 1
    second_message, _ = sent[0]
    # Only the new task is reported; git is silent (its marker did not regress).
    assert "## Completed tasks" in second_message and "Second task" in second_message
    assert "## Code activity" not in second_message


def test_routes_each_recipient_to_its_channel(tmp_path, env_and_mocks):
    """A discord and a slack recipient each get their channel's format + sender.

    Why this matters: this is the core of Phase 3 routing — the same report must
    reach Discord as `##`/`**` Markdown and Slack as `*bold*` mrkdwn, each via its
    own webhook, in one run.
    """
    mp = env_and_mocks["monkeypatch"]
    repo = _make_repo(tmp_path)
    toml = _write_dual_channel_config(tmp_path, repo)

    mp.setenv("ORION_SLACK_WEBHOOK_SAM", "https://hooks.slack.test/services/Y")
    slack_sent: list[tuple[str, str]] = []
    mp.setattr(cli, "slack_send", lambda message, url: slack_sent.append((message, url)))
    mp.setattr(cli, "summarize_raw", lambda text, level, *, client: "Did the work.")

    _answer(mp, "y")
    assert cli.main(["report", "demo", "--config", str(toml)]) == 0

    # Discord recipient: Discord-flavored Markdown via the Discord webhook.
    discord_sent = env_and_mocks["sent"]
    assert len(discord_sent) == 1
    dmsg, durl = discord_sent[0]
    assert "## Code activity" in dmsg
    assert durl == "https://discord.test/webhook"

    # Slack recipient: Slack mrkdwn (bold headers, no `##`) via the Slack webhook.
    assert len(slack_sent) == 1
    smsg, surl = slack_sent[0]
    assert "*Code activity*" in smsg and "##" not in smsg
    assert surl == "https://hooks.slack.test/services/Y"


def test_dual_channel_preview_shows_both_blocks(tmp_path, env_and_mocks, capsys):
    """The preview shows a labeled block per channel, then one combined confirm.

    Why this matters: the user must see exactly what each platform will render
    before confirming — Slack and Discord differ, so one canonical block would
    hide the Slack output.
    """
    mp = env_and_mocks["monkeypatch"]
    repo = _make_repo(tmp_path)
    toml = _write_dual_channel_config(tmp_path, repo)
    mp.setenv("ORION_SLACK_WEBHOOK_SAM", "https://hooks.slack.test/services/Y")
    mp.setattr(cli, "slack_send", lambda message, url: None)
    mp.setattr(cli, "summarize_raw", lambda text, level, *, client: "Did the work.")

    _answer(mp, "y")
    cli.main(["report", "demo", "--config", str(toml)])

    out = capsys.readouterr().out
    # One labeled preview block per channel — the user sees exactly what each
    # platform will render. (The single combined confirm is exercised by the
    # route/decline tests, where one answer covers both channels.)
    assert "PREVIEW (discord)" in out
    assert "PREVIEW (slack)" in out


def test_decline_aborts_every_channel(tmp_path, env_and_mocks):
    """Declining the combined preview sends to neither channel.

    Why this matters: the single confirm is the gate for all channels; 'n' must
    abort the whole run, not just one platform.
    """
    mp = env_and_mocks["monkeypatch"]
    repo = _make_repo(tmp_path)
    toml = _write_dual_channel_config(tmp_path, repo)
    mp.setenv("ORION_SLACK_WEBHOOK_SAM", "https://hooks.slack.test/services/Y")
    slack_sent: list[tuple[str, str]] = []
    mp.setattr(cli, "slack_send", lambda message, url: slack_sent.append((message, url)))
    mp.setattr(cli, "summarize_raw", lambda text, level, *, client: "Did the work.")

    _answer(mp, "n")
    assert cli.main(["report", "demo", "--config", str(toml)]) == 0
    assert env_and_mocks["sent"] == []
    assert slack_sent == []


def test_one_channel_failure_does_not_block_the_other(tmp_path, env_and_mocks):
    """If Slack delivery fails, Discord still receives it and state advances.

    Why this matters: per-recipient failure isolation must hold ACROSS channels —
    a down Slack webhook can't suppress the Discord report or re-report next run.
    """
    from orion.delivery import DeliveryError

    mp = env_and_mocks["monkeypatch"]
    repo = _make_repo(tmp_path)
    toml = _write_dual_channel_config(tmp_path, repo)
    mp.setenv("ORION_SLACK_WEBHOOK_SAM", "https://hooks.slack.test/services/Y")

    def slack_boom(message, url):
        raise DeliveryError("slack webhook down")

    mp.setattr(cli, "slack_send", slack_boom)
    mp.setattr(cli, "summarize_raw", lambda text, level, *, client: "Did the work.")

    _answer(mp, "y")
    assert cli.main(["report", "demo", "--config", str(toml)]) == 0
    # Discord got it despite Slack failing.
    assert len(env_and_mocks["sent"]) == 1

    # State advanced (Discord succeeded), so an immediate re-run is a no-op.
    env_and_mocks["sent"].clear()
    assert cli.main(["report", "demo", "--config", str(toml)]) == 0
    assert env_and_mocks["sent"] == []


def test_no_activity_across_all_collectors_sends_nothing(tmp_path, env_and_mocks):
    """When no enabled collector has activity, nothing is sent and exit is 0.

    Why this matters: an immediate re-run after a successful report must be a clean
    no-op across the whole structured+raw set, not an empty or duplicate message.
    """
    mp = env_and_mocks["monkeypatch"]
    repo = _make_repo(tmp_path)
    tasks = tmp_path / "TODO.md"
    tasks.write_text("- [x] Done already\n")
    toml = _write_config_collectors(
        tmp_path, repo, ["git", "tasks"], tasks_file="TODO.md"
    )
    mp.setattr(cli, "summarize_raw", lambda text, level, *, client: "Git work.")

    _answer(mp, "y")
    assert cli.main(["report", "demo", "--config", str(toml)]) == 0
    assert len(env_and_mocks["sent"]) == 1

    # Second run with no git or task changes: nothing to report.
    env_and_mocks["sent"].clear()
    assert cli.main(["report", "demo", "--config", str(toml)]) == 0
    assert env_and_mocks["sent"] == []

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
#   - the summarizer seam (cli._build_summarizer via conftest.use_summary): so no
#     Anthropic API call / key is needed; we control the body.
#   - discord_send: so no network call; we record what would have been sent.
#   - input(): so the preview confirm is scripted (y / n).
# Everything else (config, state, git collection, redaction) runs for real.
# =============================================================================

import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from orion import cli
from orion.extract import ExtractError
from orion.config import (
    PUSH_ONLY_COLLECTORS,
    REPORT_COLLECTORS,
    ConfigError,
    get_project,
    load_config,
)
from orion.state import get_last_checklist_push, get_marker, open_state

# The shared end-to-end helpers and the env_and_mocks fixture live in conftest.py
# so test_schedule.py reuses the exact same setup (DRY). pytest auto-discovers the
# fixture by name; the plain helper functions are imported explicitly.
from conftest import _answer, _make_repo, _payload_text, _run, _write_config, use_summary


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
        # as_posix(): forward slashes so a Windows repo path isn't read as a TOML
        # escape sequence (see conftest._write_config for the full why).
        f'repo_path = "{repo.as_posix()}"',
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
        repo_path = "{repo.as_posix()}"
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
    use_summary(mp, leak)

    _answer(mp, "y")
    code = cli.main(["report", "demo", "--config", str(toml)])
    assert code == 0

    message, _ = env_and_mocks["sent"][0]
    assert "AKIAIOSFODNN7EXAMPLE" not in message  # the secret was redacted before send


def test_report_blob_carries_twice_redacted_sections(tmp_path, env_and_mocks):
    """A real run carries each signal as a section on the blob, already redacted.

    Why this matters: B3 builds Block Kit / embeds from blob.sections, so two
    guarantees must hold from CP1 on: (1) the sections are carried in config order
    with their titles, and (2) redaction pass 2 runs on EACH section body, so a
    secret that reaches a section is scrubbed there too — a structured payload must
    never become a redaction bypass. We capture the blob by wrapping cli.compose.
    """
    mp = env_and_mocks["monkeypatch"]
    repo = _make_repo(tmp_path)
    tasks = tmp_path / "TODO.md"
    tasks.write_text("- [x] Ship the structured lane\n")
    toml = _write_config_collectors(
        tmp_path, repo, ["git", "tasks"], tasks_file="TODO.md"
    )

    # The model leaks an AWS key into the git (raw-lane) summary.
    leak = "Made progress; key AKIAIOSFODNN7EXAMPLE slipped in."
    use_summary(mp, leak)

    # Capture the blob handed to compose without changing what compose returns.
    captured = {}
    real_compose = cli.compose

    def _capturing_compose(blob, channel, display_timezone):
        captured["blob"] = blob
        return real_compose(blob, channel, display_timezone)

    mp.setattr(cli, "compose", _capturing_compose)

    _answer(mp, "y")
    code = cli.main(["report", "demo", "--config", str(toml)])
    assert code == 0

    blob = captured["blob"]
    # Two sections, in config order (git first, then tasks), with their titles.
    titles = [title for title, _ in blob.sections]
    assert titles == ["Code activity", "Completed tasks"]
    # The secret is scrubbed in the carried section body (pass 2 reached it)...
    git_body = blob.sections[0][1]
    assert "AKIAIOSFODNN7EXAMPLE" not in git_body
    # ...and in the flat fallback body, which is the merge of those sections.
    assert "AKIAIOSFODNN7EXAMPLE" not in blob.body
    assert "Ship the structured lane" in blob.sections[1][1]


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
    LLM entirely. We prove it by making the summarizer seam raise on construction:
    the builder is only invoked on the raw lane, so if the orchestrator routed the
    structured lane through the model, the run would error instead of sending. (No
    git collector is enabled, so the summarizer should never even be built.)
    """
    mp = env_and_mocks["monkeypatch"]
    repo = _make_repo(tmp_path)  # exists but git collector is not enabled
    tasks = tmp_path / "TODO.md"
    tasks.write_text("- [x] Ship the structured lane\n- [ ] Slack next\n")
    toml = _write_config_collectors(tmp_path, repo, ["tasks"], tasks_file="TODO.md")

    # Building (or calling) the summarizer at all is a bug on this path: the seam
    # is constructed lazily and only when a raw collector has activity.
    def _boom(cfg, secret_getter):
        raise AssertionError("the summarizer must not be built on the structured lane")

    mp.setattr(cli, "_build_summarizer", _boom)

    _answer(mp, "y")
    code = cli.main(["report", "demo", "--config", str(toml)])
    assert code == 0

    message, _ = env_and_mocks["sent"][0]
    assert "Completed tasks" in message
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

    use_summary(mp, "Refactored things.")

    _answer(mp, "y")
    code = cli.main(["report", "demo", "--config", str(toml)])
    assert code == 0

    sent = env_and_mocks["sent"]
    assert len(sent) == 1  # one merged message, not one per collector
    message, _ = sent[0]
    assert "Code activity" in message and "Refactored things." in message
    assert "Completed tasks" in message and "Wire the structured lane" in message


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
    use_summary(mp, "Git work.")

    # First run: both signals fire.
    _answer(mp, "y")
    assert cli.main(["report", "demo", "--config", str(toml)]) == 0
    first_message, _ = env_and_mocks["sent"][0]
    assert "Code activity" in first_message and "Completed tasks" in first_message

    # Now change ONLY the checklist — no new commit.
    env_and_mocks["sent"].clear()
    tasks.write_text("- [x] First task\n- [x] Second task\n")

    _answer(mp, "y")
    assert cli.main(["report", "demo", "--config", str(toml)]) == 0
    sent = env_and_mocks["sent"]
    assert len(sent) == 1
    second_message, _ = sent[0]
    # Only the new task is reported; git is silent (its marker did not regress).
    assert "Completed tasks" in second_message and "Second task" in second_message
    assert "Code activity" not in second_message


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
    mp.setattr(
        cli, "slack_send", lambda payload, url: slack_sent.append((_payload_text(payload), url))
    )
    use_summary(mp, "Did the work.")

    _answer(mp, "y")
    assert cli.main(["report", "demo", "--config", str(toml)]) == 0

    # Discord recipient: Discord-flavored Markdown via the Discord webhook.
    discord_sent = env_and_mocks["sent"]
    assert len(discord_sent) == 1
    dmsg, durl = discord_sent[0]
    assert "Code activity" in dmsg
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
    mp.setattr(cli, "slack_send", lambda payload, url: None)
    use_summary(mp, "Did the work.")

    _answer(mp, "y")
    cli.main(["report", "demo", "--config", str(toml)])

    out = capsys.readouterr().out
    # One labeled preview block per audience — the user sees exactly what each
    # platform will render AND who receives it. With D5 the label names the channel
    # and the recipients, so two audiences on the same channel never collide. (The
    # single combined confirm is exercised by the route/decline tests, where one
    # answer covers both channels.)
    assert "PREVIEW (discord → Alex)" in out
    assert "PREVIEW (slack → Sam)" in out


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
    mp.setattr(
        cli, "slack_send", lambda payload, url: slack_sent.append((_payload_text(payload), url))
    )
    use_summary(mp, "Did the work.")

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
    use_summary(mp, "Did the work.")

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
    use_summary(mp, "Git work.")

    _answer(mp, "y")
    assert cli.main(["report", "demo", "--config", str(toml)]) == 0
    assert len(env_and_mocks["sent"]) == 1

    # Second run with no git or task changes: nothing to report.
    env_and_mocks["sent"].clear()
    assert cli.main(["report", "demo", "--config", str(toml)]) == 0
    assert env_and_mocks["sent"] == []


# --- _build_summarizer dispatch (B4) ------------------------------------------
#
# These pin the provider -> backend wiring directly (no full pipeline needed). The
# secret_getter is a stand-in for get_required, so we also prove the key-fetch
# policy: Anthropic always reads ANTHROPIC_API_KEY; a local backend reads a key
# ONLY when api_key_env is set, and otherwise never touches the getter.


def test_build_summarizer_anthropic_reads_the_anthropic_key():
    """The anthropic provider builds an AnthropicSummarizer using ANTHROPIC_API_KEY.

    Why this matters: the default backend must keep reading exactly the env var
    existing setups already use — an upgrade must not silently change the key name.
    """
    from orion.config import SummarizerConfig
    from orion.summarize import AnthropicSummarizer

    fetched = []
    summarizer = cli._build_summarizer(
        SummarizerConfig(provider="anthropic", model="claude-haiku-4-5"),
        lambda name: fetched.append(name) or "test-key",
    )
    assert isinstance(summarizer, AnthropicSummarizer)
    assert fetched == ["ANTHROPIC_API_KEY"]


def test_build_summarizer_local_without_key_never_fetches_a_secret():
    """A local backend with no api_key_env builds without touching the getter.

    Why this matters: most local servers need no key; the build path must not
    demand one (which would defeat the privacy point and block the common case).
    """
    from orion.config import SummarizerConfig
    from orion.summarize import LocalSummarizer

    def _must_not_be_called(name):
        raise AssertionError(f"no secret should be fetched, got {name!r}")

    summarizer = cli._build_summarizer(
        SummarizerConfig(
            provider="local", model="llama3.1", base_url="http://localhost:11434/v1"
        ),
        _must_not_be_called,
    )
    assert isinstance(summarizer, LocalSummarizer)


def test_build_summarizer_local_with_key_fetches_named_var():
    """A local backend WITH api_key_env fetches exactly that named variable.

    Why this matters: the rare keyed endpoint must read the user-named var (not a
    hardcoded one), proving the per-provider secret convention is honored.
    """
    from orion.config import SummarizerConfig

    fetched = []
    cli._build_summarizer(
        SummarizerConfig(
            provider="local",
            model="m",
            base_url="http://x/v1",
            api_key_env="LOCAL_LLM_KEY",
        ),
        lambda name: fetched.append(name) or "tok",
    )
    assert fetched == ["LOCAL_LLM_KEY"]


# --- Relay push wiring (C1, CP4) ----------------------------------------------
#
# These pin the additive, fail-soft relay push: it fires exactly once after a
# successful delivery when enabled, is a no-op when disabled, never fires when no
# delivery succeeded, and a relay failure never changes the run's outcome. The
# low-level sender (cli.relay_push) is monkeypatched, so nothing leaves the machine
# and we assert on what WOULD have been pushed.


def _write_relay_config(tmp_path, repo, *, enabled=True, display_timezone=None):
    """Write an orion.toml with a [relay] table pointing at a fake relay URL.

    Args:
        tmp_path: per-test temp dir (also where the state db lives).
        repo: path to the git repo (used by the git collector).
        enabled: whether the [relay] table is enabled.
        display_timezone: optional IANA zone for the top-level `display_timezone` key. None
            omits it, so the config keeps the Pacific default (KI-20).

    Why:
        The relay tests need a project plus a [relay] table; this keeps each test to
        the enabled/disabled case it cares about instead of repeating TOML (DRY).
    """
    toml = tmp_path / "orion.toml"
    # An omitted display_timezone leaves the config on its Pacific default (KI-20), so
    # every existing caller of this helper keeps the config it had.
    tz_line = f'display_timezone = "{display_timezone}"' if display_timezone else ""
    toml.write_text(
        f"""
        state_db = "state.sqlite3"
        {tz_line}

        [relay]
        enabled = {str(enabled).lower()}
        url = "https://relay.test/ingest"
        token_env_var = "ORION_RELAY_TOKEN"

        [projects.demo]
        repo_path = "{repo.as_posix()}"
        share_level = "high_level"
        collectors = ["git"]

          [[projects.demo.recipients]]
          name = "Alex"
          channel = "discord"
          webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """
    )
    return toml


def _capture_relay(mp):
    """Monkeypatch cli.relay_push to record (blob_json, url, token) calls.

    Why:
        Mirrors how env_and_mocks captures discord_send — replace the real sender
        with an in-memory recorder so the test asserts on what would have been
        pushed without any network call.
    """
    pushes: list[tuple[str, str, str]] = []
    mp.setattr(cli, "relay_push", lambda blob_json, url, token: pushes.append((blob_json, url, token)))
    return pushes


def test_relay_push_fires_once_on_success(tmp_path, env_and_mocks):
    """An enabled relay pushes the serialized blob exactly once after delivery.

    Why this matters: C1's whole point is that a delivered report ALSO reaches the
    dashboard. The push must happen once per run (not per recipient), carry the
    serialized blob (the project name proves it is the real blob, not a channel
    payload), and use the configured url + the token from .env.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_relay(mp)
    repo = _make_repo(tmp_path)
    toml = _write_relay_config(tmp_path, repo)

    _answer(mp, "y")
    code = cli.main(["report", "demo", "--config", str(toml)])
    assert code == 0

    # The report itself was delivered (unchanged behavior)...
    assert len(env_and_mocks["sent"]) == 1
    # ...and the relay got exactly one push with the right url/token and the blob.
    assert len(pushes) == 1
    blob_json, url, token = pushes[0]
    assert url == "https://relay.test/ingest"
    assert token == "relay-secret"
    assert '"demo"' in blob_json  # the serialized portable blob, not a chat payload


def test_relay_push_blob_carries_configured_due_soon_days(tmp_path, env_and_mocks):
    """A report run threads the project's `due_soon_days` into the pushed ingest blob.

    Why this matters: this is carrier 1 of 2 for the due-soon window (the ingest blob;
    the /checklist push is carrier 2). It pins that `_run_report` builds the blob with
    the config value, so the relay receives it on the report path too. A project without
    the setting omits the key (wire stays back-compatible).
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_relay(mp)
    repo = _make_repo(tmp_path)
    toml = tmp_path / "orion.toml"
    toml.write_text(
        f"""
        state_db = "state.sqlite3"

        [relay]
        enabled = true
        url = "https://relay.test/ingest"
        token_env_var = "ORION_RELAY_TOKEN"

        [projects.demo]
        repo_path = "{repo.as_posix()}"
        share_level = "high_level"
        collectors = ["git"]
        due_soon_days = 21

          [[projects.demo.recipients]]
          name = "Alex"
          channel = "discord"
          webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """
    )

    _answer(mp, "y")
    code = cli.main(["report", "demo", "--config", str(toml)])
    assert code == 0
    assert len(pushes) == 1
    blob_json, _url, _token = pushes[0]
    assert json.loads(blob_json)["due_soon_days"] == 21


def test_relay_disabled_is_a_no_op(tmp_path, env_and_mocks):
    """With the relay disabled (no table), no push happens and the report still sends.

    Why this matters: the relay is opt-in; every pre-C1 config (no [relay] table)
    must behave exactly as before — deliver, but push nowhere.
    """
    mp = env_and_mocks["monkeypatch"]
    pushes = _capture_relay(mp)
    repo = _make_repo(tmp_path)
    toml = _write_config(tmp_path, repo)  # no [relay] table at all

    _answer(mp, "y")
    code = cli.main(["report", "demo", "--config", str(toml)])
    assert code == 0

    assert len(env_and_mocks["sent"]) == 1  # report delivered as usual
    assert pushes == []                      # but nothing pushed to a relay


def test_relay_does_not_fire_when_no_delivery_succeeds(tmp_path, env_and_mocks):
    """If every delivery fails, the relay is never pushed (it follows a real send).

    Why this matters: the relay push is placed AFTER the "at least one delivery
    succeeded" guard, so a fully-failed run (nothing delivered, state not advanced)
    must not leak a blob to the dashboard either.
    """
    from orion.delivery import DeliveryError

    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_relay(mp)
    # Make the only recipient's delivery fail, so sent_to ends up empty.
    def _fail(payload, url):
        raise DeliveryError("webhook down")
    mp.setattr(cli, "discord_send", _fail)
    repo = _make_repo(tmp_path)
    toml = _write_relay_config(tmp_path, repo)

    _answer(mp, "y")
    code = cli.main(["report", "demo", "--config", str(toml)])
    assert code == 1          # no delivery succeeded -> FAILED
    assert pushes == []       # and the relay was never reached


def test_relay_push_carries_redacted_live_checklist(tmp_path, env_and_mocks):
    """With `checklist = true`, the relay push carries the full open+done checklist,
    item texts redacted, even though the tasks delta had no new completions.

    Why this matters: this pins the E2 Inc 2 wiring end-to-end through the
    orchestrator. (1) The live checklist rides on the relay (full) blob, NOT the chat
    payloads. (2) It is captured INDEPENDENTLY of the tasks delta — here git activity
    triggers the report and the already-done item is "old" (so the tasks section
    reports nothing new), yet the snapshot still carries both the done and the open
    item. (3) Each item text passes through redaction before it leaves the machine —
    a secret in an item name must be scrubbed, the structured-lane privacy net.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_relay(mp)
    repo = _make_repo(tmp_path)  # git activity → a report is generated and pushed

    # A checklist with a done item, an open item, and a secret embedded in an item.
    tasks = tmp_path / "TODO.md"
    tasks.write_text(
        "- [x] Wire the relay\n"
        "- [ ] Render the dashboard\n"
        "- [ ] Rotate AKIAIOSFODNN7EXAMPLE\n",
        encoding="utf-8",
    )

    toml = tmp_path / "orion.toml"
    toml.write_text(
        f"""
        state_db = "state.sqlite3"

        [relay]
        enabled = true
        url = "https://relay.test/ingest"
        token_env_var = "ORION_RELAY_TOKEN"

        [projects.demo]
        repo_path = "{repo.as_posix()}"
        share_level = "high_level"
        collectors = ["git", "tasks"]
        tasks_file = "TODO.md"
        checklist = true

          [[projects.demo.recipients]]
          name = "Alex"
          channel = "discord"
          webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """
    )

    _answer(mp, "y")
    code = cli.main(["report", "demo", "--config", str(toml)])
    assert code == 0
    assert len(pushes) == 1

    blob_json, _url, _token = pushes[0]
    payload = json.loads(blob_json)
    checklist = payload["checklist"]

    # Full current checklist (open + done), in file order, with done-state.
    assert checklist[0] == {"text": "Wire the relay", "done": True}
    assert checklist[1] == {"text": "Render the dashboard", "done": False}
    # The secret-bearing item is still present (with its open state) but scrubbed.
    assert checklist[2]["done"] is False
    assert "AKIAIOSFODNN7EXAMPLE" not in checklist[2]["text"]
    # And the secret never appears anywhere in the pushed payload.
    assert "AKIAIOSFODNN7EXAMPLE" not in blob_json


# --- disciplines-push: the empty-clobber guard + the explicit clear -----------
# The push is a full-state REPLACE, and the snapshot is deliberately fail-soft, so
# "no cards" is reachable by accident (a moved doc, a failed extraction). These pin
# that an accidental empty NEVER goes out, while a deliberate one still can.


def _capture_disciplines_pushes(mp):
    """Record (url, project, cards, token) push_disciplines calls.

    Why: the command's only outbound effect is push_disciplines, so an in-memory
    recorder lets a test assert what WOULD have hit the relay — including, crucially,
    that nothing was pushed at all — with no network. Mirrors
    _capture_checklist_pushes.
    """
    pushes = []
    mp.setattr(
        cli,
        "push_disciplines",
        lambda url, project, cards, token: pushes.append((url, project, cards, token)),
    )
    return pushes


def _disciplines_config(tmp_path, doc_path):
    """Write an orion.toml enabling disciplines against `doc_path` + a [relay] table.

    Why: every test here varies exactly one thing — whether that doc is readable —
    so the config shape is written once.
    """
    toml = tmp_path / "orion.toml"
    toml.write_text(
        f"""
        state_db = "state.sqlite3"

        [relay]
        enabled = true
        url = "https://relay.test/ingest"
        token_env_var = "ORION_RELAY_TOKEN"

        [projects.demo]
        repo_path = "{tmp_path.as_posix()}"
        collectors = ["git", "disciplines"]
        discipline_docs = ["{Path(doc_path).as_posix()}"]

          [[projects.demo.recipients]]
          name = "Alex"
          channel = "discord"
          webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """
    )
    return toml


def test_disciplines_push_refuses_to_wipe_when_the_doc_cannot_be_read(
    tmp_path, env_and_mocks
):
    """A missing discipline doc fails loudly instead of silently clearing the cards.

    The scenario: a doc that was readable when the cards were first pushed is later
    renamed, moved, or (the trap that found this) named by a RELATIVE path in a config
    that lives in a different directory. snapshot() fail-softs to zero cards, the push
    full-state-replaces, and the project's whole "Working agreements" section is wiped
    while the command prints success. Reproduced live: 16 real cards -> 0, exit 0.

    We assert the strong property — push_disciplines is never CALLED — because a guard
    that merely warned would still have destroyed the data.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_disciplines_pushes(mp)
    toml = _disciplines_config(tmp_path, tmp_path / "gone.md")  # never created

    code = cli.main(["disciplines-push", "demo", "--config", str(toml)])

    assert code == 1
    assert pushes == [], "an accidental empty set must never reach the relay"


def test_disciplines_push_refuses_when_every_extraction_fails(tmp_path, env_and_mocks):
    """A readable doc whose extraction fails is also refused, not pushed as empty.

    Why this matters: this is the same wipe by a likelier route. The doc is fine; the
    model call failed (API error, unparseable reply), which snapshot() deliberately
    fail-softs so other docs proceed. With one doc that leaves zero cards, and a
    transient outage would have quietly cleared a live dashboard section.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_disciplines_pushes(mp)
    doc = tmp_path / "PRINCIPLES.md"
    doc.write_text("- Explicit over clever.\n", encoding="utf-8")
    toml = _disciplines_config(tmp_path, doc)

    # A doomed extractor: stands in for the model being unreachable this run.
    class _FailingExtractor:
        def extract(self, text, *, source):
            raise ExtractError("simulated API failure")

    mp.setattr(cli, "_build_extractor", lambda cfg, getter: _FailingExtractor())

    code = cli.main(["disciplines-push", "demo", "--config", str(toml)])

    assert code == 1
    assert pushes == []


def test_disciplines_push_clear_empties_the_section_deliberately(
    tmp_path, env_and_mocks
):
    """`--clear` is the sanctioned way to retire the cards, and needs no doc or key.

    Why this matters: the guard above must not trap a user who genuinely wants the
    section gone. Keeping the deliberate clear as its own FLAG is what lets the code
    tell "observed nothing" apart from "asked for nothing" instead of collapsing both
    into an empty push — the same tri-state idiom KI-35 settled for due_soon_days.
    Clearing observes nothing, so it must not demand an API key: _build_extractor is
    monkeypatched to explode, proving it is never called.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_disciplines_pushes(mp)
    toml = _disciplines_config(tmp_path, tmp_path / "also-gone.md")

    def _explode(cfg, getter):
        raise AssertionError("--clear must not build an extractor")

    mp.setattr(cli, "_build_extractor", _explode)

    code = cli.main(["disciplines-push", "demo", "--config", str(toml), "--clear"])

    assert code == 0
    assert len(pushes) == 1
    _url, project, cards, token = pushes[0]
    assert project == "demo"
    assert cards == [], "an explicit clear pushes exactly the empty set"
    assert token == "relay-secret"


# --- checklist-push: the dedicated checklist-only push + --watch (near-real-time) ---


def _capture_checklist_pushes(mp):
    """Record push_checklist calls as (url, project, checklist, token, kind,
    due_soon_days, clear_due_soon_days, about, clear_about) tuples.

    Why: the command/watch loop call push_checklist as their only outbound effect; an
    in-memory recorder lets each test assert on the exact payload without any network. Every
    keyword-only extra rides every call, so the recorder captures them all: `kind` (E2 Inc
    4, project/tracker), `due_soon_days` (E1.2, the per-project due-soon window, None
    when unset), `clear_due_soon_days` (KI-35, the explicit clear), and — Unit 2 — `about`
    (the observed About line, None when unset) plus `clear_about`. Each clear flag is
    captured SEPARATELY from its value because both a clear and an unconfigured project pass
    the value as None; only the flag tells them apart (the transport turns it into an
    explicit JSON null).
    """
    pushes = []
    mp.setattr(
        cli,
        "push_checklist",
        lambda url, project, checklist, token, *, kind="project", due_soon_days=None, clear_due_soon_days=False, about=None, clear_about=False: pushes.append(
            (url, project, checklist, token, kind, due_soon_days, clear_due_soon_days, about, clear_about)
        ),
    )
    return pushes


def _checklist_config(
    tmp_path, *, checklist=True, relay_enabled=True, due_soon_days=None, about_file=None
):
    """Write an orion.toml with a checklist-enabled project + a [relay] table.

    Why: the checklist-push tests share the same shape (a tasks project + a relay);
    this keeps each test to the one knob (checklist on/off, relay on/off, an optional
    configured due-soon horizon, an optional about_file) it varies. about_file resolves
    relative to repo_path (= tmp_path here), so a test writes tmp_path/<about_file>.
    """
    horizon = "" if due_soon_days is None else f"due_soon_days = {due_soon_days}"
    about = "" if about_file is None else f'about_file = "{about_file}"'
    toml = tmp_path / "orion.toml"
    toml.write_text(
        f"""
        state_db = "state.sqlite3"

        [relay]
        enabled = {str(relay_enabled).lower()}
        url = "https://relay.test/ingest"
        token_env_var = "ORION_RELAY_TOKEN"

        [projects.demo]
        repo_path = "{tmp_path.as_posix()}"
        collectors = ["tasks"]
        tasks_file = "TODO.md"
        checklist = {str(checklist).lower()}
        {horizon}
        {about}

          [[projects.demo.recipients]]
          name = "Alex"
          channel = "discord"
          webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """
    )
    return toml


def test_checklist_push_one_shot_pushes_redacted_checklist(tmp_path, env_and_mocks):
    """`checklist-push <project>` pushes the full open+done checklist, items redacted.

    Why this matters: this is the one-shot near-real-time primitive end to end. No
    report is generated — it reads tasks_file directly, redacts each item (a secret in
    an item name must be scrubbed), and pushes via push_checklist with the relay url +
    token. We assert the exact payload and that the secret never leaves the machine.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_checklist_pushes(mp)

    (tmp_path / "TODO.md").write_text(
        "- [x] Wire the relay\n"
        "- [ ] Render the dashboard\n"
        "- [ ] Rotate AKIAIOSFODNN7EXAMPLE\n",
        encoding="utf-8",
    )
    toml = _checklist_config(tmp_path)

    code = cli.main(["checklist-push", "demo", "--config", str(toml)])
    assert code == 0
    assert len(pushes) == 1

    url, project, checklist, token, kind, due_soon_days, _clear, _about, _clear_about = pushes[0]
    assert url == "https://relay.test/ingest"  # the client derives /checklist from it
    assert project == "demo"
    assert token == "relay-secret"
    assert kind == "project"  # default kind rides the push
    assert due_soon_days is None  # not configured → omitted from the push
    assert checklist[0] == {"text": "Wire the relay", "done": True}
    assert checklist[1] == {"text": "Render the dashboard", "done": False}
    # The secret-bearing item is present (open) but scrubbed — the privacy net holds on
    # this lane too (shared _redacted_checklist).
    assert checklist[2]["done"] is False
    assert "AKIAIOSFODNN7EXAMPLE" not in checklist[2]["text"]


def test_checklist_push_one_shot_records_history(tmp_path, env_and_mocks):
    """A successful one-shot push logs one checklist_push_history row (E1.3).

    Why this matters: the scheduled `--due` path (Unit 2) reads this log to gate on
    cadence + content change. This is the write side: after a real push, exactly one row
    exists, and its content_hash equals the hash of the WIRE payload that was pushed
    (items + kind + due_soon_days) — the same value the change-gate will later compare.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    _capture_checklist_pushes(mp)  # keep the push off the network

    (tmp_path / "TODO.md").write_text("- [ ] Ship it\n", encoding="utf-8")
    toml = _checklist_config(tmp_path)

    code = cli.main(["checklist-push", "demo", "--config", str(toml)])
    assert code == 0

    # Reopen the state store the command wrote (state_db resolves beside the config).
    conn = open_state(tmp_path / "state.sqlite3")
    row = get_last_checklist_push(conn, "demo")
    assert row is not None
    pushed_at, content_hash = row
    assert pushed_at  # a non-empty ISO timestamp was stamped
    # The recorded hash must match the wire payload the command pushed. `demo` sets no
    # due_soon_days (None) and defaults kind="project", mirroring the push args above.
    project = get_project(load_config(toml), "demo")
    payload = cli._checklist_payload(project)
    about, _ = cli._redacted_about(project)
    expected = cli._checklist_content_hash(
        payload, project.kind, project.due_soon_days, about
    )
    assert content_hash == expected


# --- KI-35: `checklist-push --clear-due-soon-days` (the explicit clear) --------------


def test_checklist_push_clear_flag_sends_the_clear_instead_of_the_config_value(
    tmp_path, env_and_mocks
):
    """--clear-due-soon-days sends the clear signal, overriding the configured horizon.

    Why this matters: the relay no longer clears a setting because a push omitted it
    (KI-35), so this flag is the ONLY way to reset a horizon. It must reach the transport
    as the clear flag with no value alongside it — a clear that quietly carried the config
    value would set the horizon it was asked to remove. The project here deliberately HAS
    `due_soon_days = 14` configured, which is the case where the two could conflict.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_checklist_pushes(mp)

    (tmp_path / "TODO.md").write_text("- [ ] Ship it\n", encoding="utf-8")
    toml = _checklist_config(tmp_path, due_soon_days=14)

    code = cli.main(
        ["checklist-push", "demo", "--config", str(toml), "--clear-due-soon-days"]
    )
    assert code == 0
    _url, _project, _checklist, _token, _kind, due_soon_days, clear, _about, _clear_about = pushes[0]
    assert clear is True
    assert due_soon_days is None  # the config's 14 must NOT ride along


def test_checklist_push_clear_flag_records_the_cleared_state_in_history(
    tmp_path, env_and_mocks
):
    """The history row after a clear hashes the CLEARED horizon, not the configured one.

    Why this matters: the `--all --due` change-gate compares a project's next payload
    against this recorded hash. If a clear recorded config's 14 (what it did not send)
    instead of None (what it did), the gate would be comparing against a state the relay
    never saw, and a later push could be wrongly skipped as unchanged.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    _capture_checklist_pushes(mp)

    (tmp_path / "TODO.md").write_text("- [ ] Ship it\n", encoding="utf-8")
    toml = _checklist_config(tmp_path, due_soon_days=14)

    assert cli.main(
        ["checklist-push", "demo", "--config", str(toml), "--clear-due-soon-days"]
    ) == 0

    conn = open_state(tmp_path / "state.sqlite3")
    _pushed_at, content_hash = get_last_checklist_push(conn, "demo")
    project = get_project(load_config(toml), "demo")
    payload = cli._checklist_payload(project)
    # Hashed with None — the horizon actually put on the wire — not project.due_soon_days.
    # `demo` has no about_file, so About is None on the wire too.
    assert content_hash == cli._checklist_content_hash(payload, project.kind, None, None)
    assert content_hash != cli._checklist_content_hash(payload, project.kind, 14, None)


def _assert_clear_flag_rejected(tmp_path, env_and_mocks, capsys, args):
    """Assert a --clear-due-soon-days invocation is a usage error with no outbound push.

    Why: the two rejection cases (--all, --watch) differ only in the argv they build, so
    the setup and the three assertions live here rather than being written twice (DRY).
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_checklist_pushes(mp)
    (tmp_path / "TODO.md").write_text("- [ ] Ship it\n", encoding="utf-8")

    assert cli.main(args(_checklist_config(tmp_path))) == 2
    assert pushes == []  # rejected before any outbound effect
    assert "clear-due-soon-days" in capsys.readouterr().err


def test_checklist_push_clear_flag_is_rejected_with_all(tmp_path, env_and_mocks, capsys):
    """--clear-due-soon-days with --all is a usage error (exit 2).

    Why this matters: clearing is a deliberate act on ONE project. A sweep would wipe the
    horizon of every project in the config at once — a destructive fat-finger the flag
    should make impossible rather than merely discourage.
    """
    _assert_clear_flag_rejected(
        tmp_path, env_and_mocks, capsys,
        lambda toml: ["checklist-push", "--all", "--config", str(toml), "--clear-due-soon-days"],
    )


def test_checklist_push_clear_flag_is_rejected_with_watch(tmp_path, env_and_mocks, capsys):
    """--clear-due-soon-days with --watch is a usage error (exit 2).

    Why this matters: a watch loop re-pushes on every change, so a clear riding it would
    fight any horizon the user later configures, re-clearing it indefinitely. A clear is
    single-shot by nature. Confining the flag to the one-shot push also keeps it away from
    the --all --due change-gate, so a clear can never be skipped as "no change".
    """
    _assert_clear_flag_rejected(
        tmp_path, env_and_mocks, capsys,
        lambda toml: ["checklist-push", "demo", "--config", str(toml), "--watch", "--clear-due-soon-days"],
    )


def test_checklist_push_failed_delivery_records_nothing(tmp_path, env_and_mocks):
    """A push that raises DeliveryError leaves NO history row (record-after-success).

    Why this matters: recording sits after push_checklist returns, so a failed push must
    not look like it happened — otherwise a later `--due` run would think the project is
    fresh and skip a real update. We make the push raise and assert exit 1 with an empty
    history (the DB + table exist, opened before the push, but hold no row).
    """
    from orion.delivery import DeliveryError

    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    # The push transport raises — a down relay / bad token surfaces exactly this.
    mp.setattr(
        cli,
        "push_checklist",
        lambda *a, **k: (_ for _ in ()).throw(DeliveryError("relay down")),
    )

    (tmp_path / "TODO.md").write_text("- [ ] Ship it\n", encoding="utf-8")
    toml = _checklist_config(tmp_path)

    code = cli.main(["checklist-push", "demo", "--config", str(toml)])
    assert code == 1  # a one-shot delivery failure is fatal

    conn = open_state(tmp_path / "state.sqlite3")
    assert get_last_checklist_push(conn, "demo") is None  # nothing recorded


def test_checklist_content_hash_sensitive_to_due_soon_days(tmp_path):
    """The wire-payload hash changes when only due_soon_days changes (config-only edit).

    Why this matters: the hash must cover the WIRE payload, not just the raw items, so a
    horizon-only config change still counts as a change and re-pushes (keeping the relay's
    due-soon flag current — the KI-35 case-2 mitigation under change-gating). Same items,
    same kind, different due_soon_days → different hash; identical inputs → identical hash.
    """
    payload = [{"text": "Ship it", "done": False}]
    base = cli._checklist_content_hash(payload, "project", None, None)
    changed = cli._checklist_content_hash(payload, "project", 14, None)
    assert base != changed
    # Deterministic: identical inputs hash identically (no ordering/encoding nondeterminism).
    assert base == cli._checklist_content_hash(payload, "project", None, None)


def test_checklist_content_hash_sensitive_to_about(tmp_path):
    """The wire-payload hash changes when only the About line changes.

    Why this matters: About rides the checklist carrier, so a project whose ONLY change
    since its last push is an edited About must NOT be gated as "no change" by the
    `--all --due` scheduled sweep — otherwise the dashboard's About would silently go
    stale (the freshness-honesty regression the Unit 2 risk list calls out). Same items,
    same kind/horizon, different About → different hash; identical inputs → identical hash.
    """
    payload = [{"text": "Ship it", "done": False}]
    base = cli._checklist_content_hash(payload, "project", None, None)
    with_about = cli._checklist_content_hash(payload, "project", None, "A build tool.")
    edited = cli._checklist_content_hash(payload, "project", None, "A deploy tool.")
    assert base != with_about  # adding About is a change
    assert with_about != edited  # editing About is a change
    # Deterministic: identical inputs (including the About string) hash identically.
    assert with_about == cli._checklist_content_hash(payload, "project", None, "A build tool.")


# --- E1.3 Unit 2: `checklist-push --all [--due]` (the scheduled sweep) ----------------
#
# The --all sweep pushes every checklist-enabled project fail-soft; --due filters on
# cadence and change-gates so an unattended run never re-stamps an untouched card. These
# drive the command end to end with push_checklist captured (no network), asserting on the
# NUMBER of relay pushes (the change-gate's whole point is "no relay call when unchanged").


def _checklist_all_config(tmp_path, *, cadence=None, due_soon_days=None, tasks_file="TODO.md"):
    """A single checklist project `demo`, optionally with a cadence / due_soon_days.

    Why: the --all --due tests vary exactly these two knobs (does the project have a
    cadence? does its wire payload include a due-soon window?); everything else is the
    shared checklist+relay shape.
    """
    cadence_line = f'cadence = "{cadence}"' if cadence else ""
    dsd_line = f"due_soon_days = {due_soon_days}" if due_soon_days is not None else ""
    toml = tmp_path / "orion.toml"
    toml.write_text(
        f"""
        state_db = "state.sqlite3"

        [relay]
        enabled = true
        url = "https://relay.test/ingest"
        token_env_var = "ORION_RELAY_TOKEN"

        [projects.demo]
        repo_path = "{tmp_path.as_posix()}"
        collectors = ["tasks"]
        tasks_file = "{tasks_file}"
        checklist = true
        {cadence_line}
        {dsd_line}

          [[projects.demo.recipients]]
          name = "Alex"
          channel = "discord"
          webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """
    )
    return toml


def test_checklist_push_all_due_pushes_then_not_due(tmp_path, env_and_mocks, capsys):
    """Two back-to-back `--all --due` runs: the first pushes, the second is NOT_DUE.

    Why this matters: this is the cadence gate on the scheduled sweep. A daily-cadence
    project pushed moments ago must be skipped on the next run (so a scheduler firing more
    than once a day doesn't re-push), proven by the relay push count staying at 1.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_checklist_pushes(mp)

    (tmp_path / "TODO.md").write_text("- [ ] Ship it\n", encoding="utf-8")
    toml = _checklist_all_config(tmp_path, cadence="daily")

    assert cli.main(["checklist-push", "--all", "--due", "--config", str(toml)]) == 0
    assert len(pushes) == 1  # first run: due (never pushed) → pushes

    assert cli.main(["checklist-push", "--all", "--due", "--config", str(toml)]) == 0
    assert len(pushes) == 1  # second run: within 23h → NOT_DUE, no new push
    assert "not due" in capsys.readouterr().out


def test_checklist_push_all_due_change_gate_skips_unchanged(tmp_path, env_and_mocks, capsys):
    """A due-but-unchanged project skips NO_CHANGE with NO relay call; an edit re-pushes.

    Why this matters: the honesty guard. The relay stamps updated_at on every push, so an
    unattended run must not re-push identical content and make an untouched card look fresh.
    A no-cadence project is ALWAYS due (Decision 3), so this also proves "always due" stays
    harmless: it pushes only when the content actually changed. No cadence + change-gate is
    the exact combination the scheduled tracker card relies on.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_checklist_pushes(mp)

    todo = tmp_path / "TODO.md"
    todo.write_text("- [ ] Ship it\n", encoding="utf-8")
    toml = _checklist_all_config(tmp_path)  # no cadence → always due

    assert cli.main(["checklist-push", "--all", "--due", "--config", str(toml)]) == 0
    assert len(pushes) == 1  # first push

    assert cli.main(["checklist-push", "--all", "--due", "--config", str(toml)]) == 0
    assert len(pushes) == 1  # unchanged content → NO_CHANGE, no relay call
    assert "no change" in capsys.readouterr().out

    # Edit the checklist: the next --due run detects the changed hash and re-pushes.
    todo.write_text("- [x] Ship it\n- [ ] And more\n", encoding="utf-8")
    assert cli.main(["checklist-push", "--all", "--due", "--config", str(toml)]) == 0
    assert len(pushes) == 2  # content changed → pushes again


def test_checklist_push_all_due_repushes_on_due_soon_days_change(tmp_path, env_and_mocks):
    """A config-only `due_soon_days` change re-pushes under --due (hash covers the wire).

    Why this matters: the change-gate hashes the WIRE payload (items + kind + due_soon_days),
    not the raw tasks_file — so changing only the due-soon window, with identical items,
    still counts as a change and re-pushes. This keeps the relay's due-soon flag current (the
    KI-35 case-2 mitigation) even under change-gating.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_checklist_pushes(mp)

    (tmp_path / "TODO.md").write_text("- [ ] Ship it\n", encoding="utf-8")
    toml = _checklist_all_config(tmp_path)  # no cadence, no due_soon_days
    assert cli.main(["checklist-push", "--all", "--due", "--config", str(toml)]) == 0
    assert len(pushes) == 1

    # Rewrite the SAME project with a due-soon window; items are untouched.
    _checklist_all_config(tmp_path, due_soon_days=14)
    assert cli.main(["checklist-push", "--all", "--due", "--config", str(toml)]) == 0
    assert len(pushes) == 2  # wire payload changed (due_soon_days) → re-push
    assert pushes[1][5] == 14  # the new window rode the push


def test_checklist_push_all_without_due_is_unconditional(tmp_path, env_and_mocks):
    """`--all` without `--due` pushes every run, even when content is unchanged.

    Why this matters: the change-gate is the AUTOMATION surface only. A manual `--all`
    sweep is explicit user intent, so it pushes unconditionally — two identical `--all`
    runs both push (unlike `--all --due`, which would skip the second NO_CHANGE).
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_checklist_pushes(mp)

    (tmp_path / "TODO.md").write_text("- [ ] Ship it\n", encoding="utf-8")
    toml = _checklist_all_config(tmp_path)

    assert cli.main(["checklist-push", "--all", "--config", str(toml)]) == 0
    assert cli.main(["checklist-push", "--all", "--config", str(toml)]) == 0
    assert len(pushes) == 2  # both runs push despite identical content


def test_checklist_push_all_skips_non_checklist_project(tmp_path, env_and_mocks, capsys):
    """`--all` pushes checklist projects and passes over ones with no checklist.

    Why this matters: --all is a sweep, not a per-project assertion — a project without a
    checklist source is skipped (NO_CHECKLIST), not an error that aborts the run. Only the
    checklist-enabled project reaches the relay.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_checklist_pushes(mp)

    (tmp_path / "TODO.md").write_text("- [ ] Ship it\n", encoding="utf-8")
    toml = tmp_path / "orion.toml"
    toml.write_text(
        f"""
        state_db = "state.sqlite3"

        [relay]
        enabled = true
        url = "https://relay.test/ingest"
        token_env_var = "ORION_RELAY_TOKEN"

        [projects.demo]
        repo_path = "{tmp_path.as_posix()}"
        collectors = ["tasks"]
        tasks_file = "TODO.md"
        checklist = true

          [[projects.demo.recipients]]
          name = "Alex"
          channel = "discord"
          webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"

        [projects.reportonly]
        repo_path = "{tmp_path.as_posix()}"
        collectors = ["git"]

          [[projects.reportonly.recipients]]
          name = "Alex"
          channel = "discord"
          webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """
    )

    assert cli.main(["checklist-push", "--all", "--config", str(toml)]) == 0
    assert len(pushes) == 1  # only `demo` (checklist-enabled) pushed
    assert pushes[0][1] == "demo"
    assert "no checklist" in capsys.readouterr().out  # `reportonly` skipped, not errored


def test_checklist_push_all_is_fail_soft_and_exits_1_on_failure(tmp_path, env_and_mocks, capsys):
    """A per-project relay failure is reported, the sweep continues, and exit is 1.

    Why this matters: mirrors the report loop's fail-soft contract — one project's down/
    rejected relay push must not abort the others, but a genuine failure must still surface
    as a non-zero exit for a scheduler. Both projects are attempted; the run exits 1.
    """
    from orion.delivery import DeliveryError

    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    # Every push fails — a down relay / bad token surfaces exactly this.
    mp.setattr(
        cli,
        "push_checklist",
        lambda *a, **k: (_ for _ in ()).throw(DeliveryError("relay down")),
    )

    (tmp_path / "A.md").write_text("- [ ] a\n", encoding="utf-8")
    (tmp_path / "B.md").write_text("- [ ] b\n", encoding="utf-8")
    toml = tmp_path / "orion.toml"
    toml.write_text(
        f"""
        state_db = "state.sqlite3"

        [relay]
        enabled = true
        url = "https://relay.test/ingest"
        token_env_var = "ORION_RELAY_TOKEN"

        [projects.a]
        repo_path = "{tmp_path.as_posix()}"
        collectors = ["tasks"]
        tasks_file = "A.md"
        checklist = true

          [[projects.a.recipients]]
          name = "Alex"
          channel = "discord"
          webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"

        [projects.b]
        repo_path = "{tmp_path.as_posix()}"
        collectors = ["tasks"]
        tasks_file = "B.md"
        checklist = true

          [[projects.b.recipients]]
          name = "Alex"
          channel = "discord"
          webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """
    )

    assert cli.main(["checklist-push", "--all", "--config", str(toml)]) == 1  # a failure occurred
    err = capsys.readouterr().err
    assert err.count("push failed") == 2  # both attempted despite the first failing

    # A failed push records nothing for either project (record-after-success).
    conn = open_state(tmp_path / "state.sqlite3")
    assert get_last_checklist_push(conn, "a") is None
    assert get_last_checklist_push(conn, "b") is None


def test_checklist_push_all_due_usage_errors(tmp_path, env_and_mocks):
    """The flag-combination guards return exit 2 with a clear message.

    Why this matters: --due only means anything as a filter over --all; --watch is a single-
    project foreground loop; and project-vs---all is exclusive. Each misuse is a usage error
    (exit 2), mirroring `report`'s XOR handling — caught before any config/relay work.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    toml = _checklist_all_config(tmp_path)

    # --due without --all
    assert cli.main(["checklist-push", "demo", "--due", "--config", str(toml)]) == 2
    # --watch combined with --all
    assert cli.main(["checklist-push", "--all", "--watch", "--config", str(toml)]) == 2
    # --watch combined with --due
    assert cli.main(["checklist-push", "demo", "--watch", "--due", "--config", str(toml)]) == 2
    # a project name AND --all
    assert cli.main(["checklist-push", "demo", "--all", "--config", str(toml)]) == 2
    # neither a project nor --all
    assert cli.main(["checklist-push", "--config", str(toml)]) == 2


def test_checklist_push_carries_configured_due_soon_days(tmp_path, env_and_mocks):
    """A project's `due_soon_days` rides the /checklist push (E1.2 Unit 3, carrier 2).

    Why this matters: the due-soon window reaches the relay on BOTH checklist carriers;
    this pins the dedicated-push carrier. A project that sets `due_soon_days = 14` must
    pass 14 through push_checklist (the relay then flags due-soon items at 14 days), while
    the default-None case is covered by test_checklist_push_one_shot.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_checklist_pushes(mp)

    (tmp_path / "TODO.md").write_text("- [ ] Ship it\n", encoding="utf-8")
    # A checklist project that also sets the due-soon window.
    toml = tmp_path / "orion.toml"
    toml.write_text(
        f"""
        state_db = "state.sqlite3"

        [relay]
        enabled = true
        url = "https://relay.test/ingest"
        token_env_var = "ORION_RELAY_TOKEN"

        [projects.demo]
        repo_path = "{tmp_path.as_posix()}"
        collectors = ["tasks"]
        tasks_file = "TODO.md"
        checklist = true
        due_soon_days = 14

          [[projects.demo.recipients]]
          name = "Alex"
          channel = "discord"
          webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """
    )

    code = cli.main(["checklist-push", "demo", "--config", str(toml)])
    assert code == 0
    assert len(pushes) == 1
    _url, _project, _checklist, _token, _kind, due_soon_days, _clear, _about, _clear_about = pushes[0]
    assert due_soon_days == 14  # the configured window rides the push


def test_checklist_push_carries_observed_about(tmp_path, env_and_mocks):
    """An `about_file`'s first paragraph rides the /checklist push (Unit 2, carrier 2).

    Why this matters: this is the About band's producer half end to end — config →
    read_about → redact → push_checklist. A project that points `about_file` at a README
    must carry that README's opening prose to the relay under `about`.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_checklist_pushes(mp)

    (tmp_path / "TODO.md").write_text("- [ ] Ship it\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "# Demo\n\nDemo turns activity into progress updates.\n", encoding="utf-8"
    )
    toml = _checklist_config(tmp_path, about_file="README.md")

    code = cli.main(["checklist-push", "demo", "--config", str(toml)])
    assert code == 0
    assert len(pushes) == 1
    _url, _project, _checklist, _token, _kind, _due, _clear, about, clear_about = pushes[0]
    assert about == "Demo turns activity into progress updates."
    assert clear_about is False  # a normal push SETS About, never clears it


def test_checklist_push_redacts_about_before_send(tmp_path, env_and_mocks):
    """A secret in the About source is scrubbed before it rides the push.

    Why this matters: About is user-authored content, so the privacy net must run on it
    like any other outbound text — a token in the README's first paragraph must never
    reach the relay.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_checklist_pushes(mp)

    (tmp_path / "TODO.md").write_text("- [ ] Ship it\n", encoding="utf-8")
    # An AWS-key-shaped secret embedded in the opening paragraph.
    (tmp_path / "README.md").write_text(
        "# Demo\n\nDeploy key AKIAIOSFODNN7EXAMPLE powers the pipeline.\n", encoding="utf-8"
    )
    toml = _checklist_config(tmp_path, about_file="README.md")

    code = cli.main(["checklist-push", "demo", "--config", str(toml)])
    assert code == 0
    _url, _project, _checklist, _token, _kind, _due, _clear, about, _clear_about = pushes[0]
    assert about is not None
    assert "AKIAIOSFODNN7EXAMPLE" not in about  # the secret was scrubbed before the wire


def test_checklist_push_clear_about_sends_the_clear(tmp_path, env_and_mocks):
    """`--clear-about` sends the explicit clear instead of the observed value.

    Why this matters: mirrors --clear-due-soon-days — removing about_file from config
    never clears the relay's stored About (KI-35), so clearing is a deliberate one-shot
    act that puts clear_about=True (and no value) on the wire.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_checklist_pushes(mp)

    (tmp_path / "TODO.md").write_text("- [ ] Ship it\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Demo\n\nSome About.\n", encoding="utf-8")
    toml = _checklist_config(tmp_path, about_file="README.md")

    code = cli.main(["checklist-push", "demo", "--config", str(toml), "--clear-about"])
    assert code == 0
    _url, _project, _checklist, _token, _kind, _due, _clear, about, clear_about = pushes[0]
    assert clear_about is True
    assert about is None  # a clear puts None (→ explicit null) on the wire, not the value


def test_checklist_push_clear_about_rejected_with_all(tmp_path, env_and_mocks):
    """`--clear-about` is single-project only; combining it with --all is a usage error.

    Why this matters: clearing is a deliberate one-shot act on ONE project, never
    something a sweep repeats — the same guard --clear-due-soon-days has (exit 2).
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    _capture_checklist_pushes(mp)
    toml = _checklist_config(tmp_path)

    code = cli.main(["checklist-push", "--all", "--config", str(toml), "--clear-about"])
    assert code == 2  # usage error, before any push


def test_checklist_push_works_for_tracker_only_project(tmp_path, env_and_mocks):
    """`checklist-push` reads a tracker_file (no tasks_file) and pushes redacted items.

    Why this matters: E2 Inc 2.6 made the tracker a second checklist source. A
    tracker-only project (the applications use case: no git, no tasks) must push its
    status-aware checklist through the SAME redaction + relay path. We assert the
    application item carries its status text, a table row surfaces as open, and a secret
    in a row is scrubbed before it leaves the machine.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_checklist_pushes(mp)

    (tmp_path / "to_do.md").write_text(
        "## 1. Claude Corps Fellow (job)\n"
        "- **Status:** Submitted\n"
        "\n"
        "## Non-Application To-Do\n"
        "\n"
        "| # | Task | Deadline |\n"
        "|---|------|----------|\n"
        "| 1 | Rotate AKIAIOSFODNN7EXAMPLE | Sun, Jun 14 |\n",
        encoding="utf-8",
    )
    toml = tmp_path / "orion.toml"
    toml.write_text(
        f"""
        state_db = "state.sqlite3"

        [relay]
        enabled = true
        url = "https://relay.test/ingest"
        token_env_var = "ORION_RELAY_TOKEN"

        [projects.apps]
        repo_path = "{tmp_path.as_posix()}"
        collectors = ["tracker"]
        tracker_file = "to_do.md"
        checklist = true

          [[projects.apps.recipients]]
          name = "Placeholder"
          channel = "discord"
          webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """
    )

    code = cli.main(["checklist-push", "apps", "--config", str(toml)])
    assert code == 0
    assert len(pushes) == 1

    _url, project, checklist, _token, _kind, _due_soon, _clear, _about, _clear_about = pushes[0]
    assert project == "apps"
    # The application item carries its status in the text and is done (Submitted); it also
    # emits the bare title as the stable forward-store `key` (Unit 3), the "Applications"
    # milestone `group` (Unit 5), and the structured `status` field (E2 Inc 4, gap-8).
    assert checklist[0] == {
        "text": "Claude Corps Fellow (job) - Submitted",
        "done": True,
        "key": "Claude Corps Fellow (job)",
        "group": "Applications",
        "status": "submitted",
    }
    # The table row is an open item, with its secret scrubbed (privacy net holds here).
    assert checklist[1]["done"] is False
    assert "AKIAIOSFODNN7EXAMPLE" not in checklist[1]["text"]


def test_checklist_push_carries_item_deadline_through_redaction(tmp_path, env_and_mocks):
    """A tracker deadline + milestone group ride the /checklist payload through redaction.

    Why this matters: this pins the end-to-end local path for the per-item forward fields.
    The deadline (Unit 1), stable key (Unit 3), milestone group (Unit 5), and structured
    status (E2 Inc 4) are each parsed onto the item, carried THROUGH the redaction rebuild
    (which reconstructs each item and would otherwise drop a new field), and emitted on the
    wire. The relay derives at-risk, milestones, and the in_progress indicator from them; if
    any were lost here, nothing downstream could surface it.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_checklist_pushes(mp)

    (tmp_path / "to_do.md").write_text(
        "## 1. Claude Corps Fellow (job)\n"
        "- **Status:** In progress\n"
        "- **Deadline:** July 17, 2026\n",
        encoding="utf-8",
    )
    toml = tmp_path / "orion.toml"
    toml.write_text(
        f"""
        state_db = "state.sqlite3"

        [relay]
        enabled = true
        url = "https://relay.test/ingest"
        token_env_var = "ORION_RELAY_TOKEN"

        [projects.apps]
        repo_path = "{tmp_path.as_posix()}"
        collectors = ["tracker"]
        tracker_file = "to_do.md"
        checklist = true

          [[projects.apps.recipients]]
          name = "Placeholder"
          channel = "discord"
          webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """
    )

    code = cli.main(["checklist-push", "apps", "--config", str(toml)])
    assert code == 0

    _url, _project, checklist, _token, _kind, _due_soon, _clear, _about, _clear_about = pushes[0]
    assert checklist[0] == {
        "text": "Claude Corps Fellow (job) - In progress",
        "done": False,
        "due_date": "2026-07-17",
        "key": "Claude Corps Fellow (job)",
        "group": "Applications",
        "status": "in_progress",
    }


def test_checklist_push_requires_checklist_enabled(tmp_path, env_and_mocks):
    """`checklist-push` on a project without `checklist = true` errors, pushes nothing.

    Why this matters: the command is opt-in like the report-path checklist; a project
    that has not enabled it should get a clear, fixable message, not a silent push.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_checklist_pushes(mp)
    (tmp_path / "TODO.md").write_text("- [ ] Something\n", encoding="utf-8")
    toml = _checklist_config(tmp_path, checklist=False)

    code = cli.main(["checklist-push", "demo", "--config", str(toml)])
    assert code == 1
    assert pushes == []


# --- S2.2 U3: the About-only push (a project with no checklist) ----------------------


def test_checklist_push_sends_about_alone_for_a_checklist_less_project(
    tmp_path, env_and_mocks, capsys
):
    """A project with no `checklist` but an `about_file` pushes its About and NO checklist.

    Why this matters: this is the producer half of the About-carrier decoupling. Before it,
    About only rode a checklist or a report, so a project with neither could never get one
    onto the dashboard — the carrier demanded an unrelated field. The load-bearing assertion
    is `checklist is None`, not an empty list: None makes the transport omit the key, which
    tells the relay to leave any stored checklist alone. An empty list would claim it is now
    empty.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_checklist_pushes(mp)

    (tmp_path / "README.md").write_text(
        "# barebones\n\nA small experiment with no task list.\n", encoding="utf-8"
    )
    toml = _checklist_config(tmp_path, checklist=False, about_file="README.md")

    code = cli.main(["checklist-push", "demo", "--config", str(toml)])
    assert code == 0
    assert len(pushes) == 1

    _url, project, checklist, _token, _kind, _dsd, _clear, about, _clear_about = pushes[0]
    assert project == "demo"
    assert checklist is None  # omitted from the wire — nothing claimed about the checklist
    assert about == "A small experiment with no task list."
    # The output says what was actually sent, not "0 items".
    out = capsys.readouterr().out
    assert "Pushed About" in out and "no checklist" in out


def test_checklist_push_still_errors_when_there_is_neither_checklist_nor_about(
    tmp_path, env_and_mocks
):
    """No `checklist = true` and no `about_file` is still a clean error, pushing nothing.

    Why this matters: the About-only path must not turn a genuinely unconfigured project
    into a silent success. The message names both fixable options.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_checklist_pushes(mp)
    toml = _checklist_config(tmp_path, checklist=False)  # no about_file either

    code = cli.main(["checklist-push", "demo", "--config", str(toml)])
    assert code == 1
    assert pushes == []


def test_checklist_true_with_no_source_file_is_still_a_hard_error(tmp_path, env_and_mocks):
    """`checklist = true` with nothing to read from stays loud (unchanged by U3).

    Why this matters: that combination is a real misconfiguration — the user asked for a
    checklist and there is no file behind it. Relaxing the About case must not quietly
    swallow this one into an About-only push.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_checklist_pushes(mp)

    toml = tmp_path / "orion.toml"
    toml.write_text(
        f"""
        state_db = "state.sqlite3"

        [relay]
        enabled = true
        url = "https://relay.test/ingest"
        token_env_var = "ORION_RELAY_TOKEN"

        [projects.demo]
        repo_path = "{tmp_path.as_posix()}"
        collectors = ["git"]
        checklist = true
        about_file = "README.md"
        """
    )
    (tmp_path / "README.md").write_text("# demo\n\nA thing.\n", encoding="utf-8")

    code = cli.main(["checklist-push", "demo", "--config", str(toml)])
    assert code == 1
    assert pushes == []


def test_checklist_content_hash_separates_no_checklist_from_an_empty_one():
    """A None checklist hashes differently from an empty list.

    Why this matters: "I am not talking about the checklist" (the About-only push) and "the
    checklist is empty" are different claims. If they hashed the same, a project switching
    between the two would slip past the `--all --due` change gate as "no change".
    """
    absent = cli._checklist_content_hash(None, "project", None, "A tool.")
    empty = cli._checklist_content_hash([], "project", None, "A tool.")
    assert absent != empty
    # Still deterministic on the new input.
    assert absent == cli._checklist_content_hash(None, "project", None, "A tool.")


def test_checklist_push_requires_relay_enabled(tmp_path, env_and_mocks):
    """`checklist-push` with no enabled [relay] errors, pushes nothing.

    Why this matters: the checklist push targets the dashboard relay; without one
    enabled there is nowhere to push, so it must fail clearly rather than no-op.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_checklist_pushes(mp)
    (tmp_path / "TODO.md").write_text("- [ ] Something\n", encoding="utf-8")
    toml = _checklist_config(tmp_path, relay_enabled=False)

    code = cli.main(["checklist-push", "demo", "--config", str(toml)])
    assert code == 1
    assert pushes == []


def test_watch_tick_pushes_on_change_and_skips_when_unchanged(tmp_path, env_and_mocks):
    """_watch_tick pushes when the checklist changed and skips when it did not.

    Why this matters: this is the watch loop's core rule, made testable without an
    infinite loop. Content-compare means a tick with no real change performs no push
    (no relay traffic), while an actual edit triggers exactly one push of the new state.
    """
    mp = env_and_mocks["monkeypatch"]
    pushes = _capture_checklist_pushes(mp)
    todo = tmp_path / "TODO.md"
    todo.write_text("- [ ] First\n", encoding="utf-8")
    toml = _checklist_config(tmp_path)
    project = get_project(load_config(toml), "demo")
    relay_cfg = load_config(toml).relay

    # First tick (no prior push): the current state is pushed. `demo` has no about_file,
    # so the About the tick carries is None (the third tuple element).
    last, about, pushed = cli._watch_tick(project, relay_cfg, "tok", None)
    assert pushed is True
    assert about is None
    assert last == [{"text": "First", "done": False}]
    assert len(pushes) == 1

    # Second tick, file unchanged: no push, same baseline returned.
    last, about, pushed = cli._watch_tick(project, relay_cfg, "tok", last)
    assert pushed is False
    assert len(pushes) == 1  # unchanged → no new push

    # Edit the file: the next tick pushes the new checklist.
    todo.write_text("- [x] First\n- [ ] Second\n", encoding="utf-8")
    last, about, pushed = cli._watch_tick(project, relay_cfg, "tok", last)
    assert pushed is True
    assert last == [{"text": "First", "done": True}, {"text": "Second", "done": False}]
    assert len(pushes) == 2


def test_relay_error_is_non_fatal(tmp_path, env_and_mocks):
    """A relay push failure is reported but does not fail the run or block state.

    Why this matters: the dashboard is a secondary surface. If the relay is down,
    the run that already delivered to Discord must still exit 0 with state advanced
    — proven here by the immediate re-run reporting no activity.
    """
    from orion.delivery import DeliveryError

    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    # The relay sender raises — exactly what a down relay / bad token surfaces as.
    mp.setattr(cli, "relay_push", lambda blob_json, url, token: (_ for _ in ()).throw(DeliveryError("relay down")))
    repo = _make_repo(tmp_path)
    toml = _write_relay_config(tmp_path, repo)

    _answer(mp, "y")
    code = cli.main(["report", "demo", "--config", str(toml)])
    assert code == 0                          # relay failure did NOT fail the run
    assert len(env_and_mocks["sent"]) == 1    # the report was still delivered

    # State advanced despite the relay error: a second run finds nothing new.
    env_and_mocks["sent"].clear()
    code2 = cli.main(["report", "demo", "--config", str(toml)])
    assert code2 == 0
    assert env_and_mocks["sent"] == []


# --- relay-backfill (push already-sent reports onto the relay, chat-silent) -----------
#
# relay-backfill lands an already-sent report on the relay's append-only history WITHOUT
# re-delivering to any chat recipient (the whole difference from `intake`). These drive it
# end to end with cli.relay_push captured (no network), asserting the pushed blob, that
# redaction ran, and — critically — that NO chat send happens.

_BACKFILL_TS = "2026-07-17T09:15:54+00:00"


def _backfill_config(tmp_path):
    """A relay-enabled `demo` project (reuses the shared relay-config + repo helpers)."""
    return _write_relay_config(tmp_path, _make_repo(tmp_path))


def test_relay_backfill_pushes_blob_chat_silent(tmp_path, env_and_mocks):
    """`relay-backfill --yes` pushes the report blob to the relay and sends NO chat message.

    Why this matters: this is the whole point vs `intake` — the report lands on the
    dashboard's history at its original timestamp, but no Discord/Slack message goes out
    (the reports were already delivered once). We assert the pushed blob's fields and that
    the chat sender was never called.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_relay(mp)
    body = tmp_path / "report1.md"
    body.write_text("Shipped the login flow and fixed two bugs.\n", encoding="utf-8")
    toml = _backfill_config(tmp_path)

    code = cli.main([
        "relay-backfill", "demo", "--generated-at", _BACKFILL_TS,
        "--body-file", str(body), "--yes", "--config", str(toml),
    ])
    assert code == 0
    assert len(pushes) == 1
    blob_json, url, token = pushes[0]
    assert url == "https://relay.test/ingest"
    assert token == "relay-secret"
    blob = json.loads(blob_json)
    assert blob["project"] == "demo"
    assert blob["body"].startswith("Shipped the login flow")
    assert blob["generated_at"] == "2026-07-17T09:15:54+00:00"
    assert blob["participants"] == ["Alex"]         # from the project's recipients
    assert blob["lane"] == "structured"             # no LLM runs in a backfill
    assert blob["sections"] == []                   # renders as one untitled section (intake-style)
    # Chat-silent: the report was already delivered — backfill must NOT re-send it.
    assert env_and_mocks["sent"] == []


def test_relay_backfill_redacts_body(tmp_path, env_and_mocks):
    """A secret in the supplied body is scrubbed before it reaches the relay.

    Why this matters: a historical report body can carry a secret (a token pasted into a
    report) just like a live one — redaction is the non-negotiable net on this lane too,
    even though the human preview is skippable with --yes.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_relay(mp)
    body = tmp_path / "r.md"
    body.write_text("Rotated the key AKIAIOSFODNN7EXAMPLE today.\n", encoding="utf-8")
    toml = _backfill_config(tmp_path)

    code = cli.main([
        "relay-backfill", "demo", "--generated-at", _BACKFILL_TS,
        "--body-file", str(body), "--yes", "--config", str(toml),
    ])
    assert code == 0
    assert "AKIAIOSFODNN7EXAMPLE" not in json.loads(pushes[0][0])["body"]


def test_relay_backfill_normalizes_naive_timestamp(tmp_path, env_and_mocks):
    """A naive --generated-at is treated as UTC and normalized to a tz-aware ISO string.

    Why this matters: the relay orders cards by generated_at; a naive timestamp must not
    reach the wire ambiguously. We pass a naive value and assert the blob carries +00:00.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_relay(mp)
    body = tmp_path / "r.md"
    body.write_text("Body.\n", encoding="utf-8")
    toml = _backfill_config(tmp_path)

    code = cli.main([
        "relay-backfill", "demo", "--generated-at", "2026-07-17T09:15:54",
        "--body-file", str(body), "--yes", "--config", str(toml),
    ])
    assert code == 0
    assert json.loads(pushes[0][0])["generated_at"] == "2026-07-17T09:15:54+00:00"


def test_relay_backfill_bad_timestamp_is_usage_error(tmp_path, env_and_mocks):
    """A malformed --generated-at is a usage error (exit 2), nothing pushed.

    Why this matters: the timestamp is required and must be valid ISO 8601; a typo should
    fail fast and clearly rather than push a report with a wrong/absent time.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_relay(mp)
    body = tmp_path / "r.md"
    body.write_text("Body.\n", encoding="utf-8")
    toml = _backfill_config(tmp_path)

    code = cli.main([
        "relay-backfill", "demo", "--generated-at", "last tuesday",
        "--body-file", str(body), "--yes", "--config", str(toml),
    ])
    assert code == 2
    assert pushes == []


def test_relay_backfill_empty_body_refused(tmp_path, env_and_mocks):
    """A whitespace-only body is refused (exit 1), nothing pushed."""
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_relay(mp)
    body = tmp_path / "r.md"
    body.write_text("   \n", encoding="utf-8")
    toml = _backfill_config(tmp_path)

    code = cli.main([
        "relay-backfill", "demo", "--generated-at", _BACKFILL_TS,
        "--body-file", str(body), "--yes", "--config", str(toml),
    ])
    assert code == 1
    assert pushes == []


def test_relay_backfill_requires_enabled_relay(tmp_path, env_and_mocks):
    """With the relay disabled, backfill errors (exit 1) — it has nowhere to push."""
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_relay(mp)
    body = tmp_path / "r.md"
    body.write_text("Body.\n", encoding="utf-8")
    toml = _write_relay_config(tmp_path, _make_repo(tmp_path), enabled=False)

    code = cli.main([
        "relay-backfill", "demo", "--generated-at", _BACKFILL_TS,
        "--body-file", str(body), "--yes", "--config", str(toml),
    ])
    assert code == 1
    assert pushes == []


def test_relay_backfill_preview_abort_pushes_nothing(tmp_path, env_and_mocks):
    """Without --yes, declining the preview aborts cleanly (exit 0) and pushes nothing.

    Why this matters: preview-before-send is the default guard (and the idempotence guard,
    since the relay history is append-only). A 'no' at the prompt must push nothing.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_relay(mp)
    _answer(mp, "n")  # decline the confirm
    body = tmp_path / "r.md"
    body.write_text("Body.\n", encoding="utf-8")
    toml = _backfill_config(tmp_path)

    code = cli.main([
        "relay-backfill", "demo", "--generated-at", _BACKFILL_TS,
        "--body-file", str(body), "--config", str(toml),
    ])
    assert code == 0
    assert pushes == []


def test_relay_backfill_push_failure_exits_1(tmp_path, env_and_mocks):
    """A relay push failure is fatal (exit 1) — unlike the report path's fail-soft push.

    Why this matters: landing the report on the dashboard IS the point of backfill, so a
    down/rejected relay must surface as a non-zero exit, not a swallowed warning.
    """
    from orion.delivery import DeliveryError

    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    mp.setattr(
        cli, "relay_push",
        lambda *a, **k: (_ for _ in ()).throw(DeliveryError("relay down")),
    )
    body = tmp_path / "r.md"
    body.write_text("Body.\n", encoding="utf-8")
    toml = _backfill_config(tmp_path)

    code = cli.main([
        "relay-backfill", "demo", "--generated-at", _BACKFILL_TS,
        "--body-file", str(body), "--yes", "--config", str(toml),
    ])
    assert code == 1


def test_relay_backfill_reads_body_from_stdin(tmp_path, env_and_mocks):
    """With no --body-file, the body is read from stdin (a shell pipe / paste)."""
    import io

    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    pushes = _capture_relay(mp)
    mp.setattr("sys.stdin", io.StringIO("Piped report body.\n"))
    toml = _backfill_config(tmp_path)

    code = cli.main([
        "relay-backfill", "demo", "--generated-at", _BACKFILL_TS, "--yes",
        "--config", str(toml),
    ])
    assert code == 0
    assert json.loads(pushes[0][0])["body"].startswith("Piped report body")


# --- relay-serve CLI adapter (C1, CP8) ----------------------------------------
#
# These pin the `orion relay-serve` command's argument plumbing and its secret
# handling WITHOUT actually starting a server: _load_relay_serve is monkeypatched
# to return a recorder, so serve() is never really called (it would block forever).


def test_relay_serve_dispatches_with_resolved_args(tmp_path, monkeypatch):
    """`relay-serve` reads the token from .env and calls serve() with parsed args.

    Why this matters: this is the whole job of the CLI adapter — turn flags + the
    ingest token + the optional view secret into a serve() call. We patch
    _load_relay_serve so nothing actually binds a socket, and assert the
    host/port/db/token/view-token reached serve() as resolved.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)  # ignore real .env
    monkeypatch.setenv("ORION_RELAY_TOKEN", "tok-123")
    monkeypatch.setenv("ORION_RELAY_VIEW_TOKEN", "view-xyz")
    # A view secret now gates the dashboard with sessions, so cmd_relay_serve fails
    # closed unless the session signing key + user pepper are also set. Provide them
    # so this test reaches serve() (the fail-closed path is its own test below).
    monkeypatch.setenv("ORION_RELAY_SESSION_KEY", "session-signing-key")
    monkeypatch.setenv("ORION_RELAY_USER_PEPPER", "user-pepper")
    calls = []
    # The recorder accepts the full serve() signature, including the trailing
    # display_tz the timezone flag threads in (see test below) and the auth= kwarg
    # the session config now rides in on (**k); a fixed-arity stub would break the
    # moment a new positional or keyword is added.
    monkeypatch.setattr(
        cli,
        "_load_relay_serve",
        lambda: (
            lambda host, port, db_path, token, view_token, require_view_auth, display_tz, **k: calls.append(
                (host, port, db_path, token, view_token, require_view_auth, display_tz)
            )
        ),
    )

    db = tmp_path / "relay.sqlite3"
    code = cli.main(
        [
            "relay-serve",
            "--host", "127.0.0.1",
            "--port", "9999",
            "--db", str(db),
            "--config", str(tmp_path / "orion.toml"),
        ]
    )
    assert code == 0
    assert len(calls) == 1
    host, port, db_path, token, view_token, require_view_auth, display_tz = calls[0]
    assert host == "127.0.0.1"
    assert port == 9999
    assert db_path == db
    assert token == "tok-123"
    assert view_token == "view-xyz"  # resolved from ORION_RELAY_VIEW_TOKEN
    assert require_view_auth is False  # flag absent -> default off
    # --timezone omitted -> the Pacific default, validated into a ZoneInfo.
    assert display_tz == ZoneInfo("America/Los_Angeles")


def test_relay_serve_require_view_auth_flag_threads(tmp_path, monkeypatch):
    """`--require-view-auth` reaches serve() as True.

    Why this matters: the proxy-topology safety switch must actually thread from the
    CLI flag through to serve()/the guard — a flag that parses but gets dropped would
    silently leave the dashboard unprotected behind a proxy (the exact KI-18 footgun
    this closes).
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_TOKEN", "tok-123")
    monkeypatch.setenv("ORION_RELAY_VIEW_TOKEN", "view-xyz")
    # A gated dashboard needs the session secrets too (see the fail-closed test below).
    monkeypatch.setenv("ORION_RELAY_SESSION_KEY", "session-signing-key")
    monkeypatch.setenv("ORION_RELAY_USER_PEPPER", "user-pepper")
    seen = []
    # **k absorbs the auth= kwarg the session config now rides in on.
    monkeypatch.setattr(cli, "_load_relay_serve", lambda: (lambda *a, **k: seen.append(a)))

    code = cli.main(
        [
            "relay-serve",
            "--host", "127.0.0.1",
            "--require-view-auth",
            "--config", str(tmp_path / "orion.toml"),
        ]
    )
    assert code == 0
    # require_view_auth is the second-to-last positional arg (display_tz now trails it);
    # auth rides in as a keyword, so it does not shift the positional tail.
    assert seen and seen[0][-2] is True


def test_parse_showcase_projects_splits_name_and_optional_blurb():
    """NAME[:blurb] flags parse to ordered (name, blurb) pairs; first colon only.

    Why this matters: the curated allowlist + its public copy come from these flags, so the
    parse rules are a contract — order is preserved (it is the display order), a missing
    blurb is "" (the serializer then falls back to the headline), a blurb may itself contain
    colons, and a blank name is dropped rather than producing a nameless card.
    """
    pairs = cli._parse_showcase_projects(
        [
            "orion:A tracker: observed, not authored",  # blurb keeps its inner colon
            "sample-app",  # no blurb -> ""
            "  ",  # blank -> dropped
            " spaced : trimmed ",  # both sides stripped
        ]
    )
    assert pairs == (
        ("orion", "A tracker: observed, not authored"),
        ("sample-app", ""),
        ("spaced", "trimmed"),
    )
    assert cli._parse_showcase_projects(None) == ()  # flag never passed


def test_relay_serve_showcase_flags_build_a_showcase_config(tmp_path, monkeypatch):
    """`--showcase` + repeated `--showcase-project` reach serve() as a ShowcaseConfig.

    Why this matters: the public surface is opt-in and curated entirely from the CLI (the
    relay does not read orion.toml), so the flags must thread through to serve()'s showcase=
    kwarg as the enabled flag plus the ordered allowlist — a flag that parsed but got
    dropped would leave the Showcase silently empty or off.
    """
    from relay.server import ShowcaseConfig

    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_TOKEN", "tok-123")
    seen = []
    monkeypatch.setattr(cli, "_load_relay_serve", lambda: (lambda *a, **k: seen.append(k)))

    code = cli.main(
        [
            "relay-serve",
            "--host", "127.0.0.1",
            "--showcase",
            "--showcase-project", "orion:A local-first tracker.",
            "--showcase-project", "sample-app",
            "--config", str(tmp_path / "orion.toml"),
        ]
    )
    assert code == 0
    showcase = seen[0]["showcase"]
    assert isinstance(showcase, ShowcaseConfig)
    assert showcase.enabled is True
    assert showcase.projects == (
        ("orion", "A local-first tracker."),
        ("sample-app", ""),
    )


def test_relay_serve_showcase_defaults_off(tmp_path, monkeypatch):
    """Without the flags, serve() gets a disabled ShowcaseConfig (the no-op default)."""
    from relay.server import ShowcaseConfig

    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_TOKEN", "tok-123")
    seen = []
    monkeypatch.setattr(cli, "_load_relay_serve", lambda: (lambda *a, **k: seen.append(k)))

    code = cli.main(["relay-serve", "--config", str(tmp_path / "orion.toml")])
    assert code == 0
    assert seen[0]["showcase"] == ShowcaseConfig(enabled=False, projects=())


def test_relay_serve_guard_error_is_a_clean_exit(tmp_path, monkeypatch):
    """A fail-closed guard ValueError from serve() becomes a clean exit 1.

    Why this matters: binding non-loopback without a view secret must fail with a
    clear, actionable message and exit 1 — not a raw traceback. We make the stubbed
    serve() raise the guard's ValueError and assert the CLI catches it.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_TOKEN", "tok-123")
    monkeypatch.delenv("ORION_RELAY_VIEW_TOKEN", raising=False)

    def _raise_guard(*_a, **_k):  # **_k absorbs the auth= kwarg
        raise ValueError("refusing to bind non-loopback host '0.0.0.0' ...")

    monkeypatch.setattr(cli, "_load_relay_serve", lambda: _raise_guard)
    code = cli.main(
        ["relay-serve", "--host", "0.0.0.0", "--config", str(tmp_path / "orion.toml")]
    )
    assert code == 1


def test_relay_serve_without_session_secrets_when_gated_is_clean_error(tmp_path, monkeypatch):
    """A gated dashboard with no session secrets fails closed (exit 1, never serves).

    Why this matters: once a view secret (or admin token) is set, the dashboard runs the
    cookie-session login, which is impossible without ORION_RELAY_SESSION_KEY and
    ORION_RELAY_USER_PEPPER. Rather than boot a login that could never succeed, the CLI
    must fail closed with a clear error before binding — the Codex-hardened
    independent-secrets requirement enforced at the CLI seam. We set a view secret but
    omit the session secrets and prove serve() is never reached.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_TOKEN", "tok-123")
    monkeypatch.setenv("ORION_RELAY_VIEW_TOKEN", "view-xyz")  # gates the dashboard
    monkeypatch.delenv("ORION_RELAY_SESSION_KEY", raising=False)
    monkeypatch.delenv("ORION_RELAY_USER_PEPPER", raising=False)
    served = []
    monkeypatch.setattr(cli, "_load_relay_serve", lambda: (lambda *a, **k: served.append(a)))

    code = cli.main(
        ["relay-serve", "--host", "127.0.0.1", "--config", str(tmp_path / "orion.toml")]
    )
    assert code == 1
    assert served == []  # fail-closed: never started serving


def test_relay_serve_missing_token_is_clean_error_and_never_serves(tmp_path, monkeypatch):
    """A missing ingest token fails cleanly (exit 1) and never starts the server.

    Why this matters: a relay with no token would 401 every push; catching the gap
    here turns it into a clear SecretsError naming the variable, before any socket is
    bound. We prove serve() is never reached.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)  # don't load a real .env
    monkeypatch.delenv("ORION_RELAY_TOKEN", raising=False)
    served = []
    monkeypatch.setattr(cli, "_load_relay_serve", lambda: (lambda *a: served.append(a)))

    code = cli.main(["relay-serve", "--config", str(tmp_path / "orion.toml")])
    assert code == 1
    assert served == []  # never started serving


def test_relay_serve_timezone_flag_threads_a_zoneinfo(tmp_path, monkeypatch):
    """`--timezone <zone>` validates into a ZoneInfo and reaches serve() as that zone.

    Why this matters: the relay does not read orion.toml, so the flag is the ONLY way
    to set the dashboard's display zone (KI-20 follow-up). A flag that parses but is
    dropped, or one passed through as a bare string, would silently leave timestamps
    in Pacific. We pass a non-default zone and assert the exact ZoneInfo reaches
    serve()'s final positional argument.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_TOKEN", "tok-123")
    # This test exercises an UNGATED relay (only the timezone flag matters), so it must
    # control its own env: an earlier non-mocked test can load the project's real .env
    # (cwd-upward search) into os.environ, leaking ORION_RELAY_VIEW_TOKEN, which would
    # gate the dashboard and trip the session-secret fail-closed guard. Clear the
    # multi-party vars so this test stays hermetic regardless of run order.
    for _var in (
        "ORION_RELAY_VIEW_TOKEN",
        "ORION_RELAY_ADMIN_TOKEN",
        "ORION_RELAY_SESSION_KEY",
        "ORION_RELAY_USER_PEPPER",
    ):
        monkeypatch.delenv(_var, raising=False)
    seen = []
    # **k absorbs the auth= kwarg; display_tz stays the last positional arg.
    monkeypatch.setattr(cli, "_load_relay_serve", lambda: (lambda *a, **k: seen.append(a)))

    code = cli.main(
        [
            "relay-serve",
            "--host", "127.0.0.1",
            "--timezone", "Europe/London",
            "--config", str(tmp_path / "orion.toml"),
        ]
    )
    assert code == 0
    # display_tz is serve()'s last positional arg; it must be the constructed zone,
    # not the raw string, so the renderer receives a ready-to-use ZoneInfo.
    assert seen and seen[0][-1] == ZoneInfo("Europe/London")


def test_relay_serve_invalid_timezone_is_a_clean_error_and_never_serves(tmp_path, monkeypatch):
    """An unknown --timezone fails cleanly (exit 1) and never starts the server.

    Why this matters: a typo'd zone must surface as a clear, named error — mirroring
    config.py's _parse_display_timezone — rather than a raw ZoneInfoNotFoundError
    traceback or (worse) a server that starts and renders confusing times. We prove
    the exit code is 1 and that serve() is never reached.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_TOKEN", "tok-123")
    served = []
    monkeypatch.setattr(cli, "_load_relay_serve", lambda: (lambda *a: served.append(a)))

    code = cli.main(
        [
            "relay-serve",
            "--timezone", "Mars/Olympus_Mons",  # not a real IANA zone
            "--config", str(tmp_path / "orion.toml"),
        ]
    )
    assert code == 1
    assert served == []  # validation failed before the server was launched


# --- baseline + ORION_CONFIG (unit 4 friction fixes) --------------------------


def test_baseline_skips_history_then_reports_only_new_activity(tmp_path, env_and_mocks):
    """`baseline` marks current state as reported without sending; later reports cover
    only NEW activity.

    Why this matters: a new project's first `report` would otherwise dump the ENTIRE git
    history. `baseline` sets the marker to HEAD and delivers nothing, so the immediately-
    following report finds no activity — and a fresh commit afterward is reported on its
    own (proving the baseline drew the line at "now," not "never").
    """
    mp = env_and_mocks["monkeypatch"]
    repo = _make_repo(tmp_path)
    toml = _write_config(tmp_path, repo)

    code = cli.main(["baseline", "demo", "--config", str(toml)])
    assert code == 0
    assert env_and_mocks["sent"] == []        # baseline delivers nothing

    # History is baselined away -> an immediate report is a no-op.
    _answer(mp, "y")
    assert cli.main(["report", "demo", "--config", str(toml)]) == 0
    assert env_and_mocks["sent"] == []

    # A new commit AFTER the baseline IS reported (only the new activity).
    (repo / "feature.py").write_text("def f():\n    return 2\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "Change feature")
    assert cli.main(["report", "demo", "--config", str(toml)]) == 0
    assert len(env_and_mocks["sent"]) == 1    # only the post-baseline commit


# --- `orion discussions pull/reply` — the developer's loop (E2 Inc 5, Unit 3b) ---
# cli.pull_discussions / cli.post_discussion are monkeypatched so the command logic
# (watermark read/advance, output, the reply payload, errors) is tested with no relay
# running. This is the single CLI conversation surface (KI-28 Stage 2 retired `comments`).


def _capture_pull_discussions(mp, response):
    """Monkeypatch cli.pull_discussions to record (project, since_id) and return canned data."""
    calls: list[tuple[str, int]] = []

    def fake_pull(relay_url, token, project, since_id, **_kw):
        calls.append((project, since_id))
        return response

    mp.setattr(cli, "pull_discussions", fake_pull)
    return calls


def _capture_post_discussion(mp, result=None):
    """Monkeypatch cli.post_discussion to record (project, body, author) and return an id."""
    calls: list[tuple[str, str, str]] = []

    def fake_post(relay_url, token, project, body, author, **_kw):
        calls.append((project, body, author))
        return result if result is not None else {"id": 1}

    mp.setattr(cli, "post_discussion", fake_post)
    return calls


def _two_discussions():
    """A canned thread: a supervisor message and the developer's reply (latest_id = 2)."""
    return {
        "discussions": [
            {"id": 1, "project": "demo", "author_id": 7, "author_name": "Supervisor A",
             "role": "supervisor", "body": "How's auth?",
             "created_at": "2026-06-28T19:30:00+00:00"},
            {"id": 2, "project": "demo", "author_id": None, "author_name": "Teammate B",
             "role": "developer", "body": "Landed.",
             "created_at": "2026-06-28T20:00:00+00:00"},
        ],
        "latest_id": 2,
    }


def test_discussions_pull_shows_thread_and_advances_watermark(tmp_path, monkeypatch, capsys):
    """A default pull renders the thread (with role tags), then advances the watermark.

    Why this matters: the unread cursor is the feature, exactly as for comments. First pull
    starts at 0; after it the watermark advances so the second starts at latest_id (2).
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_TOKEN", "relay-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_config(tmp_path, repo)
    calls = _capture_pull_discussions(monkeypatch, _two_discussions())

    assert cli.main(["discussions", "pull", "demo", "--config", str(toml)]) == 0
    out = capsys.readouterr().out
    assert "[supervisor] Supervisor A" in out and "How's auth?" in out
    assert "[developer] Teammate B" in out and "Landed." in out

    assert cli.main(["discussions", "pull", "demo", "--config", str(toml)]) == 0
    assert [since for _project, since in calls] == [0, 2]


def test_discussions_pull_all_does_not_advance(tmp_path, monkeypatch, capsys):
    """`--all` pulls from 0 and leaves the cursor untouched (the explicit re-read)."""
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_TOKEN", "relay-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_config(tmp_path, repo)
    calls = _capture_pull_discussions(monkeypatch, _two_discussions())

    cli.main(["discussions", "pull", "demo", "--config", str(toml)])           # -> advances to 2
    cli.main(["discussions", "pull", "demo", "--config", str(toml), "--all"])  # -> since_id 0
    cli.main(["discussions", "pull", "demo", "--config", str(toml)])           # -> still 2
    assert [since for _project, since in calls] == [0, 0, 2]


def test_discussions_pull_empty_is_friendly(tmp_path, monkeypatch, capsys):
    """With nothing new, the default prints a friendly 'no new' line and exits 0."""
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_TOKEN", "relay-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_config(tmp_path, repo)
    _capture_pull_discussions(monkeypatch, {"discussions": [], "latest_id": 0})

    assert cli.main(["discussions", "pull", "demo", "--config", str(toml)]) == 0
    assert "No new discussion messages" in capsys.readouterr().out


def test_discussions_reply_posts_with_author_and_echoes_id(tmp_path, monkeypatch, capsys):
    """`reply --as NAME` posts {project, body, author} and prints the returned id.

    Why this matters: the developer's write half. The --as name is sent as the author label
    (role is fixed server-side, not here), and the new id is echoed for confirmation.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_TOKEN", "relay-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_config(tmp_path, repo)
    calls = _capture_post_discussion(monkeypatch, {"id": 11})

    rc = cli.main(
        ["discussions", "reply", "demo", "Landed.", "--as", "Teammate B", "--config", str(toml)]
    )
    assert rc == 0
    assert calls == [("demo", "Landed.", "Teammate B")]
    assert "id 11" in capsys.readouterr().out


def test_discussions_reply_without_as_sends_empty_author(tmp_path, monkeypatch, capsys):
    """Without --as, the reply sends author="" so the relay applies its 'developer' fallback."""
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_TOKEN", "relay-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_config(tmp_path, repo)
    calls = _capture_post_discussion(monkeypatch)

    assert cli.main(["discussions", "reply", "demo", "hi", "--config", str(toml)]) == 0
    assert calls == [("demo", "hi", "")]


def test_discussions_reply_notes_when_as_is_overridden_by_identity(tmp_path, monkeypatch, capsys):
    """An identified producer's key overrides --as; the CLI reports the real name + a note.

    Why this matters: with a per-user contributor key the relay attributes the reply to the
    server-derived identity and IGNORES --as (identity is never client-asserted). The CLI must
    be honest about this: it reports the name the relay actually recorded and notes that the
    supplied --as was dropped, so the user is never misled about who the reply posted as.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_TOKEN", "relay-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_config(tmp_path, repo)
    # The relay echoes the STORED identity name, different from the requested --as.
    _capture_post_discussion(monkeypatch, {"id": 5, "author": "Teammate B"})

    rc = cli.main(
        ["discussions", "reply", "demo", "Landed.", "--as", "someone-else", "--config", str(toml)]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "as 'Teammate B'" in out  # reports the recorded identity, not the requested label
    assert "--as 'someone-else' was ignored" in out


def test_discussions_disabled_relay_is_clean_error(tmp_path, monkeypatch, capsys):
    """Both pull and reply fail cleanly (exit 1) with no relay enabled, and never call out."""
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    repo = _make_repo(tmp_path)
    toml = _write_relay_config(tmp_path, repo, enabled=False)
    pulls = _capture_pull_discussions(monkeypatch, _two_discussions())
    posts = _capture_post_discussion(monkeypatch)

    assert cli.main(["discussions", "pull", "demo", "--config", str(toml)]) == 1
    assert cli.main(["discussions", "reply", "demo", "x", "--config", str(toml)]) == 1
    assert pulls == [] and posts == []
    assert "no relay" in capsys.readouterr().err.lower()


# AU1-R P4: the listing renders timestamps in the CONFIGURED zone (KI-20). It used to
# hardcode America/Los_Angeles, so a user who set `display_timezone` got their zone
# everywhere except here — the one surface that reads back what a supervisor wrote.


def test_discussion_listing_renders_in_the_configured_timezone(capsys):
    """The same stored instant prints in whatever zone display_timezone names.

    Why this matters: the bug was invisible to the suite because the default config IS
    Pacific — which is exactly why this pins NON-default zones. One instant, three zones:
    19:30 UTC on the 19th is 12:30 the same day in California (PDT, UTC-7 in June) and 04:30
    the NEXT day in Tokyo, so this pins the date rollover too, not just the clock time.

    Calls the printer directly (it does no I/O) so the zone is the only variable.
    """
    item = {
        "role": "supervisor",
        "author_name": "Supervisor A",
        "body": "Looks good.",
        "created_at": "2026-06-19T19:30:00+00:00",
    }

    cli._print_discussions([item], "demo", False, "UTC")
    assert "2026-06-19 19:30 UTC" in capsys.readouterr().out

    cli._print_discussions([item], "demo", False, "America/Los_Angeles")
    assert "2026-06-19 12:30 PDT" in capsys.readouterr().out

    cli._print_discussions([item], "demo", False, "Asia/Tokyo")
    out = capsys.readouterr().out
    assert "2026-06-20 04:30 JST" in out, out


def test_discussions_pull_uses_the_configured_timezone(tmp_path, monkeypatch, capsys):
    """`discussions pull` reaches the formatter with the config's zone, not a hardcoded one.

    Why this matters: the test above proves the formatter can render any zone; this proves
    the COMMAND passes the configured value in. Without it, threading the parameter through
    and then forgetting to pass it at the single call site would still pass.

    The canned thread's supervisor message is stamped 19:30 UTC on 2026-06-28, which is 04:30
    the NEXT day in Tokyo — so this also catches a zone applied to the wrong field.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_TOKEN", "relay-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_config(tmp_path, repo, display_timezone="Asia/Tokyo")
    _capture_pull_discussions(monkeypatch, _two_discussions())

    assert cli.main(["discussions", "pull", "demo", "--config", str(toml)]) == 0
    out = capsys.readouterr().out
    assert "2026-06-29 04:30 JST" in out, out


# --- `orion relay-user` admin CLI (C3 / PR B) ---------------------------------
#
# These pin the provisioning commands WITHOUT a running relay: the admin client
# functions (cli.relay_create_user / relay_list_users / relay_revoke_user) are
# monkeypatched to recorders, so the tests assert on the args the CLI resolves (relay
# URL + admin token + parsed flags), the printed output (incl. the one-time key), and
# the config/secrets/error gates — never on a real relay.


def _write_relay_admin_config(tmp_path, repo, *, enabled=True, with_admin=True, with_project=True):
    """Write an orion.toml whose [relay] table optionally has admin_token_env_var.

    Args:
        tmp_path: per-test temp dir.
        repo: path to the git repo for the demo project (ignored when with_project=False).
        enabled: whether the [relay] table is enabled.
        with_admin: whether to include admin_token_env_var (the provisioning gate).
        with_project: whether to include a [projects.demo] table. When False the config
            has ONLY a [relay] table — the admin-only operator case relay-user must
            support (full load_config would reject it for having no projects).

    Why:
        The relay-user tests vary a few dimensions — relay enabled, whether an admin token
        env var is configured, and whether any local project exists — so this keeps each
        test to its case (DRY).
    """
    admin_line = (
        '        admin_token_env_var = "ORION_RELAY_ADMIN_TOKEN"\n' if with_admin else ""
    )
    project_block = (
        f"""
        [projects.demo]
        repo_path = "{repo.as_posix()}"
        share_level = "high_level"
        collectors = ["git"]

          [[projects.demo.recipients]]
          name = "Alex"
          channel = "discord"
          webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """
        if with_project
        else ""
    )
    toml = tmp_path / "orion.toml"
    toml.write_text(
        f"""
        state_db = "state.sqlite3"

        [relay]
        enabled = {str(enabled).lower()}
        url = "https://relay.test/ingest"
        token_env_var = "ORION_RELAY_TOKEN"
{admin_line}{project_block}
        """
    )
    return toml


def test_relay_user_add_provisions_and_prints_key_once(tmp_path, monkeypatch, capsys):
    """`relay-user add` resolves URL+admin token+flags, calls the client, prints the key.

    Why this matters: this is provisioning's CLI contract — the command must read the
    relay URL and the SEPARATE admin token from config/.env, pass the parsed name/role/
    projects to the client, and surface the returned one-time key with a copy-it-now
    warning (it cannot be retrieved later).
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_ADMIN_TOKEN", "admin-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_admin_config(tmp_path, repo)

    calls = []

    def fake_create(url, token, name, role, projects, **_kw):
        calls.append((url, token, name, role, projects))
        return {"id": 1, "name": name, "role": role, "projects": projects, "key": "RAWKEY-123"}

    monkeypatch.setattr(cli, "relay_create_user", fake_create)

    code = cli.main(
        ["relay-user", "add", "alice", "--role", "viewer", "--project", "demo",
         "--config", str(toml)]
    )
    assert code == 0
    assert calls == [("https://relay.test/ingest", "admin-secret", "alice", "viewer", ["demo"])]
    out = capsys.readouterr().out
    assert "alice" in out
    assert "RAWKEY-123" in out
    assert "once" in out.lower()  # the shown-once warning


def test_relay_user_add_threads_multiple_projects(tmp_path, monkeypatch, capsys):
    """Repeated --project flags accumulate into the projects list passed to the client.

    Why this matters: a viewer can be scoped to several projects; each --project must
    append, so the client receives the full ordered list — not just the last one.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_ADMIN_TOKEN", "admin-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_admin_config(tmp_path, repo)

    seen = []
    monkeypatch.setattr(
        cli,
        "relay_create_user",
        lambda url, token, name, role, projects, **k: seen.append(projects)
        or {"name": name, "role": role, "projects": projects, "key": "K"},
    )

    code = cli.main(
        ["relay-user", "add", "bob", "--project", "alpha", "--project", "beta",
         "--config", str(toml)]
    )
    assert code == 0
    assert seen == [["alpha", "beta"]]


def test_relay_user_add_disabled_relay_is_clean_error(tmp_path, monkeypatch):
    """With the relay disabled, `relay-user add` errors cleanly and never calls the client.

    Why this matters: provisioning targets a relay; a disabled [relay] is an actionable
    config error (exit 1), not a crash, and no request is attempted.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_ADMIN_TOKEN", "admin-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_admin_config(tmp_path, repo, enabled=False)

    made = []
    monkeypatch.setattr(cli, "relay_create_user", lambda *a, **k: made.append(a) or {})
    code = cli.main(["relay-user", "add", "alice", "--config", str(toml)])
    assert code == 1
    assert made == []  # never attempted a request


def test_relay_user_add_without_admin_token_env_var_is_clean_error(tmp_path, monkeypatch):
    """With no admin_token_env_var configured, `relay-user add` errors and never calls out.

    Why this matters: provisioning needs the SEPARATE admin token; a [relay] that lacks
    admin_token_env_var (push-only) must produce a clear error telling the operator to add
    it, not a confusing failure later.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_ADMIN_TOKEN", "admin-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_admin_config(tmp_path, repo, with_admin=False)

    made = []
    monkeypatch.setattr(cli, "relay_create_user", lambda *a, **k: made.append(a) or {})
    code = cli.main(["relay-user", "add", "alice", "--config", str(toml)])
    assert code == 1
    assert made == []


def test_relay_user_add_missing_admin_secret_is_clean_error(tmp_path, monkeypatch):
    """With admin_token_env_var configured but unset in .env, the command errors cleanly.

    Why this matters: a configured-but-unset admin token is a SecretsError naming the
    variable — caught before any request, so a forgotten secret is a clear fix, not a 401.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("ORION_RELAY_ADMIN_TOKEN", raising=False)  # configured but unset
    repo = _make_repo(tmp_path)
    toml = _write_relay_admin_config(tmp_path, repo)

    made = []
    monkeypatch.setattr(cli, "relay_create_user", lambda *a, **k: made.append(a) or {})
    code = cli.main(["relay-user", "add", "alice", "--config", str(toml)])
    assert code == 1
    assert made == []


def test_relay_user_add_delivery_error_is_clean_exit(tmp_path, monkeypatch, capsys):
    """A relay error (e.g. a duplicate name 409) surfaces as a clean exit 1 with its message.

    Why this matters: a duplicate name must read as an actionable message, not a traceback.
    The client raises DeliveryError with the relay's lifted reason; the CLI prints it and
    exits 1.
    """
    from orion.delivery import DeliveryError

    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_ADMIN_TOKEN", "admin-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_admin_config(tmp_path, repo)

    monkeypatch.setattr(
        cli,
        "relay_create_user",
        lambda *a, **k: (_ for _ in ()).throw(
            DeliveryError("Relay returned HTTP 409: a user named 'alice' already exists")
        ),
    )
    code = cli.main(["relay-user", "add", "alice", "--config", str(toml)])
    assert code == 1
    err = capsys.readouterr().err
    assert "409" in err and "already exists" in err


def test_relay_user_list_prints_roster(tmp_path, monkeypatch, capsys):
    """`relay-user list` prints each user's role, status, and scope.

    Why this matters: the operational view must show who has access and what they can see,
    drawn from the client's roster response (which carries no credential material).
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_ADMIN_TOKEN", "admin-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_admin_config(tmp_path, repo)

    monkeypatch.setattr(
        cli,
        "relay_list_users",
        lambda url, token, **k: {
            "users": [
                {"name": "alice", "role": "viewer", "active": True,
                 "projects": ["demo"], "last_login_at": None},
                {"name": "root", "role": "admin", "active": True,
                 "projects": [], "last_login_at": "2026-06-24T10:00:00+00:00"},
            ]
        },
    )
    code = cli.main(["relay-user", "list", "--config", str(toml)])
    assert code == 0
    out = capsys.readouterr().out
    assert "alice" in out and "viewer" in out and "demo" in out
    assert "root" in out and "admin" in out


def test_relay_user_list_empty_is_friendly(tmp_path, monkeypatch, capsys):
    """An empty roster prints a friendly 'no users yet' line, exit 0.

    Why this matters: a fresh relay with no users is a normal state, not an error — the
    command should say so plainly rather than print nothing.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_ADMIN_TOKEN", "admin-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_admin_config(tmp_path, repo)

    monkeypatch.setattr(cli, "relay_list_users", lambda url, token, **k: {"users": []})
    code = cli.main(["relay-user", "list", "--config", str(toml)])
    assert code == 0
    assert "No relay users" in capsys.readouterr().out


def test_relay_user_revoke_calls_client_and_confirms(tmp_path, monkeypatch, capsys):
    """`relay-user revoke <name>` calls the client with the resolved URL+token+name.

    Why this matters: revocation is the settled Inc-1 safety valve; the command must thread
    the name to the client and confirm the deactivation to the operator.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_ADMIN_TOKEN", "admin-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_admin_config(tmp_path, repo)

    calls = []
    monkeypatch.setattr(
        cli,
        "relay_revoke_user",
        lambda url, token, name, **k: calls.append((url, token, name))
        or {"name": name, "revoked": True},
    )
    code = cli.main(["relay-user", "revoke", "alice", "--config", str(toml)])
    assert code == 0
    assert calls == [("https://relay.test/ingest", "admin-secret", "alice")]
    assert "Revoked" in capsys.readouterr().out


def test_relay_user_revoke_unknown_user_is_clean_error(tmp_path, monkeypatch, capsys):
    """Revoking an unknown name surfaces the relay's 404 as a clean exit 1.

    Why this matters: a typo'd name must be an actionable message, not a crash.
    """
    from orion.delivery import DeliveryError

    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_ADMIN_TOKEN", "admin-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_admin_config(tmp_path, repo)

    monkeypatch.setattr(
        cli,
        "relay_revoke_user",
        lambda *a, **k: (_ for _ in ()).throw(
            DeliveryError("Relay returned HTTP 404: no user named 'ghost'")
        ),
    )
    code = cli.main(["relay-user", "revoke", "ghost", "--config", str(toml)])
    assert code == 1
    assert "404" in capsys.readouterr().err


def test_relay_user_grant_calls_client_and_prints_new_scope(tmp_path, monkeypatch, capsys):
    """`relay-user grant <name> --project P` threads name+projects and prints the new scope."""
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_ADMIN_TOKEN", "admin-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_admin_config(tmp_path, repo)

    calls = []
    monkeypatch.setattr(
        cli,
        "relay_grant_projects",
        lambda url, token, name, projects, **k: calls.append((url, token, name, projects))
        or {"name": name, "projects": ["demo", "other"]},
    )
    code = cli.main(
        ["relay-user", "grant", "alice", "--project", "other", "--config", str(toml)]
    )
    assert code == 0
    assert calls == [("https://relay.test/ingest", "admin-secret", "alice", ["other"])]
    out = capsys.readouterr().out
    assert "Granted" in out and "demo, other" in out


def test_relay_user_grant_without_project_is_clean_error(tmp_path, monkeypatch, capsys):
    """grant with no --project errors (exit 1) before any client call."""
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_ADMIN_TOKEN", "admin-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_admin_config(tmp_path, repo)

    called = []
    monkeypatch.setattr(cli, "relay_grant_projects", lambda *a, **k: called.append(1))
    code = cli.main(["relay-user", "grant", "alice", "--config", str(toml)])
    assert code == 1 and called == []  # errored before calling the client
    assert "project" in capsys.readouterr().err.lower()


def test_relay_user_key_add_prints_the_key_once_and_the_safe_sequence(tmp_path, monkeypatch, capsys):
    """`relay-user key add` threads name+label and prints the one-time key plus next steps.

    Why this matters: this replaced `rotate`, and the replacement is only safer if the
    operator actually follows add → deploy → verify → revoke. Printing that sequence at the
    moment the key is issued is what makes the safe path the obvious one.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_ADMIN_TOKEN", "admin-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_admin_config(tmp_path, repo)

    calls = []
    monkeypatch.setattr(
        cli,
        "relay_add_user_key",
        lambda url, token, name, label, **k: calls.append((url, token, name, label))
        or {"name": name, "label": label, "id": 7, "key": "NEWKEY-456"},
    )
    code = cli.main(["relay-user", "key", "add", "alice", "--label", "wsl2", "--config", str(toml)])
    assert code == 0
    assert calls == [("https://relay.test/ingest", "admin-secret", "alice", "wsl2")]
    out = capsys.readouterr().out
    assert "NEWKEY-456" in out and "once" in out.lower()
    assert "still work" in out.lower()   # the existing keys are explicitly not disturbed
    assert "revoke" in out.lower()       # and the operator is pointed at the final step


def test_relay_user_key_list_never_prints_key_material(tmp_path, monkeypatch, capsys):
    """`relay-user key list` shows ids/labels/usage, and no verifier ever reaches the output.

    Why this matters: the listing is the one credential surface designed to be read by a
    human, which makes it the easiest place to leak key material by accident.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_ADMIN_TOKEN", "admin-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_admin_config(tmp_path, repo)

    monkeypatch.setattr(
        cli,
        "relay_list_user_keys",
        lambda url, token, name, **k: {"name": name, "credentials": [
            {"id": 1, "type": "key", "label": "mac", "active": 1,
             "created_at": "2026-07-19T00:00:00+00:00", "last_used_at": "2026-07-20T00:00:00+00:00"},
            {"id": 2, "type": "key", "label": "wsl2", "active": 0,
             "created_at": "2026-07-19T00:00:00+00:00", "last_used_at": None},
        ]},
    )
    assert cli.main(["relay-user", "key", "list", "alice", "--config", str(toml)]) == 0
    out = capsys.readouterr().out
    assert "mac" in out and "wsl2" in out
    assert "active" in out and "revoked" in out
    assert "never used" in out          # the signal that decides what is safe to revoke
    assert "verifier" not in out.lower()


def test_relay_user_key_revoke_threads_the_id(tmp_path, monkeypatch, capsys):
    """`relay-user key revoke --id` targets one credential and says the others survive."""
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_ADMIN_TOKEN", "admin-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_admin_config(tmp_path, repo)

    calls = []
    monkeypatch.setattr(
        cli,
        "relay_revoke_user_key",
        lambda url, token, name, cid, **k: calls.append((name, cid))
        or {"name": name, "id": cid, "revoked": True},
    )
    code = cli.main(["relay-user", "key", "revoke", "alice", "--id", "2", "--config", str(toml)])
    assert code == 0
    assert calls == [("alice", 2)]
    assert "still work" in capsys.readouterr().out.lower()


def test_relay_user_role_warns_when_a_demotion_leaves_no_scope(tmp_path, monkeypatch, capsys):
    """`relay-user role` warns loudly when the new role has no grants (it would see nothing).

    Why this matters: the real operational trap. Admins bypass scope, so an admin account
    typically has ZERO grants — demoting it to a scoped role therefore produces an account
    that can log in and see an empty dashboard. Without this warning that reads as a bug.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_ADMIN_TOKEN", "admin-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_admin_config(tmp_path, repo)

    monkeypatch.setattr(
        cli,
        "relay_set_user_role",
        lambda url, token, name, role, **k: {"name": name, "role": role, "projects": []},
    )
    assert cli.main(["relay-user", "role", "dad", "supervisor", "--config", str(toml)]) == 0
    out = capsys.readouterr().out
    assert "supervisor" in out
    assert "WARNING" in out and "sees nothing" in out
    assert "relay-user grant dad" in out   # the fix is spelled out, not just the problem


def test_relay_user_role_reports_scope_when_the_account_has_grants(tmp_path, monkeypatch, capsys):
    """With grants present, the role change reports the resulting scope and does NOT warn."""
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_ADMIN_TOKEN", "admin-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_admin_config(tmp_path, repo)

    monkeypatch.setattr(
        cli,
        "relay_set_user_role",
        lambda url, token, name, role, **k: {"name": name, "role": role, "projects": ["orion"]},
    )
    assert cli.main(["relay-user", "role", "dad", "supervisor", "--config", str(toml)]) == 0
    out = capsys.readouterr().out
    assert "orion" in out and "WARNING" not in out


def test_relay_user_add_member_does_not_report_zero_grants_as_incomplete(
    tmp_path, monkeypatch, capsys
):
    """Provisioning a grantless `member` describes org-visibility, not a missing grant.

    Why this matters: a member with no grants is the INTENDED configuration — it reads every
    org-visible project without one. The grant-only wording used for viewers ("none yet —
    grant projects so this viewer can see anything") tells the operator to fix something that
    is not broken, and understates what the account can actually read. Caught in the live
    close-out when `relay-user add kb-check --role member` printed exactly that.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_ADMIN_TOKEN", "admin-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_admin_config(tmp_path, repo)

    monkeypatch.setattr(
        cli,
        "relay_create_user",
        lambda url, token, name, role, projects, **k: {
            "name": name,
            "role": role,
            "projects": [],
            "key": "k",
        },
    )
    assert cli.main(["relay-user", "add", "kb", "--role", "member", "--config", str(toml)]) == 0
    out = capsys.readouterr().out
    assert "org-visible" in out
    # The grant-only phrasing must not appear: it would misdescribe a correct account.
    assert "none yet" not in out
    assert "grant projects so this viewer" not in out


def test_relay_user_add_member_with_grants_shows_them_as_additive(
    tmp_path, monkeypatch, capsys
):
    """A member's explicit grants are reported as ADDITIONAL to org-visible projects.

    Why this matters: visibility is a floor, not a ceiling — someone can be an org member
    AND be granted one restricted project on top. Printing only the grant list (the generic
    scoped-role branch) would imply the grant is the account's whole scope, hiding the
    org-visible set it can also read.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_ADMIN_TOKEN", "admin-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_admin_config(tmp_path, repo)

    monkeypatch.setattr(
        cli,
        "relay_create_user",
        lambda url, token, name, role, projects, **k: {
            "name": name,
            "role": role,
            "projects": ["secret-project"],
            "key": "k",
        },
    )
    assert cli.main(
        ["relay-user", "add", "kb", "--role", "member", "--project", "secret-project",
         "--config", str(toml)]
    ) == 0
    out = capsys.readouterr().out
    assert "org-visible" in out and "secret-project" in out


def test_relay_user_role_to_member_does_not_warn_about_missing_grants(
    tmp_path, monkeypatch, capsys
):
    """Promoting an account to `member` must not warn that it "sees nothing".

    Why this matters: this is the sharper half of the same bug — for a grantless member the
    default-deny warning is not merely unhelpful, it is FALSE. The account sees every
    org-visible project. Warning here would push an operator to grant projects that the
    member already reads, quietly widening access beyond what was intended.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_ADMIN_TOKEN", "admin-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_admin_config(tmp_path, repo)

    monkeypatch.setattr(
        cli,
        "relay_set_user_role",
        lambda url, token, name, role, **k: {"name": name, "role": role, "projects": []},
    )
    assert cli.main(["relay-user", "role", "kb", "member", "--config", str(toml)]) == 0
    out = capsys.readouterr().out
    assert "WARNING" not in out
    assert "sees nothing" not in out
    assert "org-visible" in out


def test_relay_user_rename_notes_that_history_keeps_the_old_name(tmp_path, monkeypatch, capsys):
    """`relay-user rename` threads both names and states the history consequence."""
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_ADMIN_TOKEN", "admin-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_admin_config(tmp_path, repo)

    calls = []
    monkeypatch.setattr(
        cli,
        "relay_rename_user",
        lambda url, token, name, new_name, **k: calls.append((name, new_name))
        or {"name": name, "new_name": new_name},
    )
    assert cli.main(["relay-user", "rename", "macos", "mac-mini", "--config", str(toml)]) == 0
    assert calls == [("macos", "mac-mini")]
    assert "keep the name they were sent under" in capsys.readouterr().out


def test_relay_user_rotate_is_gone_from_the_cli(tmp_path, monkeypatch):
    """`relay-user rotate` no longer parses — the verb is retired, not merely undocumented.

    Why this matters: leaving a working `rotate` would keep its hazards (the silent-401 window
    on scheduled machines, the stranded-response state) reachable by muscle memory.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    repo = _make_repo(tmp_path)
    toml = _write_relay_admin_config(tmp_path, repo)
    try:
        code = cli.main(["relay-user", "rotate", "alice", "--config", str(toml)])
    except SystemExit as exc:      # argparse rejects an unknown subcommand with exit 2
        code = exc.code
    assert code == 2


def test_relay_user_delete_calls_client_and_confirms(tmp_path, monkeypatch, capsys):
    """`relay-user delete <name>` threads the name and confirms the name is freed."""
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_ADMIN_TOKEN", "admin-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_admin_config(tmp_path, repo)

    calls = []
    monkeypatch.setattr(
        cli,
        "relay_delete_user",
        lambda url, token, name, **k: calls.append((url, token, name))
        or {"name": name, "deleted": True},
    )
    code = cli.main(["relay-user", "delete", "alice", "--config", str(toml)])
    assert code == 0
    assert calls == [("https://relay.test/ingest", "admin-secret", "alice")]
    assert "Deleted" in capsys.readouterr().out


def test_relay_user_works_with_relay_only_config_no_projects(tmp_path, monkeypatch, capsys):
    """`relay-user` runs against a config that has ONLY a [relay] table (no projects).

    Why this matters: provisioning talks only to the relay; an admin-only operator who
    runs the relay but reports from elsewhere may have no local `[projects.<name>]`. Full
    load_config would reject that with "defines no projects"; relay-user uses the focused
    relay-only loader, so it must succeed here where a normal command would not.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_ADMIN_TOKEN", "admin-secret")
    toml = _write_relay_admin_config(tmp_path, None, with_project=False)

    # Sanity: a normal command (which uses full load_config) DOES reject this config...
    assert cli.main(["projects", "--config", str(toml)]) == 1

    # ...but relay-user works.
    seen = []
    monkeypatch.setattr(
        cli, "relay_list_users", lambda url, token, **k: seen.append(url) or {"users": []}
    )
    code = cli.main(["relay-user", "list", "--config", str(toml)])
    assert code == 0
    assert seen == ["https://relay.test/ingest"]


# --- S2.2: `relay-project lifecycle` ------------------------------------------


def test_relay_project_lifecycle_past_calls_the_client_and_says_what_changes(
    tmp_path, monkeypatch, capsys
):
    """`relay-project lifecycle <name> past` threads the value and explains the consequences.

    Why this matters: this is a curation act with real, non-obvious effects — the project
    leaves the live sections AND drops out of every deadline view — while its record is
    untouched. An operator running it once, months apart, should not have to remember which
    of those it does, so the command says so at the moment it takes effect.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_ADMIN_TOKEN", "admin-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_admin_config(tmp_path, repo)

    calls = []
    monkeypatch.setattr(
        cli,
        "relay_set_project_lifecycle",
        lambda url, token, name, lifecycle: calls.append((url, token, name, lifecycle))
        or {"name": name, "lifecycle": lifecycle},
    )
    code = cli.main(["relay-project", "lifecycle", "demo", "past", "--config", str(toml)])

    assert code == 0
    assert calls == [("https://relay.test/ingest", "admin-secret", "demo", "past")]
    out = capsys.readouterr().out
    assert "past" in out and "Past projects" in out
    assert "overdue" in out          # names the deadline exclusion
    assert "unchanged" in out        # and that the record survives


def test_relay_project_lifecycle_active_reads_as_the_reverse(tmp_path, monkeypatch, capsys):
    """Setting `active` reports the project back in the live view — the act is reversible."""
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_ADMIN_TOKEN", "admin-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_admin_config(tmp_path, repo)

    monkeypatch.setattr(
        cli, "relay_set_project_lifecycle", lambda *a: {"name": "demo", "lifecycle": "active"}
    )
    code = cli.main(["relay-project", "lifecycle", "demo", "active", "--config", str(toml)])
    assert code == 0
    assert "active again" in capsys.readouterr().out


def test_relay_project_lifecycle_reports_a_relay_failure_as_exit_1(
    tmp_path, monkeypatch, capsys
):
    """An unknown project (the relay's 404 → DeliveryError) exits 1 with the message.

    Why this matters: the relay refuses a project it has never heard of, and a silent exit 0
    would let an operator believe a typo'd name was marked past.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_ADMIN_TOKEN", "admin-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_admin_config(tmp_path, repo)

    from orion.delivery import DeliveryError

    def _boom(*a):
        raise DeliveryError("relay returned 404: no project named 'typo'")

    monkeypatch.setattr(cli, "relay_set_project_lifecycle", _boom)
    code = cli.main(["relay-project", "lifecycle", "typo", "past", "--config", str(toml)])
    assert code == 1
    assert "404" in capsys.readouterr().err


def test_config_path_defaults_to_orion_config_env(tmp_path, monkeypatch, capsys):
    """With $ORION_CONFIG set and --config omitted, commands resolve the env-pointed config.

    Why this matters: this is the friction fix for non-interactive callers (the session
    skill, git hooks, schedulers) — set ORION_CONFIG once instead of passing --config each
    time. We point it at a config whose project name ("demo") differs from any default
    orion.toml, and confirm `projects` finds it with no --config flag.
    """
    repo = _make_repo(tmp_path)
    toml = _write_config(tmp_path, repo)              # project "demo"
    monkeypatch.setenv("ORION_CONFIG", str(toml))

    code = cli.main(["projects"])                     # NO --config flag
    out = capsys.readouterr().out
    assert code == 0
    assert "demo" in out                              # loaded via $ORION_CONFIG


# --- `orion bot` is PARKED (KI-28 Stage 2) ------------------------------------
# The bot's write path (relay comments) retired and repointing to the discussion write
# awaits per-user keys, so cmd_bot no longer starts a listener — it prints why it is
# parked and exits 1. The pure decision core is still covered by test_bot_core.py.


def test_bot_is_parked_and_exits_cleanly(tmp_path, capsys):
    """`orion bot` exits 1 with a parked notice and never starts a listener.

    Why this matters: the bot cannot deliver until per-user keys land, so running it must
    be a clear, actionable no-op rather than a live Socket Mode connection that could never
    post. The message names the reason (parked) and points at the revival.
    """
    # cmd_bot is parked regardless of config, so a nonexistent config path is fine — it is
    # never read. The conftest guards already block any real .env / network.
    code = cli.main(["bot", "--config", str(tmp_path / "orion.toml")])
    assert code == 1
    assert "parked" in capsys.readouterr().err.lower()


# --- D5: per-recipient signals routing ----------------------------------------
# These drive the full report pipeline (real repo + notes file, mocked LLM and
# delivery) and assert that each recipient receives ONLY the sections their
# `signals` filter subscribed to — and that the relay still gets the full report.


def _write_signals_config(tmp_path, repo, *, notes_body="Working on it.", relay=False):
    """Write a git+notes project with two disjointly-filtered Discord recipients.

    Args:
        tmp_path: per-test temp dir (also where the state db + notes file live).
        repo: the git repo (the git collector's source of activity).
        notes_body: text written to NOTES.md; "" leaves the notes signal idle
            (file present but empty -> no notes activity, no error).
        relay: when True, add an enabled [relay] table so the push path runs.

    Returns:
        Path to the written orion.toml.

    Why:
        D5's routing is only observable with >1 collector AND recipients that
        subscribe to different slices. This keeps each test to the one variable it
        flexes (notes idle vs active, relay on/off) instead of repeating TOML (DRY).
        Alex subscribes to git only, Sam to notes only — disjoint, so a correctly
        filtered run sends each exactly one section.
    """
    notes_file = tmp_path / "NOTES.md"
    notes_file.write_text(notes_body)
    relay_table = (
        '[relay]\n'
        'enabled = true\n'
        'url = "https://relay.test/ingest"\n'
        'token_env_var = "ORION_RELAY_TOKEN"\n\n'
        if relay
        else ""
    )
    toml = tmp_path / "orion.toml"
    toml.write_text(
        f"""
        state_db = "state.sqlite3"

        {relay_table}[projects.demo]
        repo_path = "{repo.as_posix()}"
        share_level = "high_level"
        collectors = ["git", "notes"]
        notes_file = "{notes_file.as_posix()}"

          [[projects.demo.recipients]]
          name = "Alex"
          channel = "discord"
          webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
          signals = ["git"]

          [[projects.demo.recipients]]
          name = "Sam"
          channel = "discord"
          webhook_env_var = "ORION_DISCORD_WEBHOOK_SAM"
          signals = ["notes"]
        """
    )
    return toml


def test_signals_route_disjoint_slices_to_each_recipient(tmp_path, env_and_mocks):
    """Each recipient receives only the section their `signals` subscribed to.

    Why this matters: this is the core D5 guarantee — a mentor who wants notes and a
    teammate who wants code get DIFFERENT, filtered reports from one run. Alex (git
    only) must see "Code activity" and not "Notes"; Sam (notes only) the reverse.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_DISCORD_WEBHOOK_SAM", "https://discord.test/sam")
    use_summary(mp, "Did the code work.")
    repo = _make_repo(tmp_path)
    toml = _write_signals_config(tmp_path, repo, notes_body="A hand-written note.")

    _answer(mp, "y")
    code = cli.main(["report", "demo", "--config", str(toml)])
    assert code == 0

    # Index the two captured deliveries by their webhook URL.
    by_url = {url: text for text, url in env_and_mocks["sent"]}
    assert set(by_url) == {"https://discord.test/webhook", "https://discord.test/sam"}

    alex = by_url["https://discord.test/webhook"]
    sam = by_url["https://discord.test/sam"]
    # Alex (git): the git section only.
    assert "Code activity" in alex and "Did the code work." in alex
    assert "Notes" not in alex and "A hand-written note." not in alex
    # Sam (notes): the notes section only.
    assert "Notes" in sam and "A hand-written note." in sam
    assert "Code activity" not in sam and "Did the code work." not in sam


def test_signals_idle_signal_means_recipient_gets_nothing(tmp_path, env_and_mocks):
    """A recipient whose only subscribed signal was idle this run is not delivered to.

    Why this matters: filtering must not invent or mis-route content. With notes
    empty (no notes activity), Sam (notes only) has nothing to receive, so he gets
    no message at all — while Alex (git) still gets his git report and the run
    succeeds on Alex's delivery alone.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_DISCORD_WEBHOOK_SAM", "https://discord.test/sam")
    repo = _make_repo(tmp_path)
    # notes_body="" -> NOTES.md exists but is empty -> the notes signal is idle.
    toml = _write_signals_config(tmp_path, repo, notes_body="")

    _answer(mp, "y")
    code = cli.main(["report", "demo", "--config", str(toml)])
    assert code == 0

    sent_urls = [url for _text, url in env_and_mocks["sent"]]
    # Only Alex (git) received anything; Sam's idle notes filter sent him nothing.
    assert sent_urls == ["https://discord.test/webhook"]


def test_relay_receives_full_report_despite_per_recipient_filtering(tmp_path, env_and_mocks):
    """The relay push carries the FULL report even when recipients see filtered slices.

    Why this matters: D5 filters chat-channel delivery, not the dashboard's record —
    the relay must still receive the complete report (both sections) so the hosted
    view is whole, regardless of who subscribed to what.
    """
    mp = env_and_mocks["monkeypatch"]
    mp.setenv("ORION_DISCORD_WEBHOOK_SAM", "https://discord.test/sam")
    mp.setenv("ORION_RELAY_TOKEN", "relay-secret")
    use_summary(mp, "Did the code work.")
    pushes = _capture_relay(mp)
    repo = _make_repo(tmp_path)
    toml = _write_signals_config(tmp_path, repo, notes_body="A hand-written note.", relay=True)

    _answer(mp, "y")
    code = cli.main(["report", "demo", "--config", str(toml)])
    assert code == 0

    # Both recipients got their (filtered) slice...
    assert len(env_and_mocks["sent"]) == 2
    # ...but the single relay push carries BOTH sections — the complete report.
    assert len(pushes) == 1
    blob_json = pushes[0][0]
    assert "Code activity" in blob_json and "Notes" in blob_json
    assert "Did the code work." in blob_json and "A hand-written note." in blob_json


# --- D4: incubator collector end-to-end ---------------------------------------


def test_incubator_collector_end_to_end(tmp_path, env_and_mocks):
    """A report for an incubator-only project delivers an 'Idea pipeline' section.

    Why this matters: D4's full wiring — config -> _collect_for dispatch -> structured
    passthrough (NO LLM) -> merge -> compose -> deliver -> per-recipient signal route —
    must produce a readable idea-pipeline update, proving the fifth collector is fully
    integrated. The recipient subscribes to `signals = ["incubator"]` (the dedicated
    `[projects.incubator]` use the design intends), so this also exercises D5 routing.
    """
    mp = env_and_mocks["monkeypatch"]
    index = tmp_path / "index.md"
    index.write_text(
        "| Idea | Status | One-line pitch |\n"
        "|------|--------|----------------|\n"
        "| [VLM Photo Overlay](ideas/vlm.md) | refining | Annotate a photo |\n",
        encoding="utf-8",
    )
    toml = tmp_path / "orion.toml"
    # repo_path is required by config even though git isn't a collector here; it is
    # never read because only the incubator collector runs. Point it at tmp_path.
    toml.write_text(
        f"""
        state_db = "state.sqlite3"

        [projects.incubator]
        repo_path = "{tmp_path.as_posix()}"
        collectors = ["incubator"]
        incubator_file = "{index.as_posix()}"

          [[projects.incubator.recipients]]
          name = "Mentor"
          channel = "discord"
          webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
          signals = ["incubator"]
        """
    )

    _answer(mp, "y")
    code = cli.main(["report", "incubator", "--config", str(toml)])
    assert code == 0

    # The mentor received exactly the idea-pipeline section (no LLM was involved).
    assert len(env_and_mocks["sent"]) == 1
    text, _url = env_and_mocks["sent"][0]
    assert "Idea pipeline" in text
    assert "New idea: VLM Photo Overlay (refining)" in text
    assert "Annotate a photo" in text


# --- Push-only collectors must not reach the report loop ----------------------
# `collectors` holds two kinds of name: report collectors (a _collect_for branch, a
# report section) and push-only capability flags (a dedicated push command, no section).
# Treating the second kind as the first raised "Unknown collector 'disciplines'" and broke
# `orion report` outright for every project that enabled it. These pin the distinction.


def _write_disciplines_config(tmp_path, repo):
    """Write an orion.toml enabling git plus the push-only `disciplines` capability.

    Args:
        tmp_path: pytest's per-test temporary directory.
        repo: Path to the git repo the git collector will read.

    Returns:
        Path to the written orion.toml.

    Why:
        This is the exact real-world shape that broke: a normal reporting project that
        ALSO enables disciplines so `disciplines-push` will run for it. `discipline_docs`
        is required by config validation whenever the collector is on, so a real doc is
        written — the point is a config that legitimately loads, not a malformed one.
    """
    doc = tmp_path / "principles.md"
    doc.write_text("- Explicit over clever.\n", encoding="utf-8")
    toml = tmp_path / "orion.toml"
    toml.write_text(
        f"""
        state_db = "state.sqlite3"

        [projects.demo]
        repo_path = "{repo.as_posix()}"
        share_level = "high_level"
        collectors = ["git", "disciplines"]
        discipline_docs = ["{doc.as_posix()}"]

          [[projects.demo.recipients]]
          name = "Alex"
          channel = "discord"
          webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """
    )
    return toml


def test_report_succeeds_with_a_push_only_collector_enabled(tmp_path, env_and_mocks):
    """A project enabling `disciplines` alongside `git` still reports successfully.

    Why this matters: this is the direct regression. `disciplines` is accepted by config
    validation (it gates `disciplines-push`) but has no _collect_for branch by design, so
    the report loop used to raise ConfigError and NO report could be produced at all for
    such a project. Enabling one capability must never disable an unrelated command.
    """
    mp = env_and_mocks["monkeypatch"]
    repo = _make_repo(tmp_path)
    toml = _write_disciplines_config(tmp_path, repo)
    use_summary(mp, "Did the code work.")

    _answer(mp, "y")
    code = cli.main(["report", "demo", "--config", str(toml)])

    assert code == 0
    # The report actually went out — a silently-empty success would hide the bug.
    assert len(env_and_mocks["sent"]) == 1
    assert "Did the code work." in env_and_mocks["sent"][0][0]


def test_push_only_collector_contributes_no_report_section(tmp_path, env_and_mocks):
    """The skipped capability adds no section — not even an empty one.

    Why this matters: the fix must be a true no-op, not a section with nothing in it. An
    empty "Disciplines" heading would reach real supervisors and read as a bug. Asserting
    on the exact section list also catches the opposite error — a future change that
    starts routing a push-only capability into the report body.
    """
    mp = env_and_mocks["monkeypatch"]
    repo = _make_repo(tmp_path)
    toml = _write_disciplines_config(tmp_path, repo)
    use_summary(mp, "Did the code work.")

    # Capture the blob handed to compose without changing what compose returns.
    captured = {}
    real_compose = cli.compose

    def _capturing_compose(blob, channel, display_timezone):
        captured["blob"] = blob
        return real_compose(blob, channel, display_timezone)

    mp.setattr(cli, "compose", _capturing_compose)

    _answer(mp, "y")
    assert cli.main(["report", "demo", "--config", str(toml)]) == 0

    # Exactly one section: git's. Disciplines contributed nothing at all.
    titles = [title for title, _ in captured["blob"].sections]
    assert titles == ["Code activity"]


def test_every_report_collector_has_a_dispatch_branch(tmp_path):
    """Each REPORT_COLLECTORS name reaches a real branch in _collect_for.

    Why this matters: this is the MIRROR of the disciplines bug. That one added a name to
    the supported list with no dispatch branch; the same mistake made with a genuine
    report collector would fail the same way, and only at run time on a real project.
    Walking the constant means a newly-added collector is covered the moment it is
    listed, with no test to remember to write.

    A collector may still fail for its own reasons here (an empty fixture file, no
    activity) — that is fine and not what we are pinning. We assert only that dispatch
    RESOLVED: neither the unknown-name guard nor the push-only guard was hit.
    """
    repo = _make_repo(tmp_path)
    # One empty file per file-backed collector. Content is irrelevant: we are testing
    # dispatch resolution, not collector behavior (each has its own test module).
    for filename in ("TODO.md", "NOTES.md", "IDEAS.md", "TRACKER.md"):
        (tmp_path / filename).write_text("", encoding="utf-8")
    toml = tmp_path / "orion.toml"
    toml.write_text(
        f"""
        state_db = "state.sqlite3"

        [projects.demo]
        repo_path = "{repo.as_posix()}"
        collectors = {list(REPORT_COLLECTORS)!r}
        tasks_file = "TODO.md"
        notes_file = "NOTES.md"
        incubator_file = "IDEAS.md"
        tracker_file = "TRACKER.md"

          [[projects.demo.recipients]]
          name = "Alex"
          channel = "discord"
          webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """.replace("'", '"')
    )
    project = get_project(load_config(toml), "demo")

    for collector in REPORT_COLLECTORS:
        try:
            cli._collect_for(project, collector, None)
        except ConfigError as exc:
            pytest.fail(
                f"_collect_for has no dispatch branch for report collector "
                f"{collector!r}: {exc}"
            )
        except Exception:
            # A collector-specific failure means dispatch DID resolve, which is all
            # this test claims. Deliberately broad: any non-ConfigError proves the
            # name reached its branch.
            pass


def test_collect_for_rejects_a_push_only_collector_with_a_distinct_message(tmp_path):
    """Reaching dispatch with a push-only name is reported as an Orion bug, not a typo.

    Why this matters: the guard is defensive — the loop skips these before dispatch — but
    if a future caller forgets, the message should say the caller is at fault rather than
    blaming the user's config for an "unknown collector" they set correctly. Distinct
    failure modes deserve distinct messages.
    """
    repo = _make_repo(tmp_path)
    toml = _write_disciplines_config(tmp_path, repo)
    project = get_project(load_config(toml), "demo")

    for collector in PUSH_ONLY_COLLECTORS:
        with pytest.raises(ConfigError, match="push-only capability"):
            cli._collect_for(project, collector, None)


def test_report_collectors_of_drops_every_push_only_name(tmp_path):
    """_report_collectors_of keeps report inputs in order and drops capability flags.

    Why this matters: this helper is the SINGLE place the push-only skip now lives, so
    every command that walks a project's collectors inherits it. Walking the constant
    (rather than naming "disciplines") means the next push-only capability is covered
    the moment it is listed in config.py.
    """
    repo = _make_repo(tmp_path)
    toml = _write_disciplines_config(tmp_path, repo)
    project = get_project(load_config(toml), "demo")

    kept = cli._report_collectors_of(project)

    assert kept == ["git"], "report inputs should survive, in config order"
    for collector in PUSH_ONLY_COLLECTORS:
        assert collector not in kept


def test_status_survives_a_project_enabling_a_push_only_collector(tmp_path, capsys):
    """`orion status` reports on a disciplines-enabled project instead of crashing.

    Why this matters: this is the KI-39 bug mirrored into a SIBLING command. The report
    loop learned to skip push-only names; `status` kept dispatching them, so the digest
    died with an unhandled ConfigError for any project enabling `disciplines` — which is
    the developer's own live config. Found by the DF1 dogfood sweep, not by the suite,
    because no test ran a second collector-walking command against that config shape.
    """
    repo = _make_repo(tmp_path)
    toml = _write_disciplines_config(tmp_path, repo)

    assert cli.main(["status", "--config", str(toml)]) == 0
    # The project must actually appear: an empty digest would be a silent pass.
    assert "demo" in capsys.readouterr().out


def test_baseline_survives_a_push_only_collector_and_marks_no_marker_for_it(tmp_path):
    """`orion baseline` baselines the real collectors and skips the capability flag.

    Why this matters: the same mirror bug, and worse here — baseline calls set_marker
    inside the loop, so it wrote git's marker and THEN crashed on `disciplines`, leaving
    the store half-advanced while telling the user the command failed. Re-running would
    then warn about re-baselining. We pin both halves: the command succeeds, and no
    marker is invented for a push-only name.
    """
    repo = _make_repo(tmp_path)
    toml = _write_disciplines_config(tmp_path, repo)

    assert cli.main(["baseline", "demo", "--config", str(toml)]) == 0

    conn = open_state(tmp_path / "state.sqlite3")
    assert get_marker(conn, "demo", "git") is not None, "the real collector baselined"
    for collector in PUSH_ONLY_COLLECTORS:
        assert get_marker(conn, "demo", collector) is None


# --- D4 follow-on: graduate-idea ----------------------------------------------
# graduate-idea reads the incubator index, finds a graduated idea, and registers a
# project for it by delegating to add-project. These tests need no LLM/delivery
# mocks (the command only writes config) — they use --yes/--print to skip prompts.

_GRAD_INDEX = (
    "| Idea | Status | One-line pitch |\n"
    "|------|--------|----------------|\n"
    "| [VLM Photo Overlay](ideas/vlm.md) | graduated | Annotate a photo |\n"
    "| [Recipe Sorter](ideas/rs.md) | refining | Sort recipes |\n"
)


def _write_index(tmp_path, body=_GRAD_INDEX):
    """Write an incubator index file and return its path (DRY across these tests)."""
    path = tmp_path / "index.md"
    path.write_text(body, encoding="utf-8")
    return path


def test_graduate_idea_registers_graduated_idea(tmp_path):
    """A graduated idea becomes a registered project (name slugified from the title).

    Why this matters: this is the whole point of D4's follow-on — close the loop from
    "idea reached graduated" to "tracked project", reusing add-project's write path.
    """
    index = _write_index(tmp_path)
    cfg = tmp_path / "orion.toml"
    code = cli.main([
        "graduate-idea", "VLM Photo Overlay",
        "--incubator-file", str(index),
        "--repo-path", str(tmp_path),
        "--recipient", "Alex:discord:ORION_W_ALEX",
        "--config", str(cfg), "--yes",
    ])
    assert code == 0
    # The written config re-loads and contains the slugified project.
    assert "vlm-photo-overlay" in load_config(cfg).projects


def test_graduate_idea_refuses_non_graduated_without_force(tmp_path, capsys):
    """An idea that is not 'graduated' is refused (and nothing is written).

    Why this matters: the command's semantics are "graduate a graduated idea"; a
    mistaken non-graduated target should fail loudly, not silently register.
    """
    index = _write_index(tmp_path)
    cfg = tmp_path / "orion.toml"
    code = cli.main([
        "graduate-idea", "Recipe Sorter",
        "--incubator-file", str(index),
        "--repo-path", str(tmp_path),
        "--recipient", "Alex:discord:ORION_W_ALEX",
        "--config", str(cfg), "--yes",
    ])
    assert code == 1
    assert not cfg.exists()  # refused before delegating to the writer
    assert "not 'graduated'" in capsys.readouterr().err


def test_graduate_idea_force_allows_non_graduated(tmp_path):
    """--force graduates an idea regardless of its status.

    Why this matters: the override exists for the deliberate case; it must actually
    bypass the status gate and register the project.
    """
    index = _write_index(tmp_path)
    cfg = tmp_path / "orion.toml"
    code = cli.main([
        "graduate-idea", "Recipe Sorter", "--force",
        "--incubator-file", str(index),
        "--repo-path", str(tmp_path),
        "--recipient", "Alex:discord:ORION_W_ALEX",
        "--config", str(cfg), "--yes",
    ])
    assert code == 0
    assert "recipe-sorter" in load_config(cfg).projects


def test_graduate_idea_not_found_lists_graduated(tmp_path, capsys):
    """An unknown idea fails and names the graduated ideas that ARE available.

    Why this matters: a typo'd title should be a one-glance fix, not a dead end —
    mirroring get_project listing known projects.
    """
    index = _write_index(tmp_path)
    code = cli.main([
        "graduate-idea", "Nonexistent Idea",
        "--incubator-file", str(index),
        "--config", str(tmp_path / "orion.toml"),
    ])
    assert code == 1
    err = capsys.readouterr().err
    assert "not found" in err and "VLM Photo Overlay" in err


def test_graduate_idea_name_override(tmp_path):
    """--name overrides the derived slug.

    Why this matters: the slug is a default, not a constraint; the user can choose
    the project key explicitly.
    """
    index = _write_index(tmp_path)
    cfg = tmp_path / "orion.toml"
    code = cli.main([
        "graduate-idea", "VLM Photo Overlay", "--name", "vlm",
        "--incubator-file", str(index),
        "--repo-path", str(tmp_path),
        "--recipient", "Alex:discord:ORION_W_ALEX",
        "--config", str(cfg), "--yes",
    ])
    assert code == 0
    projects = load_config(cfg).projects
    assert "vlm" in projects and "vlm-photo-overlay" not in projects


def test_graduate_idea_print_writes_nothing(tmp_path, capsys):
    """--print shows the stanza (with the slugified name) and writes no config.

    Why this matters: the review-before-write affordance carries through from
    add-project; a --print run must never create the file.
    """
    index = _write_index(tmp_path)
    cfg = tmp_path / "orion.toml"
    code = cli.main([
        "graduate-idea", "VLM Photo Overlay",
        "--incubator-file", str(index),
        "--repo-path", str(tmp_path),
        "--recipient", "Alex:discord:ORION_W_ALEX",
        "--config", str(cfg), "--print",
    ])
    assert code == 0
    assert not cfg.exists()
    assert "[projects.vlm-photo-overlay]" in capsys.readouterr().out


def test_graduate_idea_finds_incubator_project_from_config(tmp_path):
    """With no --incubator-file, the index is found via the configured incubator project.

    Why this matters: the intended use is a dedicated [projects.incubator]; the user
    shouldn't repeat the index path. This also exercises --like copying recipients.
    """
    index = _write_index(tmp_path)
    cfg = tmp_path / "orion.toml"
    cfg.write_text(
        f"""
        state_db = "state.sqlite3"

        [projects.incubator]
        repo_path = "{tmp_path.as_posix()}"
        collectors = ["incubator"]
        incubator_file = "{index.as_posix()}"

          [[projects.incubator.recipients]]
          name = "Mentor"
          channel = "discord"
          webhook_env_var = "ORION_W_MENTOR"
        """
    )
    code = cli.main([
        "graduate-idea", "VLM Photo Overlay",
        "--like", "incubator",
        "--repo-path", str(tmp_path),
        "--config", str(cfg), "--yes",
    ])
    assert code == 0
    project = load_config(cfg).projects["vlm-photo-overlay"]
    # Recipients were copied from the incubator project via --like.
    assert project.recipients[0].name == "Mentor"


# --- KI-43 (AU1-R F4): graduate-idea shares add-project's flags ------------------
# graduate-idea's flags began as a verbatim copy of add-project's. add-project later grew
# --tracker-file and --seed-tasks-from; the copy did not, so graduating into a
# tracker-carrying project was IMPOSSIBLE (the tracker collector requires a path and there
# was no flag to supply one). The fix shares one parent parser, so the first test below
# pins the behaviour and the second pins the structure that keeps it true.


def test_graduate_idea_accepts_tracker_file(tmp_path):
    """`graduate-idea --collectors git,tracker --tracker-file X` registers with the tracker.

    Why this matters: this is the exact command KI-43 filed as broken. Before the fix the
    parser rejected --tracker-file outright (exit 2), and omitting it hit the
    enabled-collector-needs-a-path check in scaffold.py — so there was no way to graduate an
    idea into a tracker-carrying project without falling back to add-project by hand. The
    assertion goes all the way to the written config, not just the exit code, because the
    bug's shape was "the flag parses but the value is discarded on the way through".
    """
    index = _write_index(tmp_path)
    cfg = tmp_path / "orion.toml"
    tracker = tmp_path / "ROADMAP.md"
    code = cli.main([
        "graduate-idea", "VLM Photo Overlay",
        "--incubator-file", str(index),
        "--repo-path", str(tmp_path),
        "--collectors", "git,tracker",
        "--tracker-file", str(tracker),
        "--recipient", "Supervisor A:discord:ORION_W_A",
        "--config", str(cfg), "--yes",
    ])
    assert code == 0
    project = load_config(cfg).projects["vlm-photo-overlay"]
    assert project.collectors == ("git", "tracker")
    assert project.tracker_file == tracker
    assert not tracker.exists()  # a tracker points at a doc the user maintains


def test_graduate_idea_accepts_seed_tasks_from(tmp_path):
    """`graduate-idea --seed-tasks-from DOC` seeds the created checklist, like add-project.

    Why this matters: --seed-tasks-from is the second flag that drifted. It is asserted
    separately from --tracker-file because the two reach cmd_add_project by different
    routes (one lands in the config stanza, the other changes what gets WRITTEN to a new
    checklist file), so a single test could pass while the other path stayed broken.
    """
    index = _write_index(tmp_path)
    cfg = tmp_path / "orion.toml"
    seed_doc = tmp_path / "PLAN.md"
    seed_doc.write_text(
        "| Task | Status |\n|------|--------|\n| Ship the thing | done |\n",
        encoding="utf-8",
    )
    code = cli.main([
        "graduate-idea", "VLM Photo Overlay",
        "--incubator-file", str(index),
        "--repo-path", str(tmp_path),
        "--collectors", "git,tasks",
        "--seed-tasks-from", str(seed_doc),
        "--recipient", "Supervisor A:discord:ORION_W_A",
        "--config", str(cfg), "--yes",
    ])
    assert code == 0
    # 'tasks' with no --tasks-file defaults to <repo>/TODO.md and CREATES it; the seed doc's
    # table is what should be in it, rather than the empty starter checklist.
    created = load_config(cfg).projects["vlm-photo-overlay"].tasks_file
    assert created is not None and created.exists()
    assert "Ship the thing" in created.read_text(encoding="utf-8")


def test_add_project_and_graduate_idea_flag_sets_cannot_drift(capsys):
    """The two commands' flag sets differ ONLY by graduate-idea's idea-specific three.

    Why this matters: this is the anti-drift guard, and it is the actual point of the fix —
    KI-43 was not one missing flag, it was a duplicated flag list nobody diffed for months.
    Adding a flag to one command and not the other now fails here with a readable diff.

    How it works: both commands' `--help` output is captured (argparse exits 0 after
    printing) and the `--flag` tokens extracted, so the assertion runs against the REAL
    parsers rather than a hand-kept list. The parser is built inline inside main(), so
    there is no build_parser() to introspect directly — capturing help is what reaches it
    without restructuring main().

    The three expected differences are intentional and load-bearing:
      --name, --incubator, --force  exist only on graduate-idea (they are about locating and
                                    vetting the IDEA, which add-project knows nothing about).
    Note --incubator-file is in NEITHER direction: both commands accept it, with genuinely
    different meanings (the new project's collector file vs. the index to read), which is
    exactly why it is excluded from the shared parent.
    """
    def flags_of(command):
        with pytest.raises(SystemExit) as exc:
            cli.main([command, "--help"])
        assert exc.value.code == 0
        return set(re.findall(r"--[a-z][a-z0-9-]*", capsys.readouterr().out))

    add_flags = flags_of("add-project")
    graduate_flags = flags_of("graduate-idea")

    assert add_flags - graduate_flags == set(), (
        "add-project grew flags graduate-idea lacks — put them on the shared parent parser "
        "(_project_registration_parser), not on add-project alone. This is KI-43 recurring."
    )
    assert graduate_flags - add_flags == {"--name", "--incubator", "--force"}

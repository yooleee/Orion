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
from zoneinfo import ZoneInfo

from orion import cli
from orion.config import load_config

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


def _write_relay_config(tmp_path, repo, *, enabled=True):
    """Write an orion.toml with a [relay] table pointing at a fake relay URL.

    Args:
        tmp_path: per-test temp dir (also where the state db lives).
        repo: path to the git repo (used by the git collector).
        enabled: whether the [relay] table is enabled.

    Why:
        The relay tests need a project plus a [relay] table; this keeps each test to
        the enabled/disabled case it cares about instead of repeating TOML (DRY).
    """
    toml = tmp_path / "orion.toml"
    toml.write_text(
        f"""
        state_db = "state.sqlite3"

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


# --- `orion comments` pull-back (C2) ------------------------------------------
#
# These pin the pull-back command WITHOUT any network call: cli.pull_comments is
# monkeypatched to a recorder that returns a canned relay response, so the tests
# assert on the since_id the CLI passes (the watermark behavior), the output shape,
# and the error paths — never on a real relay.


def _capture_pull(mp, response):
    """Monkeypatch cli.pull_comments to record calls and return a canned response.

    Args:
        mp: pytest monkeypatch.
        response: the dict pull_comments should return (a {"comments", "latest_id"}).

    Returns:
        A `calls` list; each entry is the (project, since_id) the CLI passed, so a
        test can assert what watermark the pull used (the whole point of the cursor).

    Why:
        Mirrors _capture_relay — replace the real client with an in-memory recorder so
        the command's logic (watermark read/advance, output, errors) is tested with no
        relay running.
    """
    calls: list[tuple[str, int]] = []

    def fake_pull(relay_url, token, project, since_id, **_kw):
        calls.append((project, since_id))
        return response

    mp.setattr(cli, "pull_comments", fake_pull)
    return calls


def _two_comments():
    """A canned relay response with two comments (latest_id = the higher id).

    Why:
        Shared fixture-shaped helper so each test states only what it asserts; the
        created_at values are real ISO-8601 UTC so the human formatter can parse them.
    """
    return {
        "comments": [
            {"id": 1, "report_id": 10, "author": "Alex", "body": "Looks great.",
             "created_at": "2026-06-19T19:30:00+00:00"},
            {"id": 2, "report_id": 10, "author": "", "body": "One nit on naming.",
             "created_at": "2026-06-19T20:00:00+00:00"},
        ],
        "latest_id": 2,
    }


def test_comments_default_shows_new_and_advances_watermark(tmp_path, monkeypatch, capsys):
    """A default pull shows comments, then advances the watermark to latest_id.

    Why this matters: the unread cursor is the feature. The FIRST pull starts at
    since_id=0 (full history); after it, the watermark must advance so the SECOND pull
    starts at the latest_id of the first (2 here) — only what is newer. We assert both
    the since_id sequence and that the comment text reached stdout.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)  # ignore real .env
    monkeypatch.setenv("ORION_RELAY_TOKEN", "relay-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_config(tmp_path, repo)
    calls = _capture_pull(monkeypatch, _two_comments())

    assert cli.main(["comments", "demo", "--config", str(toml)]) == 0
    out = capsys.readouterr().out
    assert "Alex" in out and "Looks great." in out
    assert "(anonymous)" in out  # the empty-author comment shows a placeholder

    # Second run: the watermark advanced to latest_id (2) after the first pull.
    assert cli.main(["comments", "demo", "--config", str(toml)]) == 0
    assert [since for _project, since in calls] == [0, 2]


def test_comments_all_shows_everything_without_advancing(tmp_path, monkeypatch, capsys):
    """`--all` pulls from since_id=0 and leaves the watermark untouched.

    Why this matters: --all is the explicit re-read escape hatch. It must always start
    at 0 (show everything) AND must not move the cursor, so a normal run afterward still
    resumes from where the last NORMAL run left it. We advance the watermark with a
    default run (to 2), then assert --all uses 0 and a following default run still uses 2.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_TOKEN", "relay-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_config(tmp_path, repo)
    calls = _capture_pull(monkeypatch, _two_comments())

    cli.main(["comments", "demo", "--config", str(toml)])              # default -> advances to 2
    cli.main(["comments", "demo", "--config", str(toml), "--all"])     # --all -> since_id 0
    cli.main(["comments", "demo", "--config", str(toml)])              # default -> still 2

    assert [since for _project, since in calls] == [0, 0, 2]


def test_comments_json_emits_the_raw_response(tmp_path, monkeypatch, capsys):
    """`--json` prints exactly the relay response dict (for the session skill).

    Why this matters: the skill parses this output, so it must be valid JSON equal to
    what pull_comments returned (comments + latest_id), not the human listing.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_TOKEN", "relay-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_config(tmp_path, repo)
    response = _two_comments()
    _capture_pull(monkeypatch, response)

    assert cli.main(["comments", "demo", "--config", str(toml), "--json"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out) == response


def test_comments_empty_default_is_friendly_and_advances(tmp_path, monkeypatch, capsys):
    """With nothing new, the default prints a 'no new comments' line and is exit 0.

    Why this matters: a quiet result must read as a deliberate answer, not silence, and
    still be a clean success. latest_id echoes since_id, so advancing is a safe no-op.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_TOKEN", "relay-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_config(tmp_path, repo)
    _capture_pull(monkeypatch, {"comments": [], "latest_id": 0})

    assert cli.main(["comments", "demo", "--config", str(toml)]) == 0
    out = capsys.readouterr().out
    assert "No new comments" in out


def test_comments_disabled_relay_is_clean_error(tmp_path, monkeypatch, capsys):
    """With no relay enabled, `comments` fails cleanly (exit 1) and never pulls.

    Why this matters: comments live on the relay; asking to read them without one is a
    user error, surfaced as a clear message rather than a crash or an empty result.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    repo = _make_repo(tmp_path)
    toml = _write_relay_config(tmp_path, repo, enabled=False)
    calls = _capture_pull(monkeypatch, _two_comments())

    assert cli.main(["comments", "demo", "--config", str(toml)]) == 1
    assert calls == []  # never reached the pull
    assert "no relay" in capsys.readouterr().err.lower()


def test_comments_missing_token_is_clean_error_and_never_pulls(tmp_path, monkeypatch, capsys):
    """A missing relay token fails with a named SecretsError (exit 1), no pull.

    Why this matters: the token gates the authenticated pull; a missing one must be a
    clean error naming the variable (never its value), caught before any request.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("ORION_RELAY_TOKEN", raising=False)  # token absent
    repo = _make_repo(tmp_path)
    toml = _write_relay_config(tmp_path, repo)
    calls = _capture_pull(monkeypatch, _two_comments())

    assert cli.main(["comments", "demo", "--config", str(toml)]) == 1
    assert calls == []
    assert "ORION_RELAY_TOKEN" in capsys.readouterr().err  # names the var


def test_comments_pull_failure_does_not_advance_watermark(tmp_path, monkeypatch):
    """A failed pull (DeliveryError) is exit 1 and leaves the watermark unmoved.

    Why this matters: the cursor must only advance on a SUCCESSFUL pull — otherwise a
    transient relay outage would skip comments forever. We make the first pull raise,
    then let a second succeed and assert it still starts at since_id=0 (never advanced).
    """
    from orion.delivery import DeliveryError

    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_RELAY_TOKEN", "relay-secret")
    repo = _make_repo(tmp_path)
    toml = _write_relay_config(tmp_path, repo)

    # First call raises; the recorder below replaces it for the second call.
    monkeypatch.setattr(
        cli,
        "pull_comments",
        lambda *a, **k: (_ for _ in ()).throw(DeliveryError("relay down")),
    )
    assert cli.main(["comments", "demo", "--config", str(toml)]) == 1

    # A subsequent successful pull must still start at 0 — the failure advanced nothing.
    calls = _capture_pull(monkeypatch, _two_comments())
    assert cli.main(["comments", "demo", "--config", str(toml)]) == 0
    assert calls == [("demo", 0)]


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


# --- `orion bot` CLI adapter (C2-bots) ----------------------------------------
#
# These pin the `orion bot` command's setup WITHOUT starting a real listener:
# cli._load_run_bot is monkeypatched to return a recorder, so run_bot (which would
# block on a Socket Mode connection) is never really called. They assert the
# enable-gates ([bot] and [relay]), the secret resolution, and the args reaching
# run_bot.


def _write_bot_config(tmp_path, repo, *, bot_enabled=True, relay_enabled=True):
    """Write an orion.toml with [relay] and [bot] tables for the bot-command tests.

    Args:
        tmp_path: per-test temp dir (also where the state db lives).
        repo: path to the git repo (used only to make the project valid).
        bot_enabled / relay_enabled: toggles for the two enable-gate tests.

    Why:
        cmd_bot requires both an enabled [bot] (what to run) and an enabled [relay]
        (its write target). This keeps each test to the toggle it exercises (DRY),
        mirroring _write_relay_config.
    """
    toml = tmp_path / "orion.toml"
    toml.write_text(
        f"""
        state_db = "state.sqlite3"

        [relay]
        enabled = {str(relay_enabled).lower()}
        url = "https://relay.test/ingest"
        token_env_var = "ORION_RELAY_TOKEN"

        [bot]
        enabled = {str(bot_enabled).lower()}
        platform = "slack"
        token_env_var = "ORION_SLACK_BOT_TOKEN"
        app_token_env_var = "ORION_SLACK_APP_TOKEN"

          [[bot.channels]]
          channel_id = "C07ABC123"
          project = "demo"

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


def test_bot_dispatches_with_resolved_args(tmp_path, monkeypatch):
    """`orion bot` resolves both tokens + the relay token and calls run_bot with the map.

    Why this matters: this is the whole job of the CLI adapter — turn the [bot]/[relay]
    config and the three .env secrets into a run_bot call. We patch _load_run_bot so
    nothing connects, and assert the bot token, app token, channel map, relay url, and
    relay token all reach run_bot as resolved.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)  # ignore real .env
    monkeypatch.setenv("ORION_SLACK_BOT_TOKEN", "xoxb-123")
    monkeypatch.setenv("ORION_SLACK_APP_TOKEN", "xapp-456")
    monkeypatch.setenv("ORION_RELAY_TOKEN", "relay-secret")
    calls = []
    monkeypatch.setattr(
        cli,
        "_load_run_bot",
        lambda: (
            lambda bot_token, app_token, channel_map, relay_url, relay_token: calls.append(
                (bot_token, app_token, channel_map, relay_url, relay_token)
            )
        ),
    )

    repo = _make_repo(tmp_path)
    toml = _write_bot_config(tmp_path, repo)
    code = cli.main(["bot", "--config", str(toml)])

    assert code == 0
    assert len(calls) == 1
    bot_token, app_token, channel_map, relay_url, relay_token = calls[0]
    assert bot_token == "xoxb-123"
    assert app_token == "xapp-456"
    assert channel_map == {"C07ABC123": "demo"}
    assert relay_url == "https://relay.test/ingest"
    assert relay_token == "relay-secret"


def test_bot_disabled_is_clean_error_and_never_runs(tmp_path, monkeypatch):
    """A disabled [bot] fails cleanly (exit 1) and never calls run_bot.

    Why this matters: the bot is opt-in; running `orion bot` with it off must be a
    clear, actionable error, not a crash — and the listener must never start.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    ran = []
    monkeypatch.setattr(cli, "_load_run_bot", lambda: (lambda *a: ran.append(a)))

    repo = _make_repo(tmp_path)
    toml = _write_bot_config(tmp_path, repo, bot_enabled=False)
    code = cli.main(["bot", "--config", str(toml)])

    assert code == 1
    assert ran == []  # never started


def test_bot_requires_enabled_relay(tmp_path, monkeypatch):
    """`orion bot` with [bot] on but [relay] off fails cleanly (exit 1), never runs.

    Why this matters: the bot writes replies INTO the relay, so a missing write target
    is a setup error caught before the listener starts — not a confusing failure on the
    first reply.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ORION_SLACK_BOT_TOKEN", "xoxb-123")
    monkeypatch.setenv("ORION_SLACK_APP_TOKEN", "xapp-456")
    ran = []
    monkeypatch.setattr(cli, "_load_run_bot", lambda: (lambda *a: ran.append(a)))

    repo = _make_repo(tmp_path)
    toml = _write_bot_config(tmp_path, repo, relay_enabled=False)
    code = cli.main(["bot", "--config", str(toml)])

    assert code == 1
    assert ran == []


def test_bot_missing_token_is_clean_error_and_never_runs(tmp_path, monkeypatch):
    """A missing bot token fails cleanly (exit 1) and never starts the listener.

    Why this matters: a bot with no token can't authenticate to Slack; catching the gap
    here turns it into a clear SecretsError naming the variable, before any connection.
    """
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("ORION_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setenv("ORION_SLACK_APP_TOKEN", "xapp-456")
    monkeypatch.setenv("ORION_RELAY_TOKEN", "relay-secret")
    ran = []
    monkeypatch.setattr(cli, "_load_run_bot", lambda: (lambda *a: ran.append(a)))

    repo = _make_repo(tmp_path)
    toml = _write_bot_config(tmp_path, repo)
    code = cli.main(["bot", "--config", str(toml)])

    assert code == 1
    assert ran == []


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

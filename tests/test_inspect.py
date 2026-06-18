# =============================================================================
# tests/test_inspect.py
# -----------------------------------------------------------------------------
# Responsible for: The read-only config-inspect commands (B6) — `projects`,
#                  `show`, and (CP2) `check`.
# Role in project: These give visibility into the config without editing it
#                  (Orion never writes config). The tests pin that they print the
#                  right facts, fail cleanly on a bad config / unknown project,
#                  and — important — never print a secret VALUE (the config holds
#                  env-var NAMES and paths, not secrets).
# =============================================================================

from pathlib import Path

from orion import cli


def _write(tmp_path: Path, text: str) -> Path:
    """Write an orion.toml into tmp_path and return its path (DRY across tests)."""
    path = tmp_path / "orion.toml"
    path.write_text(text)
    return path


_TWO_PROJECTS = """
state_db = "state.sqlite3"

[projects.alpha]
repo_path = "/tmp/alpha"
auto_send = true
collectors = ["git", "tasks"]
tasks_file = "TODO.md"

  [[projects.alpha.recipients]]
  name = "Alex"
  channel = "discord"
  webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"

[projects.beta]
repo_path = "/tmp/beta"

  [[projects.beta.recipients]]
  name = "Sam"
  channel = "slack"
  webhook_env_var = "ORION_SLACK_WEBHOOK_SAM"
"""


def test_projects_lists_every_project_with_key_facts(tmp_path, capsys):
    """`projects` lists all projects with auto_send, share level, and channels.

    Why this matters: this is the command that resolves the visibility gap (KI-15)
    — at a glance you can see which projects exist and, crucially, whether each has
    opted into auto_send. We confirm both projects appear with their (differing)
    auto_send values and their channels.
    """
    toml = _write(tmp_path, _TWO_PROJECTS)

    code = cli.main(["projects", "--config", str(toml)])
    assert code == 0

    out = capsys.readouterr().out
    assert "alpha" in out and "beta" in out          # every project listed
    assert "true" in out and "false" in out          # alpha opted in, beta not
    assert "Alex (discord)" in out
    assert "Sam (slack)" in out


def test_show_prints_resolved_fields_without_secret_values(tmp_path, capsys):
    """`show <project>` prints the resolved config — names/paths, never a secret.

    Why this matters: the per-project detail view must surface the webhook ENV-VAR
    NAME (so you can check your wiring) but never a secret value — the URL lives in
    .env and never enters the config. We assert the var name shows and that no URL
    leaks into the output.
    """
    toml = _write(tmp_path, _TWO_PROJECTS)

    code = cli.main(["show", "alpha", "--config", str(toml)])
    assert code == 0

    out = capsys.readouterr().out
    # `show` prints Path(repo_path), which renders with OS-NATIVE separators — on
    # Windows "/tmp/alpha" becomes "\tmp\alpha". Compare against that rendering, not
    # the POSIX literal, so the assertion holds on every OS (caught by CI on Windows).
    expected_repo = str(Path("/tmp/alpha"))
    assert "repo_path:" in out and expected_repo in out
    assert "auto_send:    true" in out
    assert "git, tasks" in out                        # collectors
    assert "webhook_env_var=ORION_DISCORD_WEBHOOK_ALEX" in out  # the NAME
    assert "https://" not in out                       # no URL/secret value


def test_show_unknown_project_errors(tmp_path, capsys):
    """`show` on an unknown project fails cleanly (exit 1) and names known ones.

    Why this matters: a typo'd project should get the same helpful ConfigError the
    rest of the CLI gives — the known-names list — not a traceback.
    """
    toml = _write(tmp_path, _TWO_PROJECTS)

    code = cli.main(["show", "nope", "--config", str(toml)])
    assert code == 1
    err = capsys.readouterr().err
    assert "nope" in err and "alpha" in err            # names the bad + the known


def test_projects_on_invalid_config_errors(tmp_path, capsys):
    """An invalid config makes `projects` fail cleanly (exit 1).

    Why this matters: the inspect commands load+validate like every other command,
    so a malformed config surfaces here as a clear error, not a crash.
    """
    toml = _write(tmp_path, 'state_db = "state.sqlite3"\n')  # no [projects.*]
    code = cli.main(["projects", "--config", str(toml)])
    assert code == 1
    assert "Error" in capsys.readouterr().err


# --- CP2: `check` (validity + readiness) -------------------------------------
#
# check loads secrets like a real run. load_secrets -> load_dotenv() finds the
# real repo .env (python-dotenv searches from the calling module's directory, not
# the CWD), which would pollute these tests. So we no-op load_dotenv and drive the
# environment purely through monkeypatch — isolating check's readiness LOGIC from
# .env discovery (which is covered separately in test_secrets.py).


def _no_dotenv(monkeypatch):
    """Make load_secrets a pure env reader by no-opping its load_dotenv calls."""
    monkeypatch.setattr("orion.secrets.load_dotenv", lambda *a, **k: None)


def _ready_config(tmp_path):
    """A minimal, valid single-project config whose repo_path (tmp_path) exists."""
    return _write(
        tmp_path,
        f"""
        state_db = "state.sqlite3"

        [projects.demo]
        repo_path = "{tmp_path.as_posix()}"

          [[projects.demo.recipients]]
          name = "Alex"
          channel = "discord"
          webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )


def test_check_ready_config_exits_zero(tmp_path, monkeypatch, capsys):
    """A valid, fully-provisioned config reports ready and exits 0.

    Why this matters: this is the green pre-flight path — config parses, the git
    repo path exists, and both required secrets are present — so `check` should
    confirm readiness with a zero exit a setup script can trust.
    """
    _no_dotenv(monkeypatch)  # only our monkeypatched env applies (ignore real .env)
    monkeypatch.setenv("ORION_DISCORD_WEBHOOK_ALEX", "https://discord.test/webhook")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    toml = _ready_config(tmp_path)

    code = cli.main(["check", "--config", str(toml)])
    out = capsys.readouterr().out
    assert code == 0
    assert "Config valid" in out
    assert "Ready to send" in out


def test_check_missing_webhook_is_flagged_without_leaking_values(tmp_path, monkeypatch, capsys):
    """A missing webhook secret fails the check (exit 1), named not valued.

    Why this matters: `check`'s whole point is catching "not actually set up to
    send" before a run does. The Anthropic key is present but the webhook var is
    unset, so the check must flag exactly that variable by NAME as MISSING and
    exit non-zero — and it must never print a secret value (we also seed a set
    var and confirm its value never appears).
    """
    _no_dotenv(monkeypatch)  # isolate from the real .env
    monkeypatch.delenv("ORION_DISCORD_WEBHOOK_ALEX", raising=False)  # the gap
    monkeypatch.setenv("ANTHROPIC_API_KEY", "super-secret-key-value")  # set, must not print
    toml = _ready_config(tmp_path)

    code = cli.main(["check", "--config", str(toml)])
    captured = capsys.readouterr()
    assert code == 1
    assert "ORION_DISCORD_WEBHOOK_ALEX" in captured.out  # named...
    assert "MISSING" in captured.out                      # ...and flagged
    assert "Not ready" in captured.err
    # No secret VALUE is ever printed — only names and set/MISSING.
    assert "super-secret-key-value" not in captured.out
    assert "super-secret-key-value" not in captured.err


def test_check_invalid_config_exits_one(tmp_path, capsys):
    """An invalid config fails `check` with a clean error (exit 1).

    Why this matters: validity is the first half of check — a malformed config
    should surface the ConfigError, not a readiness report.
    """
    toml = _write(tmp_path, 'state_db = "state.sqlite3"\n')  # no [projects.*]
    code = cli.main(["check", "--config", str(toml)])
    assert code == 1
    assert "Error" in capsys.readouterr().err


def _local_summarizer_config(tmp_path, *, api_key_env: str | None = None):
    """A valid config whose summarizer is the local backend (git lane enabled)."""
    api_key_line = f'api_key_env = "{api_key_env}"' if api_key_env else ""
    return _write(
        tmp_path,
        f"""
        state_db = "state.sqlite3"

        [summarizer]
        provider = "local"
        base_url = "http://localhost:11434/v1"
        model = "llama3.1"
        {api_key_line}

        [projects.demo]
        repo_path = "{tmp_path.as_posix()}"

          [[projects.demo.recipients]]
          name = "Alex"
          channel = "discord"
          webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )


def test_check_local_backend_needs_no_api_key(tmp_path, monkeypatch, capsys):
    """A local-backend project is ready WITHOUT any Anthropic key (B4).

    Why this matters: this is the local-first payoff made visible in pre-flight —
    with provider='local' and no api_key_env, no API key is required, so check must
    confirm readiness (exit 0) even with ANTHROPIC_API_KEY entirely unset, and say
    so explicitly rather than flag a missing key.
    """
    _no_dotenv(monkeypatch)
    monkeypatch.setenv("ORION_DISCORD_WEBHOOK_ALEX", "https://discord.test/webhook")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # deliberately absent
    toml = _local_summarizer_config(tmp_path)

    code = cli.main(["check", "--config", str(toml)])
    out = capsys.readouterr().out
    assert code == 0
    assert "needs no API key" in out
    assert "Ready to send" in out


def test_check_local_backend_with_missing_named_key_is_flagged(tmp_path, monkeypatch, capsys):
    """A local backend that names an api_key_env flags it MISSING when unset.

    Why this matters: the rare keyed endpoint must be held to the same readiness
    bar — the user-named variable is reported MISSING by NAME and fails the check,
    just like a webhook secret.
    """
    _no_dotenv(monkeypatch)
    monkeypatch.setenv("ORION_DISCORD_WEBHOOK_ALEX", "https://discord.test/webhook")
    monkeypatch.delenv("LOCAL_LLM_KEY", raising=False)  # the named-but-unset key
    toml = _local_summarizer_config(tmp_path, api_key_env="LOCAL_LLM_KEY")

    code = cli.main(["check", "--config", str(toml)])
    captured = capsys.readouterr()
    assert code == 1
    assert "LOCAL_LLM_KEY" in captured.out  # named...
    assert "MISSING" in captured.out         # ...and flagged
    assert "Not ready" in captured.err


# --- C1 (CP4): `check` reports relay readiness --------------------------------


def _relay_config(tmp_path, *, enabled=True):
    """A valid config with an enabled [relay] table (git lane + a webhook)."""
    return _write(
        tmp_path,
        f"""
        state_db = "state.sqlite3"

        [relay]
        enabled = {str(enabled).lower()}
        url = "https://relay.test/ingest"
        token_env_var = "ORION_RELAY_TOKEN"

        [projects.demo]
        repo_path = "{tmp_path.as_posix()}"

          [[projects.demo.recipients]]
          name = "Alex"
          channel = "discord"
          webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )


def test_check_relay_token_present_is_ok(tmp_path, monkeypatch, capsys):
    """With the relay enabled and its token set, check reports it OK and exits 0.

    Why this matters: when everything is wired, the relay token should show as set
    (by NAME) alongside the other readiness lines, confirming the dashboard push
    will work — without weakening the green verdict.
    """
    _no_dotenv(monkeypatch)
    monkeypatch.setenv("ORION_DISCORD_WEBHOOK_ALEX", "https://discord.test/webhook")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ORION_RELAY_TOKEN", "relay-secret-value")  # set, must not print
    toml = _relay_config(tmp_path)

    code = cli.main(["check", "--config", str(toml)])
    captured = capsys.readouterr()
    assert code == 0
    assert "ORION_RELAY_TOKEN" in captured.out  # named and shown OK
    assert "Ready to send" in captured.out
    # The token VALUE is never printed — only its name and set/MISSING.
    assert "relay-secret-value" not in captured.out


def test_check_relay_token_missing_is_a_warning_not_a_failure(tmp_path, monkeypatch, capsys):
    """An enabled relay with a missing token WARNS but does not fail the check.

    Why this matters: the relay is fail-soft and additive — a report still sends
    fine without it. So a missing relay token must be a warning (exit 0, "ready to
    send"), NOT a hard failure, keeping check's exit code a faithful "core delivery
    ready?" gate. The gap is still surfaced by NAME so the user can fix it.
    """
    _no_dotenv(monkeypatch)
    monkeypatch.setenv("ORION_DISCORD_WEBHOOK_ALEX", "https://discord.test/webhook")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("ORION_RELAY_TOKEN", raising=False)  # the gap
    toml = _relay_config(tmp_path)

    code = cli.main(["check", "--config", str(toml)])
    captured = capsys.readouterr()
    assert code == 0                                # warning, not a failure
    assert "ORION_RELAY_TOKEN" in captured.out      # named...
    assert "MISSING" in captured.out                 # ...and flagged
    assert "Ready to send" in captured.out           # but still ready
    assert "warning" in captured.out                 # surfaced as a warning


def test_check_relay_disabled_is_silent(tmp_path, monkeypatch, capsys):
    """A disabled relay produces no relay line in check output.

    Why this matters: check should stay focused on what a run actually needs; an
    opt-out relay isn't a readiness concern, so it shouldn't add noise or mention a
    token var the user never configured.
    """
    _no_dotenv(monkeypatch)
    monkeypatch.setenv("ORION_DISCORD_WEBHOOK_ALEX", "https://discord.test/webhook")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    toml = _relay_config(tmp_path, enabled=False)

    code = cli.main(["check", "--config", str(toml)])
    out = capsys.readouterr().out
    assert code == 0
    assert "ORION_RELAY_TOKEN" not in out  # disabled -> not mentioned at all

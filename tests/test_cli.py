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

# =============================================================================
# tests/conftest.py
# -----------------------------------------------------------------------------
# Responsible for: Shared test helpers and fixtures for the CLI-level tests
#                  (test_cli.py and test_schedule.py), which both drive the full
#                  report pipeline against a REAL temporary git repo with only the
#                  LLM call and the network POST mocked.
# Role in project: Keeps the end-to-end setup (a throwaway repo, a config writer,
#                  the env-var + LLM + delivery mocks, and the scripted preview
#                  answer) in ONE place so each test file states only what it
#                  cares about (DRY). pytest auto-discovers conftest.py, so the
#                  `env_and_mocks` fixture is available to every test without an
#                  import; the plain helper functions are imported explicitly
#                  (`from conftest import _make_repo, ...`).
# =============================================================================

import subprocess

import pytest

from orion import cli


def _run(repo, *args):
    """Run a git command in a test repo, raising on failure.

    Args:
        repo: Path to the git repo to run the command in.
        *args: The git subcommand and its arguments.

    Returns:
        None. Raises CalledProcessError if git exits non-zero.

    Why:
        Tests build real repos so the git collector runs for real; this wrapper
        keeps each git call to one readable line and fails loudly on a bad setup.
    """
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_repo(tmp_path, name="repo"):
    """Create a git repo with one commit and return its path.

    Args:
        tmp_path: pytest's per-test temporary directory.
        name: subdirectory name for the repo. Defaults to "repo" so existing
            single-repo callers are unchanged; the `report --all` tests pass
            distinct names to build several repos under one tmp_path.

    Returns:
        Path to the initialized repo containing a single commit.

    Why:
        A real one-commit repo gives the git collector genuine activity to find on
        the first run, so the pipeline has something to report end to end.
    """
    repo = tmp_path / name
    repo.mkdir()
    _run(repo, "init", "-q")
    _run(repo, "config", "user.email", "t@example.com")
    _run(repo, "config", "user.name", "Tester")
    (repo / "feature.py").write_text("def f():\n    return 1\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "Add feature")
    return repo


def _write_config(tmp_path, repo, *, auto_send=None):
    """Write an orion.toml in tmp_path pointing at the repo. Returns its path.

    Args:
        tmp_path: per-test temp dir (also where the state db lives).
        repo: path to the git repo (used by the git collector).
        auto_send: when not None, emit `auto_send = true/false` for the project.
            Omitted by default so existing callers get the config's own default
            (False), and so Phase 4 tests can set it explicitly either way.

    Returns:
        Path to the written orion.toml.

    Why:
        A single shared writer means test_cli.py and test_schedule.py describe the
        same minimal project the same way; the optional auto_send keeps the Phase 4
        opt-in expressible without a second near-identical writer (DRY).
    """
    auto_send_line = ""
    if auto_send is not None:
        # TOML booleans are lowercase; render the Python bool accordingly.
        auto_send_line = f"auto_send = {str(auto_send).lower()}\n"
    toml = tmp_path / "orion.toml"
    toml.write_text(
        f"""
        state_db = "state.sqlite3"

        [projects.demo]
        repo_path = "{repo}"
        share_level = "high_level"
        collectors = ["git"]
        {auto_send_line}
          [[projects.demo.recipients]]
          name = "Alex"
          channel = "discord"
          webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """
    )
    return toml


def _answer(monkeypatch, value):
    """Script the preview confirm prompt to return `value`.

    Args:
        monkeypatch: pytest's monkeypatch fixture.
        value: the string the (replaced) input() should return ("y" / "n").

    Returns:
        None. Replaces builtins.input for the duration of the test.

    Why:
        The preview gate calls input(); scripting it lets a test confirm ("y") or
        decline ("n") deterministically with no terminal. test_schedule.py instead
        replaces input() with a call-counting spy to PROVE it is or isn't called.
    """
    monkeypatch.setattr("builtins.input", lambda prompt="": value)


@pytest.fixture
def env_and_mocks(monkeypatch):
    """Set required env vars and capture mocked LLM + delivery.

    Args:
        monkeypatch: pytest's monkeypatch fixture (also returned for further use).

    Returns:
        A dict with a `sent` list that records (message, url) tuples the (mocked)
        Discord sender was asked to deliver, plus the `monkeypatch` handle.

    Why:
        Centralizes the three mocks every CLI test needs — the Anthropic + webhook
        env vars, the LLM call, and the network POST — so each test only scripts
        the preview answer and asserts on outcomes (DRY). Nothing real leaves the
        machine: delivery is captured in memory.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ORION_DISCORD_WEBHOOK_ALEX", "https://discord.test/webhook")

    sent: list[tuple[str, str]] = []

    # Default summary echoes nothing sensitive; individual tests can override.
    monkeypatch.setattr(cli, "summarize_raw", lambda text, level, *, client: "Made progress.")
    monkeypatch.setattr(cli, "discord_send", lambda message, url: sent.append((message, url)))

    return {"sent": sent, "monkeypatch": monkeypatch}

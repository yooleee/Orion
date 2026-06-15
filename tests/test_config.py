# =============================================================================
# tests/test_config.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying that config loading resolves defaults correctly and
#                  rejects malformed configs with clear ConfigError messages.
# Role in project: Config is the first gate of every run; a bad config should
#                  fail loudly here, not confusingly downstream. These tests pin
#                  that behavior.
# =============================================================================

from pathlib import Path

import pytest

from orion.config import ConfigError, get_project, load_config


def _write(tmp_path: Path, text: str) -> Path:
    """Write a config file into a temp dir and return its path.

    Args:
        tmp_path: pytest's per-test temporary directory.
        text: TOML content to write.

    Returns:
        Path to the written orion.toml.

    Why:
        Every test needs a throwaway config on disk; this keeps each test to the
        one TOML snippet it cares about instead of repeating file I/O (DRY).
    """
    path = tmp_path / "orion.toml"
    path.write_text(text)
    return path


def test_valid_config_resolves_defaults(tmp_path):
    """A minimal valid config loads, applies defaults, and expands paths.

    Why this matters: this is the happy path the whole pipeline depends on —
    if defaults (share_level, collectors) and path resolution are wrong, every
    later step gets bad inputs.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )

    config = load_config(path)
    project = get_project(config, "demo")

    assert project.repo_path == Path("/tmp/demo")
    # Defaults: safest share level and the git collector.
    assert project.share_level == "high_level"
    assert project.collectors == ("git",)
    assert project.recipients[0].name == "Alex"
    # state_db default is resolved next to the config file, as an absolute path.
    assert config.state_db == (tmp_path / "orion.sqlite3").resolve()


def test_missing_file_raises(tmp_path):
    """A nonexistent config path gives a clear ConfigError, not FileNotFoundError.

    Why this matters: a new user who hasn't copied the example should be told to
    do exactly that.
    """
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "does_not_exist.toml")


def test_no_projects_raises(tmp_path):
    """A config with no [projects.*] tables is rejected.

    Why this matters: an empty registry is a setup mistake; reporting nothing
    silently would hide it.
    """
    path = _write(tmp_path, 'state_db = "orion.sqlite3"\n')
    with pytest.raises(ConfigError, match="no projects"):
        load_config(path)


def test_missing_repo_path_raises(tmp_path):
    """A project without repo_path fails — there is nothing to report on.

    Why this matters: repo_path is the one truly required project field.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    with pytest.raises(ConfigError, match="repo_path"):
        load_config(path)


def test_invalid_share_level_raises(tmp_path):
    """An unknown share_level is rejected so the privacy dial can't be mis-set.

    Why this matters: share_level controls how much detail leaves the machine; a
    typo here is a security-relevant mistake, so it must fail, not default away.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"
        share_level = "everything"

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    with pytest.raises(ConfigError, match="share_level"):
        load_config(path)


def test_no_recipients_raises(tmp_path):
    """A project with no recipients is rejected — delivery would be impossible.

    Why this matters: catching this at load time points at the config, far from
    the eventual send step.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"
        """,
    )
    with pytest.raises(ConfigError, match="recipients"):
        load_config(path)


def test_unknown_collector_still_rejected(tmp_path):
    """An unsupported collector name is rejected (regression for the new list).

    Why this matters: expanding SUPPORTED_COLLECTORS to add tasks/notes must not
    accidentally start accepting arbitrary names — a typo'd collector should
    still fail loudly rather than silently produce no signal.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"
        collectors = ["git", "todos"]

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    with pytest.raises(ConfigError, match="todos"):
        load_config(path)


def test_tasks_enabled_requires_tasks_file(tmp_path):
    """Enabling the tasks collector without a tasks_file is a clear ConfigError.

    Why this matters: the collector has nothing to read without a path; catching
    it at load time names the exact key to add, instead of failing mid-run.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"
        collectors = ["git", "tasks"]

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    with pytest.raises(ConfigError, match="tasks_file"):
        load_config(path)


def test_notes_enabled_requires_notes_file(tmp_path):
    """Enabling the notes collector without a notes_file is a clear ConfigError.

    Why this matters: same pairing guarantee as tasks — an enabled file collector
    must have its file configured.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"
        collectors = ["notes"]

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    with pytest.raises(ConfigError, match="notes_file"):
        load_config(path)


def test_collector_file_relative_resolved_to_config_dir(tmp_path):
    """A relative tasks_file resolves against the config file's directory.

    Why this matters: a user should be able to write `tasks_file = "TODO.md"` and
    have it found next to their config, regardless of the working directory the
    command runs from (mirrors how state_db resolves).
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"
        collectors = ["tasks"]
        tasks_file = "TODO.md"

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    project = get_project(load_config(path), "demo")
    assert project.tasks_file == (tmp_path / "TODO.md").resolve()
    # The disabled collector's path stays None.
    assert project.notes_file is None


def test_collector_file_ignored_when_collector_disabled(tmp_path):
    """A tasks_file present but the collector disabled is ignored, not an error.

    Why this matters: a user may keep a path in config while toggling the
    collector off; that should not fail, and the resolved field should be None so
    the orchestrator never runs a disabled collector.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"
        collectors = ["git"]
        tasks_file = "TODO.md"

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    project = get_project(load_config(path), "demo")
    assert project.tasks_file is None


def test_slack_recipient_is_accepted(tmp_path):
    """A recipient on the "slack" channel loads (Phase 3 widened the set).

    Why this matters: Slack delivery is pointless if config rejects a slack
    recipient; this pins that the channel is now valid.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"

        [[projects.demo.recipients]]
        name = "Sam"
        channel = "slack"
        webhook_env_var = "ORION_SLACK_WEBHOOK_SAM"
        """,
    )
    project = get_project(load_config(path), "demo")
    assert project.recipients[0].channel == "slack"


def test_unknown_channel_is_rejected(tmp_path):
    """A recipient on an unsupported channel fails with a clear ConfigError.

    Why this matters: widening SUPPORTED_CHANNELS to add slack must not start
    accepting arbitrary channel names — a typo ("slak") should still fail loudly
    rather than silently never deliver.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"

        [[projects.demo.recipients]]
        name = "Sam"
        channel = "slak"
        webhook_env_var = "ORION_SLACK_WEBHOOK_SAM"
        """,
    )
    with pytest.raises(ConfigError, match="channel"):
        load_config(path)


def test_unknown_project_lists_known(tmp_path):
    """Requesting a missing project lists the ones that exist.

    Why this matters: turns a typo'd project name into a one-glance fix.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    config = load_config(path)
    with pytest.raises(ConfigError, match="demo"):
        get_project(config, "typo")

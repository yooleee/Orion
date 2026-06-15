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

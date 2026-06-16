# =============================================================================
# tests/test_secrets.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying get_required's present/missing/blank behavior, and
#                  load_secrets' config-relative .env discovery.
# Role in project: Secrets are the one path by which an API key or webhook URL
#                  enters the program; a missing one must fail with a clear name,
#                  and a non-interactive run (git hook / scheduler) must be able
#                  to find Orion's central .env via its --config even when its
#                  working directory is some other repo.
# =============================================================================

import os

import pytest

from orion.secrets import SecretsError, get_required, load_secrets


def test_present_secret_is_returned(monkeypatch):
    """A set environment variable is returned, stripped of whitespace.

    Why this matters: trailing newlines in a copy-pasted .env value are common;
    they would silently break a webhook URL if not stripped.
    """
    monkeypatch.setenv("ORION_TEST_SECRET", "  value123  ")
    assert get_required("ORION_TEST_SECRET") == "value123"


def test_missing_secret_raises_with_name(monkeypatch):
    """An unset variable raises SecretsError naming the exact variable.

    Why this matters: the error should tell the user precisely which line to add
    to .env, not surface later as an opaque auth failure.
    """
    monkeypatch.delenv("ORION_TEST_SECRET", raising=False)
    with pytest.raises(SecretsError, match="ORION_TEST_SECRET"):
        get_required("ORION_TEST_SECRET")


def test_blank_secret_treated_as_missing(monkeypatch):
    """A whitespace-only value is treated as missing.

    Why this matters: a blank line like `KEY=` in .env is a forgotten value, not
    an intentional empty secret; failing here is safer than sending an empty key.
    """
    monkeypatch.setenv("ORION_TEST_SECRET", "   ")
    with pytest.raises(SecretsError):
        get_required("ORION_TEST_SECRET")


def test_load_secrets_finds_env_next_to_config_from_any_cwd(tmp_path, monkeypatch):
    """load_secrets(config) loads the .env beside the config, regardless of CWD.

    Why this matters: this is the fix that makes git-hook and scheduled runs work.
    They start with the working directory set to some OTHER repo, so the default
    CWD-upward .env search can't find Orion's central .env. Passing the config
    path lets load_secrets look beside orion.toml instead. We simulate that by
    putting the .env in one directory, changing CWD to an unrelated one, and
    confirming the secret still loads.
    """
    key = "ORION_TEST_ENV_NEXT_TO_CONFIG"
    monkeypatch.delenv(key, raising=False)  # ensure it isn't already set

    config_dir = tmp_path / "orion-home"
    config_dir.mkdir()
    (config_dir / ".env").write_text(f"{key}=from_config_dir\n", encoding="utf-8")
    elsewhere = tmp_path / "some_other_repo"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # CWD has no .env (and monkeypatch restores it)

    try:
        load_secrets(config_dir / "orion.toml")
        assert os.environ[key] == "from_config_dir"
    finally:
        # load_dotenv mutates os.environ directly (monkeypatch won't undo that),
        # so remove the key we introduced to keep other tests isolated.
        os.environ.pop(key, None)


def test_load_secrets_does_not_override_the_real_environment(tmp_path, monkeypatch):
    """A value already set in the environment wins over the .env file.

    Why this matters: override=False is a deliberate precedence choice — an
    exported variable (CI, or a user exporting a key) must beat a stale .env
    value. We set the var, point load_secrets at a .env that disagrees, and
    confirm the exported value survives.
    """
    key = "ORION_TEST_ENV_PRECEDENCE"
    monkeypatch.setenv(key, "from_real_env")  # monkeypatch restores this one

    config_dir = tmp_path / "orion-home"
    config_dir.mkdir()
    (config_dir / ".env").write_text(f"{key}=from_dotenv\n", encoding="utf-8")

    load_secrets(config_dir / "orion.toml")
    assert os.environ[key] == "from_real_env"  # real environment wins

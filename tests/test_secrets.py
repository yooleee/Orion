# =============================================================================
# tests/test_secrets.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying get_required's present/missing/blank behavior,
#                  load_secrets' config-relative .env discovery, and the
#                  bootstrap_env_secrets safe-write contract (CS-O PR9).
# Role in project: Secrets are the one path by which an API key or webhook URL
#                  enters the program; a missing one must fail with a clear name,
#                  and a non-interactive run (git hook / scheduler) must be able
#                  to find Orion's central .env via its --config even when its
#                  working directory is some other repo.
# =============================================================================

import os

import pytest

from orion.secrets import SecretsError, bootstrap_env_secrets, get_required, load_secrets


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


@pytest.mark.real_dotenv  # this test exercises the genuine python-dotenv loader
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


@pytest.mark.real_dotenv  # this test exercises the genuine python-dotenv loader
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


# --- bootstrap_env_secrets: generate missing relay secrets into .env, safely (CS-O PR9) ---

_NAMES = ("ORION_RELAY_USER_PEPPER", "ORION_RELAY_SESSION_KEY", "ORION_RELAY_ADMIN_TOKEN")


def _values(env_path):
    """Parse NAME=VALUE lines from a .env into a dict (test-side reader, deliberately naive)."""
    out = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            name, _, value = line.partition("=")
            out[name.strip().removeprefix("export ").strip()] = value
    return out


def test_bootstrap_creates_env_with_every_name_and_strong_values(tmp_path):
    """On a directory with no .env, every requested name is generated into a new file.

    Why this matters: the fresh-deploy case. The values must be real secrets (43-char
    urlsafe tokens from 32 random bytes) and distinct from one another, and the report
    must name them without carrying a single value.
    """
    env = tmp_path / ".env"
    report = bootstrap_env_secrets(env, _NAMES)
    assert report.generated == _NAMES
    assert report.filled == () and report.already_set == () and report.placeholders == ()
    values = _values(env)
    assert set(values) == set(_NAMES)
    assert all(len(v) == 43 for v in values.values())
    assert len(set(values.values())) == 3  # independent secrets, never one value reused
    for value in values.values():
        assert value not in repr(report)  # names only, by construction


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits do not exist on Windows")
def test_bootstrap_written_file_is_owner_only_and_leaves_no_temp_file(tmp_path):
    """The written .env is mode 0600 and no temp file lingers beside it.

    Why this matters: a secrets file must never be group/world-readable, not even for the
    instant between write and rename — so the mode is set on the temp file before the
    atomic rename, and the rename leaves nothing behind.
    """
    env = tmp_path / ".env"
    bootstrap_env_secrets(env, _NAMES)
    assert (env.stat().st_mode & 0o777) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == [".env"]


def test_bootstrap_never_overwrites_a_nonempty_value_and_preserves_the_file(tmp_path):
    """Set values are kept byte-for-byte; only the missing names are appended, in order.

    Why this matters: the operator's secret always wins. Comments, blank lines, ordering,
    an `export`-prefixed line and an unrelated key must all survive untouched — the file is
    theirs, the tool only adds what is missing.
    """
    env = tmp_path / ".env"
    original = (
        "# relay secrets\n"
        "ANTHROPIC_API_KEY=sk-keep-me\n"
        "\n"
        "export ORION_RELAY_SESSION_KEY=already-there\n"
        "ORION_RELAY_TOKEN_X=not-the-token\n"
    )
    env.write_text(original, encoding="utf-8")
    report = bootstrap_env_secrets(env, _NAMES)
    assert report.already_set == ("ORION_RELAY_SESSION_KEY",)
    assert report.generated == ("ORION_RELAY_USER_PEPPER", "ORION_RELAY_ADMIN_TOKEN")
    text = env.read_text(encoding="utf-8")
    assert text.startswith(original)  # every original byte in place, in order
    assert "ORION_RELAY_SESSION_KEY=already-there" in text
    appended = text[len(original):].splitlines()
    assert [line.split("=")[0] for line in appended] == list(report.generated)


def test_bootstrap_fills_an_empty_assignment_in_place(tmp_path):
    """`NAME=` (or `NAME=""`) is a forgotten value: it is filled on that line, not appended.

    Why this matters: an empty assignment is the "copied the template, never filled it"
    shape; leaving it AND appending a second assignment would make python-dotenv read the
    later one while the file shows two — filling in place keeps one truthful line.
    """
    env = tmp_path / ".env"
    env.write_text('A=1\nORION_RELAY_USER_PEPPER=\nORION_RELAY_ADMIN_TOKEN=""\nB=2\n', encoding="utf-8")
    report = bootstrap_env_secrets(env, _NAMES)
    assert report.filled == ("ORION_RELAY_USER_PEPPER", "ORION_RELAY_ADMIN_TOKEN")
    assert report.generated == ("ORION_RELAY_SESSION_KEY",)
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "A=1" and lines[3] == "B=2"  # neighbours untouched, order kept
    assert lines[1].startswith("ORION_RELAY_USER_PEPPER=") and len(lines[1]) > 30
    assert lines[2].startswith("ORION_RELAY_ADMIN_TOKEN=") and '""' not in lines[2]
    assert lines[4].startswith("ORION_RELAY_SESSION_KEY=")  # the truly missing one appended


def test_bootstrap_flags_example_placeholders_but_does_not_replace_them(tmp_path):
    """A value copied from .env.example is reported as a placeholder and left alone.

    Why this matters: `cp .env.example .env` is step 4 of setup, and a placeholder is the
    one "set" value that is not a secret. The tool must SAY so — but the never-overwrite
    rule still holds, because silently replacing a value the operator wrote is worse.
    """
    env = tmp_path / ".env"
    env.write_text(
        "ORION_RELAY_USER_PEPPER=replace-with-a-long-random-secret\n"
        'ORION_RELAY_SESSION_KEY="replace-with-a-long-random-secret"\n',
        encoding="utf-8",
    )
    before = env.read_bytes()
    report = bootstrap_env_secrets(env, _NAMES)
    assert report.placeholders == ("ORION_RELAY_USER_PEPPER", "ORION_RELAY_SESSION_KEY")
    assert report.already_set == report.placeholders
    assert report.generated == ("ORION_RELAY_ADMIN_TOKEN",)
    assert env.read_bytes().startswith(before)  # placeholders untouched


def test_bootstrap_preserves_crlf_newlines(tmp_path):
    """A CRLF .env stays CRLF throughout, including the appended lines.

    Why this matters: a file edited on Windows must not come back mixed — python-dotenv
    reads both, but a mixed file is the kind of drift that confuses diffs and editors.
    """
    env = tmp_path / ".env"
    env.write_bytes(b"A=1\r\nORION_RELAY_SESSION_KEY=x\r\n")
    bootstrap_env_secrets(env, _NAMES)
    raw = env.read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    assert raw.startswith(b"A=1\r\nORION_RELAY_SESSION_KEY=x\r\n")


def test_bootstrap_is_idempotent_and_does_not_rewrite_a_complete_file(tmp_path):
    """A second run reports everything already set and leaves the file bytes unchanged.

    Why this matters: re-running the bootstrap must be safe to do by habit — no rewrite,
    no permission churn, no new values.
    """
    env = tmp_path / ".env"
    bootstrap_env_secrets(env, _NAMES)
    before = env.read_bytes()
    mtime = env.stat().st_mtime_ns
    report = bootstrap_env_secrets(env, _NAMES)
    assert report.already_set == _NAMES and report.generated == () and report.filled == ()
    assert env.read_bytes() == before and env.stat().st_mtime_ns == mtime

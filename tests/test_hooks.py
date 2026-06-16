# =============================================================================
# tests/test_hooks.py
# -----------------------------------------------------------------------------
# Responsible for: The Phase B1 hook helpers in src/orion/hooks.py — the PURE
#                  hook-script builder and the git-backed hooks-directory resolver.
# Role in project: These pin the exact bytes that get written into .git/hooks/
#                  (so we can assert the safety properties — backgrounded, always
#                  exit 0, --yes, portable forward-slash paths — without executing
#                  a real hook) and that we install into the directory git actually
#                  reads, even in non-standard layouts.
# =============================================================================

import os
from pathlib import Path

import pytest

from orion import cli
from orion.collectors.git import GitError
from orion.hooks import build_hook_script, resolve_hooks_dir

# Shared real-repo builder and config writer (a one-commit git repo + an
# orion.toml pointing at it) live in conftest.
from conftest import _make_repo, _write_config


def _sample_script(hook_type="pre-push"):
    """Build a hook script from representative inputs.

    Args:
        hook_type: which hook to render ("pre-push" or "post-commit").

    Returns:
        The generated shell-script text.

    Why:
        Every content test needs the same realistic inputs; centralizing them
        keeps each test to the one property it asserts (DRY).
    """
    return build_hook_script(
        python="/home/u/proj/.venv/bin/python",
        project="demo",
        config_path=Path("/home/u/proj/orion.toml"),
        log_path=Path("/home/u/proj/.git/orion-hook.log"),
        hook_type=hook_type,
    )


def test_hook_script_is_a_backgrounded_fire_and_forget_report():
    """The generated hook delegates to `report --yes`, backgrounded, exiting 0.

    Why this matters: this is the whole safety contract of B1 in one string. The
    hook must (a) call the existing `report --yes` path (so redaction + the
    auto_send gate still apply — no new send path), (b) run in the BACKGROUND so
    it never delays a commit/push while Orion calls the LLM + posts, and (c)
    always `exit 0` so a pre-push hook can never abort the push on a report error.
    """
    script = _sample_script()

    assert script.startswith("#!/bin/sh\n")          # a real shell script
    # Delegates to the unattended report path for this project.
    assert '-m orion report "demo" --yes' in script
    assert '--config "/home/u/proj/orion.toml"' in script
    # Backgrounded (&) with output captured to the log, then an unconditional exit 0.
    assert '>> "/home/u/proj/.git/orion-hook.log" 2>&1 &' in script
    assert script.rstrip().endswith("exit 0")
    # The interpreter to use is embedded (so the venv need not be "activated").
    assert "/home/u/proj/.venv/bin/python" in script


def test_hook_script_names_its_hook_type_in_a_comment():
    """The installed file is self-describing (says which hook it is).

    Why this matters: a user later reading .git/hooks/pre-push should immediately
    see it's an Orion hook and which event it fires on, not an opaque script.
    """
    assert "# Orion pre-push hook" in _sample_script("pre-push")
    assert "# Orion post-commit hook" in _sample_script("post-commit")


def test_hook_script_uses_forward_slash_paths_only():
    """Embedded paths are POSIX (forward-slash), never backslash.

    Why this matters: git runs hooks under its bundled `sh` on Windows too, where
    a backslash is an escape character — so "C:\\Users\\..." would corrupt the
    command. build_hook_script normalizes every path via Path.as_posix(); this
    test pins that no backslash can leak into the script.
    """
    # Pass Windows-style inputs explicitly so the normalization is actually exercised.
    script = build_hook_script(
        python=r"C:\Users\u\proj\.venv\Scripts\python.exe",
        project="demo",
        config_path=Path("C:/Users/u/proj/orion.toml"),
        log_path=Path("C:/Users/u/proj/.git/orion-hook.log"),
        hook_type="pre-push",
    )
    assert "\\" not in script                           # no backslashes survive
    assert "C:/Users/u/proj/.venv/Scripts/python.exe" in script


def test_resolve_hooks_dir_points_at_the_real_hooks_dir(tmp_path):
    """resolve_hooks_dir returns the absolute .git/hooks for a normal repo.

    Why this matters: install-hook must write into the directory git actually
    reads. For a standard repo that's <repo>/.git/hooks; we confirm we resolve to
    exactly that (absolute, existing), via git rather than a hardcoded join.
    """
    repo = _make_repo(tmp_path)
    hooks = resolve_hooks_dir(repo)

    assert hooks.is_absolute()
    assert hooks.name == "hooks"
    assert hooks == (repo / ".git" / "hooks").resolve()
    assert hooks.exists()  # git creates .git/hooks on init


def test_resolve_hooks_dir_on_a_non_repo_raises_giterror(tmp_path):
    """A path that is not a git repo fails with a clean GitError.

    Why this matters: install-hook should turn "you pointed at a non-repo" into
    one clear, CLI-catchable message (the same GitError the collector uses), not a
    raw subprocess traceback.
    """
    plain = tmp_path / "not_a_repo"
    plain.mkdir()
    with pytest.raises(GitError):
        resolve_hooks_dir(plain)


# --- CP2: the `install-hook` command (cli.cmd_install_hook) -------------------


def test_install_hook_writes_an_executable_hook(tmp_path):
    """`install-hook` writes a runnable hook into the repo's real hooks dir.

    Why this matters: this is the command's core job — produce a hook git will
    actually run. We confirm it lands at <repo>/.git/hooks/pre-push (the default),
    is a `#!/bin/sh` script that calls `report --yes` for this project against the
    ABSOLUTE config, and is marked executable on POSIX (so git will run it).
    """
    repo = _make_repo(tmp_path)
    toml = _write_config(tmp_path, repo, auto_send=True)

    code = cli.main(["install-hook", "demo", "--config", str(toml)])
    assert code == 0

    hook_file = repo / ".git" / "hooks" / "pre-push"
    assert hook_file.exists()
    body = hook_file.read_text(encoding="utf-8")
    assert body.startswith("#!/bin/sh")
    assert '-m orion report "demo" --yes' in body
    assert str(toml.resolve().as_posix()) in body  # absolute, posix config path
    if os.name != "nt":  # exec bit is meaningful on POSIX; git on Windows uses sh
        assert hook_file.stat().st_mode & 0o111


def test_install_hook_supports_post_commit(tmp_path):
    """`--hook post-commit` installs the post-commit hook instead of pre-push.

    Why this matters: both triggers are supported; this pins that the choice is
    honored (right filename, right self-describing comment) so a user who wants
    on-every-commit behavior gets it.
    """
    repo = _make_repo(tmp_path)
    toml = _write_config(tmp_path, repo, auto_send=True)

    code = cli.main(["install-hook", "demo", "--hook", "post-commit", "--config", str(toml)])
    assert code == 0

    hook_file = repo / ".git" / "hooks" / "post-commit"
    assert hook_file.exists()
    assert "# Orion post-commit hook" in hook_file.read_text(encoding="utf-8")
    # And it did NOT install the default pre-push hook.
    assert not (repo / ".git" / "hooks" / "pre-push").exists()


def test_install_hook_refuses_to_clobber_without_force(tmp_path):
    """An existing hook is not overwritten unless --force is given.

    Why this matters: this is the safety guard for the command's one filesystem
    write. A repo may already use a hook manager (husky/pre-commit); silently
    replacing its hook would break the user's setup. We confirm a refusal (exit 1,
    file untouched), then that --force does replace it.
    """
    repo = _make_repo(tmp_path)
    toml = _write_config(tmp_path, repo, auto_send=True)
    hook_file = repo / ".git" / "hooks" / "pre-push"
    hook_file.write_text("#!/bin/sh\n# pre-existing hook\n", encoding="utf-8")

    # Without --force: refuse, leave the existing hook exactly as it was.
    code = cli.main(["install-hook", "demo", "--config", str(toml)])
    assert code == 1
    assert "pre-existing hook" in hook_file.read_text(encoding="utf-8")

    # With --force: replace it with Orion's hook.
    code = cli.main(["install-hook", "demo", "--config", str(toml), "--force"])
    assert code == 0
    assert "-m orion report" in hook_file.read_text(encoding="utf-8")


def test_install_hook_print_only_writes_nothing(tmp_path, capsys):
    """`--print` shows the script and installs nothing.

    Why this matters: review-before-install is the safe default for a tool that
    writes into your repo. We confirm the script is printed but no hook file is
    created.
    """
    repo = _make_repo(tmp_path)
    toml = _write_config(tmp_path, repo, auto_send=True)

    code = cli.main(["install-hook", "demo", "--config", str(toml), "--print"])
    assert code == 0
    out = capsys.readouterr().out
    assert out.startswith("#!/bin/sh")
    assert not (repo / ".git" / "hooks" / "pre-push").exists()


def test_install_hook_unknown_project_errors(tmp_path):
    """An unknown project name fails cleanly (exit 1), installing nothing.

    Why this matters: a typo'd project should give the same clear ConfigError the
    rest of the CLI gives, not a traceback or a half-written hook.
    """
    repo = _make_repo(tmp_path)
    toml = _write_config(tmp_path, repo, auto_send=True)

    code = cli.main(["install-hook", "nope", "--config", str(toml)])
    assert code == 1


def test_install_hook_warns_when_project_not_opted_in(tmp_path, capsys):
    """Installing for an auto_send=false project still works but warns.

    Why this matters: the hook calls `report --yes`, which SKIPS a project without
    auto_send — so a hook on such a project would silently do nothing. The command
    must flag that (so the user knows to set auto_send=true) while still installing
    the hook (they may flip the flag later).
    """
    repo = _make_repo(tmp_path)
    toml = _write_config(tmp_path, repo, auto_send=False)

    code = cli.main(["install-hook", "demo", "--config", str(toml)])
    assert code == 0
    assert (repo / ".git" / "hooks" / "pre-push").exists()  # installed anyway
    assert "auto_send=false" in capsys.readouterr().out      # but warned

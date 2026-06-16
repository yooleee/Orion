# =============================================================================
# hooks.py
# -----------------------------------------------------------------------------
# Responsible for: Building and locating git hooks that fire Orion automatically
#                  on a git event (Phase B1 — event-driven triggers).
# Role in project: The pure, testable helpers behind the `orion install-hook`
#                  command (cli.cmd_install_hook): generate the portable hook
#                  SCRIPT, and resolve a repo's hooks DIRECTORY. The command in
#                  cli.py does the actual file I/O; this module stays
#                  side-effect-light (build_hook_script is pure; resolve_hooks_dir
#                  only READS, via git).
# Assumptions: git is on PATH, and git runs hooks under `sh` (Git for Windows
#              bundles one), so a single `#!/bin/sh` script is portable across
#              Linux, macOS, and Windows.
# =============================================================================

from __future__ import annotations

import subprocess
from pathlib import Path

from orion.collectors.git import GitError

# The git hooks Orion can install. pre-push is the default (it batches the commits
# you're actually sharing, so it's far less noisy than firing on every commit);
# post-commit fires on every local commit. Both are CLIENT-SIDE hooks that git
# runs via sh on every OS. (git has no client-side "post-push" hook — these two
# are the local options.)
SUPPORTED_HOOKS = ("pre-push", "post-commit")


def build_hook_script(
    python: str,
    project: str,
    config_path: Path,
    log_path: Path,
    hook_type: str,
) -> str:
    """Build the portable `#!/bin/sh` git-hook script that fires an Orion report.

    Args:
        python: Absolute path to the Python interpreter to run Orion with
            (normally sys.executable — the venv's python — so the hook works even
            though a hook environment never "activates" the venv).
        project: The Orion project name to report on (the orion.toml key).
        config_path: Absolute path to the orion.toml the hook should use.
        log_path: Absolute path the hook appends its output to. Fire-and-forget
            otherwise hides errors; this log is where you look when a hook
            "seemed to do nothing".
        hook_type: Which hook this is (one of SUPPORTED_HOOKS) — recorded in a
            comment so the installed file is self-describing.

    Returns:
        The complete shell-script text (with a trailing newline).

    Why:
        Kept PURE (strings in, string out) so the exact bytes that land in
        .git/hooks/ are unit-testable without touching the filesystem. The script
        only ever calls `report ... --yes`, so every Horizon-A guarantee
        (two-pass redaction, the --yes + auto_send gate) still holds — the hook
        adds no new send path. Two deliberate choices:
          * It runs the report in the BACKGROUND (`&`) and then `exit 0`, so it
            never delays `git commit`/`git push` (Orion makes an LLM call and a
            network POST), and a pre-push hook can never ABORT the push by exiting
            non-zero on a report error.
          * Every embedded path is rendered with forward slashes (_posix) so the
            script is valid in the `sh` git uses on Windows too, where a backslash
            is an escape character.
    """
    # Forward-slash every path: the hook is sh, and git runs it under its bundled
    # sh on Windows, where "C:/Users/..." works but "C:\Users\..." would break.
    py = _posix(python)
    cfg = _posix(config_path)
    log = _posix(log_path)

    # Each field is double-quoted so a space in a path (or project name) is safe.
    # `--yes` engages the unattended path, which still only delivers projects with
    # auto_send=true (the gate lives in cli._run_report, unchanged by B1).
    command = f'"{py}" -m orion report "{project}" --yes --config "{cfg}"'

    return (
        "#!/bin/sh\n"
        f"# Orion {hook_type} hook — installed by `orion install-hook`.\n"
        "# Fire-and-forget: runs the report in the background and always exits 0,\n"
        "# so it never delays git or (for pre-push) aborts the push on an error.\n"
        f'{command} >> "{log}" 2>&1 &\n'
        "exit 0\n"
    )


def _posix(path: str | Path) -> str:
    """Render a path string with forward slashes, regardless of the host OS.

    Args:
        path: A path, as a str or Path.

    Returns:
        The path as a string with every backslash turned into a forward slash.

    Why:
        The hook is `sh`, and git runs it under `sh` even on Windows, where a
        backslash is an escape character — so a Windows path must be embedded as
        "C:/Users/..." not "C:\\Users\\...". Path.as_posix() only converts on a
        Windows Path *flavor*, so we normalize explicitly instead: this holds no
        matter which OS generates the hook, and makes the property unit-testable on
        any OS. (A literal backslash in a POSIX path is vanishingly rare and not
        worth special-casing.)
    """
    return str(path).replace("\\", "/")


def resolve_hooks_dir(repo_path: Path) -> Path:
    """Return the absolute path to a repo's git hooks directory.

    Args:
        repo_path: The repository to resolve hooks for (a project's repo_path).

    Returns:
        The absolute Path to the directory git reads hooks from.

    Why:
        We ask git (`rev-parse --git-path hooks`) instead of hardcoding
        `<repo>/.git/hooks`, because that path is wrong for linked worktrees,
        submodules, and repos that set `core.hooksPath`. git knows the truth, so
        this keeps `install-hook` correct in those setups. `--git-path` reports
        relative to git's working dir (here repo_path, via `-C`), so a relative
        result is joined back onto repo_path before being resolved to absolute.
    """
    raw = _git(repo_path, "rev-parse", "--git-path", "hooks").strip()
    hooks = Path(raw)
    if not hooks.is_absolute():
        hooks = repo_path / hooks
    return hooks.resolve()


def _git(repo_path: Path, *args: str) -> str:
    """Run a read-only git command in repo_path and return its stdout.

    Args:
        repo_path: The repo to run in (passed via `git -C`, so the process cwd is
            never changed).
        *args: The git subcommand and its arguments.

    Returns:
        Captured stdout, decoded as UTF-8.

    Why:
        A local mirror of collectors.git._git so hook resolution shares the same
        contract — list args (never shell=True), `-C` instead of chdir, and the
        shared GitError so the CLI reports a missing-git / not-a-repo problem with
        one clean message. Kept here rather than importing a private helper across
        modules; if a third git-running module appears, factor these into a shared
        util then.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    except FileNotFoundError as exc:  # `git` not installed / not on PATH.
        raise GitError("git executable not found on PATH.") from exc
    except subprocess.CalledProcessError as exc:
        # git writes the human-readable reason to stderr; surface it verbatim.
        raise GitError(f"git {' '.join(args)} failed:\n{exc.stderr.strip()}") from exc
    return completed.stdout

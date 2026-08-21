# =============================================================================
# help_snapshot.py
# -----------------------------------------------------------------------------
# Responsible for: Rendering the complete `orion --help` tree (every command,
#                  grouping parser, and leaf subcommand) into one deterministic
#                  text document, and optionally writing it to the committed
#                  fixture at tests/fixtures/cli_help_snapshot.txt.
# Role in project: The command-surface baseline for the CS-O overhaul arc
#                  (docs/command-surface-overhaul-build-kickoff.md). Every
#                  surface-changing PR regenerates the fixture so its diff shows
#                  the intentional before/after; PR10's mechanical cli.py split
#                  is pinned by a byte-diff against it. Enforced by
#                  tests/test_help_surface.py.
# Assumptions: Run with the project environment on sys.path (e.g.
#              `.venv/bin/python scripts/help_snapshot.py` from an editable
#              install). The command tree below is hand-kept, deliberately —
#              adding or removing a command is a deliberate edit here too, the
#              same rationale as tests/test_cli.py's _LEAF_COMMANDS.
# =============================================================================

import argparse
import contextlib
import io
import os
import sys
from pathlib import Path

# Determinism pins, set BEFORE importing orion.cli so nothing downstream can
# read the un-pinned values:
# - COLUMNS: argparse wraps help text to the terminal width; 80 keeps the
#   snapshot byte-stable regardless of the invoking terminal.
# - ORION_CONFIG: main() folds it into every --config flag's "(default: ...)"
#   help text, which would embed a machine-specific path in the fixture.
os.environ["COLUMNS"] = "80"
os.environ.pop("ORION_CONFIG", None)

from orion import cli  # noqa: E402  (imports after the env pins, see above)

# Every node in the command tree, root first, in the CLI's own declaration
# order: [] is `orion --help` itself; grouping parsers (discussions, relay-user,
# relay-user key, relay-user password, relay-project) are listed as nodes too,
# because their help screens are part of the surface an operator sees.
COMMAND_TREE = [
    [],
    ["report"],
    ["checklist-push"],
    ["disciplines-push"],
    ["intake"],
    ["relay-backfill"],
    ["install-hook"],
    ["add-project"],
    ["graduate-idea"],
    ["projects"],
    ["show"],
    ["check"],
    ["status"],
    ["baseline"],
    ["discussions"],
    ["discussions", "pull"],
    ["discussions", "reply"],
    ["bot"],
    ["relay-serve"],
    ["relay-user"],
    ["relay-user", "add"],
    ["relay-user", "list"],
    ["relay-user", "revoke"],
    ["relay-user", "grant"],
    ["relay-user", "ungrant"],
    ["relay-user", "key"],
    ["relay-user", "key", "add"],
    ["relay-user", "key", "list"],
    ["relay-user", "key", "revoke"],
    ["relay-user", "password"],
    ["relay-user", "password", "set"],
    ["relay-user", "password", "unlock"],
    ["relay-user", "role"],
    ["relay-user", "rename"],
    ["relay-user", "set-operator"],
    ["relay-user", "delete"],
    ["relay-project"],
    ["relay-project", "visibility"],
    ["relay-project", "lifecycle"],
]

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "cli_help_snapshot.txt"

HEADER = (
    "# Orion CLI --help snapshot (COLUMNS=80, ORION_CONFIG unset).\n"
    "# Regenerate with: python scripts/help_snapshot.py --write\n"
)


def capture_help(command: list) -> str:
    """Return one command node's `--help` output, captured in-process.

    Args:
        command: The subcommand path as a list of tokens ([] for the top level,
            e.g. ["relay-user", "key", "add"] for a nested leaf).

    Returns:
        The help text argparse printed to stdout for `orion <command> --help`.

    Why:
        The parser is built inline inside cli.main(), so there is no
        build_parser() to introspect — invoking main() with --help and catching
        the SystemExit argparse raises after printing is the only way to reach
        the real parsers (the same technique tests/test_cli.py uses).
    """
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            cli.main(command + ["--help"])
        except SystemExit as exc:
            if exc.code != 0:
                raise RuntimeError(
                    f"`orion {' '.join(command)} --help` exited {exc.code} — "
                    "is COMMAND_TREE out of date with the real surface?"
                ) from exc
        else:
            raise RuntimeError(
                f"`orion {' '.join(command)} --help` returned instead of exiting"
            )
    return out.getvalue()


def render_snapshot() -> str:
    """Render the full help tree as one labeled document.

    Returns:
        The snapshot text: a regeneration header, then each node's help output
        under a `$ orion <command> --help` banner line.

    Why:
        One flat document (rather than one file per command) keeps the fixture a
        single reviewable diff per PR and a single byte-comparison for PR10.
    """
    parts = [HEADER]
    for command in COMMAND_TREE:
        label = " ".join(["orion", *command, "--help"])
        parts.append(f"$ {label}\n{capture_help(command)}")
    return "\n".join(parts)


def main(argv: list | None = None) -> int:
    """Print the snapshot, or rewrite the fixture with --write.

    Args:
        argv: Argument list (defaults to sys.argv[1:] when None), for testability.

    Returns:
        Process exit code (always 0 unless rendering raises).

    Why:
        Printing is the default so the enforcement test can compare stdout to
        the fixture without touching the filesystem; --write is the one
        deliberate mutation path for the PR that changes the surface.
    """
    parser = argparse.ArgumentParser(
        description="Snapshot the full orion --help tree (see tests/test_help_surface.py)."
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help=f"Rewrite the committed fixture at {FIXTURE_PATH} instead of printing.",
    )
    args = parser.parse_args(argv)

    snapshot = render_snapshot()
    if args.write:
        FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n" keeps the fixture byte-identical across OSes (no CRLF
        # translation on Windows), so the enforcement test can compare exactly.
        with open(FIXTURE_PATH, "w", encoding="utf-8", newline="\n") as f:
            f.write(snapshot)
        print(f"Wrote {FIXTURE_PATH}")
    else:
        sys.stdout.write(snapshot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# =============================================================================
# tests/test_cli_entrypoints.py
# -----------------------------------------------------------------------------
# Responsible for: Pinning the CLI's IMPORT SURFACE and ENTRY POINTS — the things a
#                  mechanical restructuring of the CLI code can break without a single
#                  --help line changing (CS-O PR10, the cli.py package split).
# Role in project: Three pins, each named in the split's plan:
#                    1. every way to start Orion (`orion` console script, `python -m
#                       orion`, `python -m orion.cli`) behaves identically — same
#                       stdout, stderr and exit code;
#                    2. every `orion.cli` member the test suite reaches (patches or
#                       calls) stays importable from the package;
#                    3. importing `orion.cli` never drags in the relay package or its
#                       optional `argon2` dependency (the producer must run without the
#                       `relay` extra and without the repo checkout).
#                  The tests were added BEFORE the split against the single-module CLI,
#                  so they prove equivalence rather than describe the new shape.
# Assumptions: run from the repo (src/ layout). Subprocesses get PYTHONPATH=src and a
#              scratch cwd, so they depend on neither the editable install's mechanism
#              nor the repo root being on sys.path.
# =============================================================================
import os
import subprocess
import sys
from pathlib import Path

import pytest

from orion import cli

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

# The names tests reach on `orion.cli` — patched via monkeypatch.setattr(cli, ...) or called
# as cli.<name>. Inventoried from the suite at the time of the split (see the plan). A name
# leaving this list is a deliberate edit here, never a silent consequence of moving code.
_PATCHED_NAMES = (
    "_load_relay_serve",
    "relay_grant_projects",
    "relay_create_user",
    "relay_push",
    "slack_send",
    "compose",
    "_build_extractor",
    "relay_ungrant_projects",
    "relay_set_project_lifecycle",
    "relay_list_users",
    "pull_discussions",
    "post_discussion",
    "discord_send",
    "_build_summarizer",
    "push_checklist",
    "push_disciplines",
    "relay_revoke_user",
    "relay_add_user_key",
    "relay_list_user_keys",
    "relay_revoke_user_key",
    "relay_set_user_role",
    "relay_rename_user",
    "relay_delete_user",
)
_CALLED_NAMES = (
    "main",
    "DEFAULT_CONFIG",
    "_checklist_content_hash",
    "_is_due",
    "_is_due_at",
    "_status_is_done",
    "_reconfigure_stream_utf8",
    "_ensure_utf8_output",
    "_watch_tick",
    "_print_discussions",
    "_collect_for",
    "_seed_checklist_from_doc",
    "_checklist_payload",
    "_run_report",
    "_report_collectors_of",
    "_relay_push",
    "_redacted_about",
    "cmd_install_hook",
)


def _subprocess_env() -> dict[str, str]:
    """A clean environment for the entry-point subprocesses.

    Why:
        Strip every ORION_* variable (a developer's shell may carry ORION_CONFIG or relay
        secrets) so the three entry points see the same world, pin the encoding and width
        so --help renders identically, and put src/ on the path so `orion` imports the
        same way regardless of how the venv was installed.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("ORION_")}
    env["PYTHONPATH"] = str(SRC)
    env["PYTHONIOENCODING"] = "utf-8"
    env["COLUMNS"] = "80"
    return env


def _entry_points() -> list[tuple[str, list[str]]]:
    """The ways to start Orion: (label, argv prefix). The console script only when present."""
    modes = [
        ("python -m orion", [sys.executable, "-m", "orion"]),
        ("python -m orion.cli", [sys.executable, "-m", "orion.cli"]),
    ]
    script = Path(sys.executable).parent / ("orion.exe" if os.name == "nt" else "orion")
    if script.exists():
        modes.append(("console script", [str(script)]))
    return modes


def _run(prefix: list[str], args: list[str], cwd: Path) -> tuple[int, str, str]:
    result = subprocess.run(
        [*prefix, *args], capture_output=True, text=True, cwd=cwd, env=_subprocess_env()
    )
    return result.returncode, result.stdout, result.stderr


def test_every_entry_point_renders_the_same_help(tmp_path):
    """`orion --help` is byte-identical (stdout, empty stderr, exit 0) from every entry point.

    Why this matters: the console script, `python -m orion` and `python -m orion.cli` are
    three different import paths into the same main(). After the split, `orion.cli` is a
    package with a `__main__`, and a slip there (a missing guard, a stale re-export) shows
    up as one entry point diverging — which --help alone would never reveal for the other
    two.
    """
    results = {label: _run(prefix, ["--help"], tmp_path) for label, prefix in _entry_points()}
    assert len(results) >= 2, "expected at least the two `python -m` entry points"
    baseline = next(iter(results.values()))
    assert baseline[0] == 0 and baseline[2] == "" and "usage: orion" in baseline[1]
    for label, result in results.items():
        assert result == baseline, f"{label} diverged from the first entry point"


def test_every_entry_point_fails_a_missing_config_identically(tmp_path):
    """A clean setup error (no orion.toml) exits 1 with the message on stderr, from every entry point.

    Why this matters: exit codes and stdout/stderr placement are part of the CLI contract a
    scheduler relies on (stderr carries the verdict, stdout the detail), and they are the
    kind of thing a restructuring can shift without failing any --help pin.
    """
    results = {label: _run(prefix, ["check"], tmp_path) for label, prefix in _entry_points()}
    baseline = next(iter(results.values()))
    code, out, err = baseline
    assert code == 1 and out == "" and err.startswith("Error: Config file not found")
    for label, result in results.items():
        assert result == baseline, f"{label} diverged from the first entry point"


@pytest.mark.parametrize("name", _PATCHED_NAMES + _CALLED_NAMES)
def test_cli_member_stays_importable_from_the_package(name):
    """Every name the suite patches or calls on `orion.cli` is an attribute of it.

    Why this matters: identical --help output does not prove the import surface survived a
    split. Tests reach these names through the `orion.cli` module object; a name that
    quietly moved into a submodule without a re-export would fail those tests later and
    less legibly than this one.
    """
    assert hasattr(cli, name), f"orion.cli no longer exposes {name!r}"


def test_importing_the_cli_needs_neither_the_relay_package_nor_argon2(tmp_path):
    """`import orion.cli` succeeds with argon2 unimportable and the relay package off the path.

    Why this matters: the relay lives outside the installed producer and needs the `relay`
    extra (argon2); the CLI reaches it only through the lazy `_load_relay_serve` seam. A
    split that turned that lazy import into a top-level one would break every producer
    install that has no repo checkout — invisibly, until the first `orion report`.
    """
    code = (
        "import sys\n"
        "sys.modules['argon2'] = None\n"  # makes `import argon2` raise ImportError
        "import orion.cli\n"
        "assert 'relay.server' not in sys.modules, 'the relay was imported eagerly'\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        cwd=tmp_path, env=_subprocess_env(),  # cwd is NOT the repo: relay/ is unreachable
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"

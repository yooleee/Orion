# =============================================================================
# tests/test_cli_entry.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the portable `python -m orion` entry point resolves
#                  and dispatches on every OS.
# Role in project: Phase 3.5 (cross-platform pass) makes `python -m orion` the
#                  canonical invocation the docs lead with, because the installed
#                  console-script path differs per platform. This test pins that
#                  the module entry point (src/orion/__main__.py) actually works,
#                  so a docs promise we make on all three OSes is enforced by CI.
# How it's tested: we shell out to the SAME interpreter running the tests
#   (`sys.executable -m orion --help`) rather than import-and-call, because the
#   whole point is to exercise the real `-m orion` machinery (module resolution
#   + __main__ execution), which an in-process call would bypass. `--help` makes
#   argparse print usage and exit 0 without needing config, secrets, or network.
# =============================================================================

import subprocess
import sys


def test_python_dash_m_orion_help_runs():
    """`python -m orion --help` exits 0 and prints usage on any platform.

    Why:
        This is the cross-OS guarantee behind the docs leading with
        `python -m orion`. If __main__.py were missing or wired wrong, the
        module would fail to resolve and this would be a non-zero exit — so the
        test is a real portability regression guard, not a formality.
    """
    # text=True decodes stdout/stderr to str; capture_output keeps the child's
    # output out of the test log unless we assert on it.
    result = subprocess.run(
        [sys.executable, "-m", "orion", "--help"],
        capture_output=True,
        text=True,
    )

    # argparse exits 0 after printing help for a recognized --help flag.
    assert result.returncode == 0
    # "usage:" is argparse's standard help banner; "orion" is our prog name. Both
    # confirm we reached OUR parser, not some unrelated module's help.
    assert "usage:" in result.stdout
    assert "orion" in result.stdout

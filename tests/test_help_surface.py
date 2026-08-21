# =============================================================================
# test_help_surface.py
# -----------------------------------------------------------------------------
# Responsible for: Pinning the CLI's complete --help tree to the committed
#                  snapshot fixture (tests/fixtures/cli_help_snapshot.txt).
# Role in project: The CS-O arc's surface guard — any change to the command
#                  surface fails here until the fixture is deliberately
#                  regenerated, so every surface change appears as an explicit,
#                  reviewable fixture diff in its PR, and PR10's mechanical
#                  cli.py split can prove zero behavior change by byte-diff.
# Assumptions: scripts/help_snapshot.py owns the rendering (and the hand-kept
#              command tree); this test only runs it and compares.
# =============================================================================

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_SCRIPT = REPO_ROOT / "scripts" / "help_snapshot.py"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "cli_help_snapshot.txt"


def test_help_tree_matches_the_committed_snapshot():
    """The full `orion --help` tree is byte-identical to the committed fixture.

    Why this matters: the command surface is under a deliberate, PR-by-PR
    overhaul (docs/command-surface-overhaul-build-kickoff.md). This guard turns
    every surface change — a new verb, a renamed command, reworded help — into a
    failing test until the snapshot is regenerated in the same PR, which is what
    makes each change intentional and each PR's before/after reviewable.

    How it works: the snapshot script is run as a subprocess (it pins COLUMNS
    and clears ORION_CONFIG itself, so the comparison is environment-proof) and
    its stdout is compared to the fixture. Comparison is on universal-newlines
    text so a Windows checkout cannot fail on line endings alone.
    """
    result = subprocess.run(
        [sys.executable, str(SNAPSHOT_SCRIPT)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, f"help_snapshot.py failed:\n{result.stderr}"
    assert result.stdout == FIXTURE.read_text(encoding="utf-8"), (
        "The CLI --help tree no longer matches tests/fixtures/cli_help_snapshot.txt. "
        "If this surface change is intentional, regenerate the fixture in this same "
        "change with: python scripts/help_snapshot.py --write"
    )

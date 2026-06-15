# =============================================================================
# state.py
# -----------------------------------------------------------------------------
# Responsible for: Persisting, per project, what was last reported (a git commit
#                  marker + timestamp) and an append-only history of sent reports.
# Role in project: Makes each report a DELTA. `get_last_reported` tells the git
#                  collector where to start; `advance_state` is called ONLY after
#                  a successful send, so a crash/decline safely re-reports next run.
# Why sqlite (stdlib) over a JSON file: zero dependency either way, but sqlite
#                  gives atomic writes (the marker can't be half-written on a
#                  crash) and a clean home for history. See the plan's decision.
# Safety note: report_history stores only the ALREADY-REDACTED body — raw text
#                  and secrets never reach this store.
# =============================================================================

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

# Schema kept as a module constant so open_state has a single source of truth and
# the tables are easy to read in one place. "IF NOT EXISTS" makes open_state
# idempotent — no migration framework needed for this simple, additive schema.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_state (
    project       TEXT PRIMARY KEY,
    last_commit   TEXT NOT NULL,   -- last reported git HEAD sha (the delta marker)
    last_reported TEXT NOT NULL    -- ISO 8601 UTC timestamp of that report
);

CREATE TABLE IF NOT EXISTS report_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project    TEXT NOT NULL,
    summary    TEXT NOT NULL,      -- the redacted body that was sent
    recipients TEXT NOT NULL,      -- JSON array of recipient names
    sent_at    TEXT NOT NULL       -- ISO 8601 UTC timestamp
);
"""


def open_state(db_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the sqlite state store and ensure the schema.

    Args:
        db_path: Path to the sqlite database file.

    Returns:
        An open sqlite3.Connection with the schema in place.

    Why:
        Bundling "connect + create tables if missing" means the first run on a
        fresh machine just works — no separate `orion init` step, which serves
        the "clone and run in ten minutes" goal. We create the parent directory
        so a state_db pointed at a not-yet-existing folder doesn't fail.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    # executescript runs the multi-statement schema; it commits implicitly.
    conn.executescript(_SCHEMA)
    return conn


def get_last_reported(conn: sqlite3.Connection, project: str) -> str | None:
    """Return the last reported commit SHA for a project, or None if never.

    Args:
        conn: An open state connection.
        project: The project name.

    Returns:
        The stored commit SHA, or None for a first-ever run (no row yet).

    Why:
        None is the signal the git collector uses to mean "no prior marker —
        report the full history this time." Distinguishing "never reported" from
        "reported up to X" is what makes the first run behave correctly.
    """
    row = conn.execute(
        "SELECT last_commit FROM project_state WHERE project = ?",
        (project,),
    ).fetchone()
    return row[0] if row is not None else None


def advance_state(
    conn: sqlite3.Connection,
    project: str,
    commit_sha: str,
    reported_at: str,
) -> None:
    """Record the new last-reported marker for a project (insert or update).

    Args:
        conn: An open state connection.
        project: The project name.
        commit_sha: The git HEAD sha this report covered up to.
        reported_at: ISO 8601 UTC timestamp of this report.

    Returns:
        None. Side effect: upserts the project_state row and commits.

    Why:
        This is an UPSERT (ON CONFLICT) so the first run inserts and every later
        run updates — one code path for both. The caller invokes this ONLY after
        a successful delivery; that ordering is what guarantees we never advance
        past activity that was never actually sent. The commit_sha passed here is
        the same marker the user saw in the preview, so what gets recorded
        matches what was delivered.
    """
    conn.execute(
        """
        INSERT INTO project_state (project, last_commit, last_reported)
        VALUES (?, ?, ?)
        ON CONFLICT(project) DO UPDATE SET
            last_commit = excluded.last_commit,
            last_reported = excluded.last_reported
        """,
        (project, commit_sha, reported_at),
    )
    conn.commit()


def record_report(
    conn: sqlite3.Connection,
    project: str,
    summary: str,
    recipients: list[str],
    sent_at: str,
) -> None:
    """Append one delivered report to the history table.

    Args:
        conn: An open state connection.
        project: The project name.
        summary: The REDACTED body that was sent (never raw text).
        recipients: Names of the recipients it was sent to.
        sent_at: ISO 8601 UTC timestamp.

    Returns:
        None. Side effect: inserts a report_history row and commits.

    Why:
        A lightweight audit trail ("what did I send, to whom, when") that costs
        almost nothing and supports a future "show last report" feature. We store
        recipients as a JSON array because sqlite has no list type and JSON keeps
        the names structured for later parsing — simpler than a join table for
        this scale.
    """
    conn.execute(
        "INSERT INTO report_history (project, summary, recipients, sent_at) "
        "VALUES (?, ?, ?, ?)",
        (project, summary, json.dumps(recipients), sent_at),
    )
    conn.commit()

# =============================================================================
# state.py
# -----------------------------------------------------------------------------
# Responsible for: Persisting, per project AND per collector, what was last
#                  reported (an opaque delta marker + timestamp) and an
#                  append-only history of sent reports.
# Role in project: Makes each report a DELTA. `get_marker` tells a collector
#                  where it left off; `set_marker` is called ONLY after a
#                  successful send, so a crash/decline safely re-reports next run.
# Phase 2 change: markers moved from a single per-project git column to a generic
#                  per-(project, collector) table, because each collector now
#                  carries its own marker (git: a HEAD sha; tasks: the completed
#                  set; notes: a content hash). The marker is OPAQUE to this
#                  store — exactly as git's sha always was. One uniform mechanism
#                  instead of one column per signal (the DRY choice).
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

# The collector key git uses in the markers table. Named so the wrapper helpers
# and any backfill share one spelling (DRY) and it reads clearly at call sites.
GIT_COLLECTOR = "git"

# How long sqlite waits for a held write-lock before raising "database is locked".
# 5s comfortably covers the realistic concurrency here: two reports firing in quick
# succession (e.g. a pre-push hook on rapid hackathon commits) — the later writer
# waits for the earlier one's brief marker/history write instead of failing and
# silently losing that run's report. Passed to sqlite3.connect as `timeout`, which
# under the hood sets the connection's busy_timeout (in ms).
_BUSY_TIMEOUT_SECONDS = 5.0

# Schema kept as a module constant so open_state has a single source of truth and
# the tables are easy to read in one place. "IF NOT EXISTS" makes open_state
# idempotent — no migration framework needed for this simple, additive schema.
#
# project_state is the LEGACY Phase-1 table. It is kept (not dropped) only as the
# source for the one-time backfill in open_state: dropping a column isn't an
# idempotent "IF NOT EXISTS" operation, and a dead column costs nothing. New
# writes go to collector_markers; project_state is never written again.
# (Tracked as vestigial in docs/known-issues.md, to be removed in a future migration.)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_state (
    project       TEXT PRIMARY KEY,
    last_commit   TEXT NOT NULL,   -- legacy: last reported git HEAD sha (pre-Phase-2)
    last_reported TEXT NOT NULL    -- legacy: ISO 8601 UTC timestamp of that report
);

CREATE TABLE IF NOT EXISTS collector_markers (
    project     TEXT NOT NULL,
    collector   TEXT NOT NULL,     -- "git" | "tasks" | "notes"
    marker      TEXT NOT NULL,     -- opaque per-collector delta marker
    reported_at TEXT NOT NULL,     -- ISO 8601 UTC of the report that advanced it
    PRIMARY KEY (project, collector)
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
    # Pass a busy timeout so concurrent writers wait for the lock instead of
    # immediately raising "database is locked" (see _BUSY_TIMEOUT_SECONDS).
    conn = sqlite3.connect(db_path, timeout=_BUSY_TIMEOUT_SECONDS)
    # executescript runs the multi-statement schema; it commits implicitly.
    conn.executescript(_SCHEMA)
    # Migrate any pre-Phase-2 git markers into the new per-collector table. This
    # runs every open but is idempotent and a no-op once migrated (see helper).
    _backfill_git_markers(conn)
    return conn


def _backfill_git_markers(conn: sqlite3.Connection) -> None:
    """Copy legacy project_state git markers into collector_markers, once.

    Args:
        conn: An open state connection with the schema already created.

    Returns:
        None. Side effect: inserts missing git rows into collector_markers.

    Why:
        Phase 1 stored git's marker in project_state.last_commit. Phase 2 reads
        markers from collector_markers. Without this, upgrading would silently
        reset every tracked project's git delta to "never reported" and re-send
        its whole history. INSERT OR IGNORE makes it safe to run on every open:
        it only fills a (project, "git") row that doesn't already exist, so once
        a project has advanced under the new helpers this copies nothing.
    """
    conn.execute(
        """
        INSERT OR IGNORE INTO collector_markers (project, collector, marker, reported_at)
        SELECT project, ?, last_commit, last_reported FROM project_state
        """,
        (GIT_COLLECTOR,),
    )
    conn.commit()


def get_marker(conn: sqlite3.Connection, project: str, collector: str) -> str | None:
    """Return a collector's last-reported marker for a project, or None if never.

    Args:
        conn: An open state connection.
        project: The project name.
        collector: The collector key (e.g. "git", "tasks", "notes").

    Returns:
        The stored opaque marker, or None for a first-ever run (no row yet).

    Why:
        None is the universal "no prior marker — this is the first report for
        this signal" signal every collector uses. Keying by (project, collector)
        lets each signal track its own delta independently: advancing git must
        not touch where tasks or notes left off.
    """
    row = conn.execute(
        "SELECT marker FROM collector_markers WHERE project = ? AND collector = ?",
        (project, collector),
    ).fetchone()
    return row[0] if row is not None else None


def set_marker(
    conn: sqlite3.Connection,
    project: str,
    collector: str,
    marker: str,
    reported_at: str,
) -> None:
    """Record a collector's new last-reported marker (insert or update).

    Args:
        conn: An open state connection.
        project: The project name.
        collector: The collector key this marker belongs to.
        marker: The opaque marker this report covered up to (a sha, a serialized
            set, a content hash — this store does not interpret it).
        reported_at: ISO 8601 UTC timestamp of this report.

    Returns:
        None. Side effect: upserts the collector_markers row and commits.

    Why:
        An UPSERT on (project, collector) means the first run inserts and every
        later run updates the same row — one code path for both, one row per
        signal. The caller invokes this ONLY after a successful delivery; that
        ordering is what guarantees we never advance past activity that was never
        actually sent. The marker passed here is exactly what produced the
        previewed report, so what gets recorded matches what was delivered.
    """
    conn.execute(
        """
        INSERT INTO collector_markers (project, collector, marker, reported_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(project, collector) DO UPDATE SET
            marker = excluded.marker,
            reported_at = excluded.reported_at
        """,
        (project, collector, marker, reported_at),
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

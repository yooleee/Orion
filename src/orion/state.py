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
# (KI-8: the legacy Phase-1 `project_state` table was dropped 2026-06-25. It existed
# only as the source for a one-time git-marker backfill; that window closed long ago —
# every live DB backfilled when Phase 2 shipped 2026-06-15. New DBs never had it.)
_SCHEMA = """
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

-- C2 pull-back: the per-developer "unread" cursor for supervisor comments. When you
-- pull a project's comments back from a relay, we record the highest comment id seen
-- so the next pull shows only what's newer. This is a LOCAL notion: "unread" is per
-- reader, so the relay stays a dumb append-only store and never holds a read-cursor.
-- Keyed by (project, relay_url) — NOT project alone — so pointing a project at a
-- different relay starts a fresh cursor instead of silently reusing a stale one that
-- counts ids from the old relay's store. A separate table (not collector_markers) on
-- purpose: this is a read-cursor, semantically distinct from a report's delta marker.
CREATE TABLE IF NOT EXISTS comment_watermark (
    project              TEXT NOT NULL,
    relay_url            TEXT NOT NULL,
    last_seen_comment_id INTEGER NOT NULL,  -- highest relay comment id pulled so far
    updated_at           TEXT NOT NULL,     -- ISO 8601 UTC of the pull that set it
    PRIMARY KEY (project, relay_url)
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
    return conn


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


def get_comment_watermark(
    conn: sqlite3.Connection, project: str, relay_url: str
) -> int:
    """Return the highest comment id already pulled for a (project, relay), or 0.

    Args:
        conn: An open state connection.
        project: The project the comments belong to.
        relay_url: The relay the comments were pulled from (the configured [relay]
            `url`). Part of the key so a different relay gets its own cursor.

    Returns:
        The stored last-seen comment id, or 0 when there is no row yet (a first-ever
        pull). 0 is the natural "seen nothing" sentinel: relay comment ids start at 1,
        so `id > 0` returns everything — exactly what a first pull should show.

    Why:
        This is the read side of the unread cursor. Returning 0 (not None) for "never
        pulled" lets the caller pass it straight to pull_comments as `since_id` with no
        null handling — the first pull naturally fetches the full history, and every
        later pull fetches only what is newer than what it last recorded.
    """
    row = conn.execute(
        "SELECT last_seen_comment_id FROM comment_watermark "
        "WHERE project = ? AND relay_url = ?",
        (project, relay_url),
    ).fetchone()
    return row[0] if row is not None else 0


def set_comment_watermark(
    conn: sqlite3.Connection,
    project: str,
    relay_url: str,
    last_seen_comment_id: int,
    updated_at: str,
) -> None:
    """Record the highest comment id pulled for a (project, relay) (insert or update).

    Args:
        conn: An open state connection.
        project: The project the comments belong to.
        relay_url: The relay they were pulled from (the configured [relay] `url`).
        last_seen_comment_id: The new high-water mark — the `latest_id` the relay
            returned for this pull.
        updated_at: ISO 8601 UTC timestamp of this pull.

    Returns:
        None. Side effect: upserts the comment_watermark row and commits.

    Why:
        Mirrors set_marker's UPSERT on a composite key: the first pull inserts and
        every later pull updates the same row — one code path for both, one row per
        (project, relay). The caller advances this AFTER a successful pull (and only
        for a normal run, not an `--all` re-read), so the cursor always reflects what
        was actually shown.
    """
    conn.execute(
        """
        INSERT INTO comment_watermark (project, relay_url, last_seen_comment_id, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(project, relay_url) DO UPDATE SET
            last_seen_comment_id = excluded.last_seen_comment_id,
            updated_at = excluded.updated_at
        """,
        (project, relay_url, last_seen_comment_id, updated_at),
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


def get_last_report_time(conn: sqlite3.Connection, project: str) -> str | None:
    """Return the timestamp of a project's most recent delivered report, or None.

    Args:
        conn: An open state connection.
        project: The project name.

    Returns:
        The ISO 8601 UTC timestamp of the latest report_history row for the
        project, or None if the project has never had a report delivered.

    Why:
        The `orion status` digest needs "when did I last report this project" to
        show staleness. It is derivable from the existing append-only
        report_history (MAX(sent_at)), so this needs no new schema — a read-only
        query keeps the state store the single source of that fact. MAX over an
        empty set yields one row holding NULL, so we map that to None.
    """
    row = conn.execute(
        "SELECT MAX(sent_at) FROM report_history WHERE project = ?",
        (project,),
    ).fetchone()
    return row[0] if row is not None else None

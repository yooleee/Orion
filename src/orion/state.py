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

-- ORPHANED (KI-28 Stage 2): the C2 comment pull-back retired, so nothing reads or writes
-- this table anymore — the discussion_watermark below is the live cursor. The CREATE stays
-- for the no-local-drops idiom: a user's existing state DB keeps its (now-inert) table
-- rather than us shipping a destructive migration. It can be dropped in a later cleanup.
-- Historical purpose: the per-developer "unread" cursor over a project's pulled comments,
-- keyed by (project, relay_url) so a re-pointed relay started a fresh cursor.
CREATE TABLE IF NOT EXISTS comment_watermark (
    project              TEXT NOT NULL,
    relay_url            TEXT NOT NULL,
    last_seen_comment_id INTEGER NOT NULL,  -- highest relay comment id pulled so far
    updated_at           TEXT NOT NULL,     -- ISO 8601 UTC of the pull that set it
    PRIMARY KEY (project, relay_url)
);

-- E2 Inc 5: the developer's "unread" cursor over a project's discussion thread — the exact
-- analogue of comment_watermark for the supervisor-interaction loop. The discussion-item id
-- space is its own (relay_discussion_items, distinct from report_comments), so this is a
-- SEPARATE table: reusing comment_watermark would conflate two unrelated id streams and a
-- pull on one would corrupt the other's cursor. Keyed by (project, relay_url) for the same
-- reason as comments — pointing a project at a different relay starts a fresh cursor.
CREATE TABLE IF NOT EXISTS discussion_watermark (
    project           TEXT NOT NULL,
    relay_url         TEXT NOT NULL,
    last_seen_item_id INTEGER NOT NULL,  -- highest relay discussion-item id pulled so far
    updated_at        TEXT NOT NULL,     -- ISO 8601 UTC of the pull that set it
    PRIMARY KEY (project, relay_url)
);

-- E2 Inc 4 (4b): a per-(project, collector, key) cache so an EXPENSIVE collector step
-- runs only when its input actually changed. The disciplines collector keys on the doc
-- path, stores the doc's content hash + the extracted disciplines JSON, and reuses the
-- value when the hash is unchanged (so the LLM extraction runs only on a real edit).
-- Distinct from collector_markers (a delta marker advanced after a SEND): this is an
-- optimization cache, advanced as soon as the work is done, with no send semantics. One
-- row per (project, collector, key) — overwritten on change — so it stays bounded.
CREATE TABLE IF NOT EXISTS collector_cache (
    project      TEXT NOT NULL,
    collector    TEXT NOT NULL,   -- the collector that owns the entry (e.g. "disciplines")
    cache_key    TEXT NOT NULL,   -- the input identity (e.g. a doc's absolute path)
    content_hash TEXT NOT NULL,   -- hash of the (redacted) input the value was derived from
    value        TEXT NOT NULL,   -- the cached result (e.g. extracted disciplines JSON)
    cached_at    TEXT NOT NULL,   -- ISO 8601 UTC of when the value was computed
    PRIMARY KEY (project, collector, cache_key)
);

-- E1.3: an append-only log of dedicated checklist pushes (the `checklist-push` command),
-- the exact analogue of report_history for the checklist lane. It exists so a SCHEDULED
-- `checklist-push --all --due` can answer two questions the stateless push path could not:
-- "when did we last push this project" (cadence gate) and "with what content" (change gate).
-- We store only a content_hash of the pushed wire payload, never the body: the pushed
-- checklist already lives on the relay, and the hash is all the change-gate compares. A row
-- is inserted ONLY after a successful push (mirroring report_history), so a failed push
-- leaves no trace and the next run retries.
CREATE TABLE IF NOT EXISTS checklist_push_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project      TEXT NOT NULL,
    pushed_at    TEXT NOT NULL,     -- ISO 8601 UTC of the successful push
    content_hash TEXT NOT NULL      -- sha256 of the exact wire payload that was pushed
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


# NOTE: get_comment_watermark / set_comment_watermark were removed in KI-28 Stage 2 (the
# `orion comments` pull-back retired). The comment_watermark TABLE is deliberately kept
# (no-local-drops idiom, see its schema note); the discussion_watermark accessors below are
# the live analogue.


def get_discussion_watermark(
    conn: sqlite3.Connection, project: str, relay_url: str
) -> int:
    """Return the highest discussion-item id already pulled for a (project, relay), or 0.

    Args:
        conn: An open state connection.
        project: The project whose thread the cursor tracks.
        relay_url: The relay the items were pulled from (the configured [relay] `url`).
            Part of the key so a different relay gets its own cursor.

    Returns:
        The stored last-seen item id, or 0 when there is no row yet (a first-ever pull).
        0 is the natural "seen nothing" sentinel: relay item ids start at 1, so `id > 0`
        returns everything — exactly what a first pull should show.

    Why:
        The read side of the developer's unread cursor over the discussion thread.
        Returning 0 (not None) lets the caller pass it straight to pull_discussions as
        since_id with no null handling — the first pull fetches the full history.
    """
    row = conn.execute(
        "SELECT last_seen_item_id FROM discussion_watermark "
        "WHERE project = ? AND relay_url = ?",
        (project, relay_url),
    ).fetchone()
    return row[0] if row is not None else 0


def set_discussion_watermark(
    conn: sqlite3.Connection,
    project: str,
    relay_url: str,
    last_seen_item_id: int,
    updated_at: str,
) -> None:
    """Record the highest discussion-item id pulled for a (project, relay) (insert/update).

    Args:
        conn: An open state connection.
        project: The project whose thread the cursor tracks.
        relay_url: The relay they were pulled from (the configured [relay] `url`).
        last_seen_item_id: The new high-water mark — the `latest_id` the relay returned.
        updated_at: ISO 8601 UTC timestamp of this pull.

    Returns:
        None. Side effect: upserts the discussion_watermark row and commits.

    Why:
        An UPSERT on a composite key — first pull inserts, later pulls update the same row.
        The caller advances this AFTER a successful pull (and only for a normal run, not an
        `--all` re-read), so the cursor always reflects what was actually shown.
    """
    conn.execute(
        """
        INSERT INTO discussion_watermark (project, relay_url, last_seen_item_id, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(project, relay_url) DO UPDATE SET
            last_seen_item_id = excluded.last_seen_item_id,
            updated_at = excluded.updated_at
        """,
        (project, relay_url, last_seen_item_id, updated_at),
    )
    conn.commit()


def get_cache(
    conn: sqlite3.Connection, project: str, collector: str, cache_key: str
) -> tuple[str, str] | None:
    """Return a cache entry's (content_hash, value), or None if there is no row.

    Args:
        conn: An open state connection.
        project: The project the entry belongs to.
        collector: The collector that owns the entry (e.g. "disciplines").
        cache_key: The input identity (e.g. a doc's absolute path).

    Returns:
        A (content_hash, value) pair for a cached input, or None for a first-ever
        (or evicted) key. The caller compares the stored content_hash to the
        current input's hash: equal means the value is still valid and the expensive
        step can be skipped.

    Why:
        Returning the hash alongside the value lets the caller decide validity in one
        read — no second lookup. None is the universal "nothing cached for this
        input" signal, identical in spirit to get_marker's None.
    """
    row = conn.execute(
        "SELECT content_hash, value FROM collector_cache "
        "WHERE project = ? AND collector = ? AND cache_key = ?",
        (project, collector, cache_key),
    ).fetchone()
    return (row[0], row[1]) if row is not None else None


def set_cache(
    conn: sqlite3.Connection,
    project: str,
    collector: str,
    cache_key: str,
    content_hash: str,
    value: str,
    cached_at: str,
) -> None:
    """Store (or replace) a cache entry for one (project, collector, key).

    Args:
        conn: An open state connection.
        project: The project the entry belongs to.
        collector: The collector that owns the entry (e.g. "disciplines").
        cache_key: The input identity (e.g. a doc's absolute path).
        content_hash: Hash of the (redacted) input the value was derived from.
        value: The cached result to store (e.g. extracted disciplines JSON).
        cached_at: ISO 8601 UTC timestamp of when the value was computed.

    Returns:
        None. Side effect: upserts the collector_cache row and commits.

    Why:
        An UPSERT on (project, collector, cache_key) means a changed input overwrites
        the same row — one row per key, so the cache stays bounded — mirroring
        set_marker's one-row-per-signal shape. The caller writes ONLY after the
        expensive step succeeds, so a failure leaves the prior entry (or no entry) in
        place and the next run retries.
    """
    conn.execute(
        """
        INSERT INTO collector_cache
            (project, collector, cache_key, content_hash, value, cached_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(project, collector, cache_key) DO UPDATE SET
            content_hash = excluded.content_hash,
            value = excluded.value,
            cached_at = excluded.cached_at
        """,
        (project, collector, cache_key, content_hash, value, cached_at),
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


def record_checklist_push(
    conn: sqlite3.Connection,
    project: str,
    content_hash: str,
    pushed_at: str,
) -> None:
    """Append one successful checklist push to the history table.

    Args:
        conn: An open state connection.
        project: The project whose checklist was pushed.
        content_hash: sha256 of the exact wire payload that was pushed (items +
            kind + due_soon_days) — an opaque marker to this store, never the body.
        pushed_at: ISO 8601 UTC timestamp of the push. Supplied by the caller (as
            record_report takes sent_at) so this store stays time-free and
            deterministic to test.

    Returns:
        None. Side effect: inserts a checklist_push_history row and commits.

    Why:
        The write side of the scheduled-push change gate (E1.3). The caller invokes
        this ONLY after push_checklist returns successfully — mirroring where
        record_report sits — so the log reflects pushes that actually landed on the
        relay, and a failed push records nothing and is retried next run. We store
        the hash rather than the body because the pushed checklist already lives on
        the relay; the hash is all `--due`'s change gate needs to compare.
    """
    conn.execute(
        "INSERT INTO checklist_push_history (project, pushed_at, content_hash) "
        "VALUES (?, ?, ?)",
        (project, pushed_at, content_hash),
    )
    conn.commit()


def get_last_checklist_push(
    conn: sqlite3.Connection, project: str
) -> tuple[str, str] | None:
    """Return a project's most recent checklist push as (pushed_at, content_hash), or None.

    Args:
        conn: An open state connection.
        project: The project name.

    Returns:
        A (pushed_at, content_hash) pair for the latest push, or None if the project
        has never had a checklist push recorded.

    Why:
        `checklist-push --all --due` needs BOTH facts about the last push: `pushed_at`
        gates the cadence check ("is it time to push again?") and `content_hash` gates
        the change check ("would this push actually differ?"). Returning both in one
        read (unlike get_last_report_time's single value) avoids a second lookup. We
        take the MAX row by `id DESC` rather than MAX(pushed_at): id is a monotonic
        insertion order and each run inserts exactly once after its push, so `id DESC`
        is the true latest row and is robust to two pushes sharing a timestamp — a
        correlated read of the pair, which MAX(pushed_at) alone could not give. None
        is the universal "never pushed — nothing to compare" signal (first-run due).
    """
    row = conn.execute(
        "SELECT pushed_at, content_hash FROM checklist_push_history "
        "WHERE project = ? ORDER BY id DESC LIMIT 1",
        (project,),
    ).fetchone()
    return (row[0], row[1]) if row is not None else None

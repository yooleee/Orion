# =============================================================================
# relay/store.py
# -----------------------------------------------------------------------------
# Responsible for: The relay's OWN SQLite store of ingested report blobs — open,
#                  ingest, and the read queries the dashboard renders from.
# Role in project: The hosted half's persistence. It MIRRORS the shape of local
#                  Orion's report_history (project, body, recipients, timestamp)
#                  but widens it to everything the dashboard renders (sections,
#                  share_level, lane, version). It deliberately does NOT import or
#                  share orion.state: the relay is a swappable, separately-deployable
#                  component, so it owns its schema rather than coupling to the
#                  local store's. The two agree only on the portable blob contract.
# Safety note: a blob is ALREADY twice-redacted before local Orion pushes it, so
#              this store, like report_history, only ever holds redacted text. It is
#              still sensitive (the user's redacted activity), so the dashboard that
#              reads it is access-gated — but no raw secret reaches here.
# Why sqlite (stdlib): same reasoning as the local store — zero dependency, atomic
#              writes, a natural home for a small history. Keeps the relay's deps as
#              light as the core's.
# =============================================================================

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

# How long sqlite waits for a held write-lock before raising "database is locked".
# The relay's server is threaded (CP6), so two pushes can arrive close together;
# a busy timeout lets the later writer wait out the earlier brief insert instead of
# failing. This mirrors the local store's _BUSY_TIMEOUT_SECONDS, but is duplicated
# DELIBERATELY rather than imported: relay/ shares no code with orion/ (see the
# module header), and 5s is small enough that one constant per package is clearer
# than a cross-package dependency built just to share a number.
_BUSY_TIMEOUT_SECONDS = 5.0

# Schema as a module constant so open_relay_store has one source of truth.
# "IF NOT EXISTS" makes open idempotent — no migration framework for this simple,
# additive schema, matching the local store's approach. relay_reports widens
# report_history's (project, summary, recipients, sent_at) with the extra blob
# fields the dashboard renders (sections, share_level, lane, orion_version) plus an
# ingested_at stamped on arrival (distinct from the blob's own generated_at).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS relay_reports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project       TEXT NOT NULL,
    body          TEXT NOT NULL,      -- the redacted report body (canonical text)
    sections      TEXT NOT NULL,      -- JSON array of [title, body] pairs
    participants  TEXT NOT NULL,      -- JSON array of recipient names
    share_level   TEXT NOT NULL,      -- "high_level" | "detailed"
    lane          TEXT NOT NULL,      -- "raw" | "structured" (provenance)
    generated_at  TEXT NOT NULL,      -- ISO 8601 UTC, when local Orion built it
    orion_version TEXT NOT NULL,      -- the producing Orion version
    ingested_at   TEXT NOT NULL       -- ISO 8601 UTC, when the relay received it
);

-- History lookups filter by project; a tiny index keeps that query fast and
-- signals intent, even though the data volume here is small.
CREATE INDEX IF NOT EXISTS idx_relay_reports_project ON relay_reports(project);

-- C2: supervisor comments on a report. Append-only and flat (no threading, edit, or
-- delete) — the v1 model. report_id points at relay_reports.id but is deliberately
-- NOT a foreign key: sqlite enforces FKs only when explicitly enabled per-connection,
-- and the server already confirms the report exists (get()) before inserting, so the
-- check lives there rather than in a constraint we'd have to opt into. author is the
-- self-entered display name (free text, "" when omitted — NOT authenticated identity,
-- which is C3); body is plain text, escaped on render. created_at is when the relay
-- received the comment (its clock), matching ingested_at's provenance meaning.
CREATE TABLE IF NOT EXISTS report_comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id   INTEGER NOT NULL,   -- the relay_reports.id this hangs off
    author      TEXT NOT NULL,      -- self-entered display name, or "" when omitted
    body        TEXT NOT NULL,      -- plain text; escaped on render
    created_at  TEXT NOT NULL       -- ISO 8601 UTC, when the relay received it
);

-- Comments are always fetched for one report; the index keeps that filter fast.
CREATE INDEX IF NOT EXISTS idx_report_comments_report ON report_comments(report_id);
"""


def open_relay_store(db_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the relay's SQLite store and ensure the schema.

    Args:
        db_path: Path to the relay's sqlite database file (e.g. orion-relay.sqlite3).

    Returns:
        An open sqlite3.Connection with the schema in place and a Row row_factory.

    Why:
        Like the local store's open_state, bundling "connect + create tables if
        missing" means the relay just works on first run with no separate init
        step. We set row_factory = sqlite3.Row so the read helpers can decode rows
        by COLUMN NAME — far more readable than positional indexing given this
        table's width, and it keeps _row_to_report explicit about which field is
        which. The parent directory is created so a db path in a not-yet-existing
        folder doesn't fail.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Busy timeout so concurrent pushes wait for the lock instead of erroring.
    conn = sqlite3.connect(db_path, timeout=_BUSY_TIMEOUT_SECONDS)
    # Named-column access for the decoder below (explicit over positional).
    conn.row_factory = sqlite3.Row
    # executescript runs the multi-statement schema and commits implicitly.
    conn.executescript(_SCHEMA)
    return conn


def ingest(conn: sqlite3.Connection, blob: dict, ingested_at: str) -> int:
    """Store one ingested report blob and return its new row id.

    Args:
        conn: An open relay-store connection.
        blob: A VALIDATED portable blob dict — the parsed JSON that local Orion
            POSTed (serialize_blob's output). The server validates its shape and
            version (CP6) BEFORE calling this, so the required keys are assumed
            present here; this function does not re-validate.
        ingested_at: ISO 8601 UTC timestamp of when the relay received the push.
            Passed in (not generated here) so the server controls the clock and the
            function stays deterministic and easy to test.

    Returns:
        The autoincrement id of the inserted row — the handle the dashboard uses in
        its /report/<id> route.

    Why:
        The relay's job on ingest is simply to persist the blob as-is for later
        rendering. sections and participants are JSON-encoded for storage because
        sqlite has no list/tuple type — the same choice report_history makes for
        recipients — and they are decoded back to structures on read. Returning the
        new id lets the server report 201 Created with a concrete reference.
    """
    cursor = conn.execute(
        """
        INSERT INTO relay_reports (
            project, body, sections, participants,
            share_level, lane, generated_at, orion_version, ingested_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            blob["project"],
            blob["body"],
            # Re-encode the already-parsed lists for the TEXT columns.
            json.dumps(blob["sections"]),
            json.dumps(blob["participants"]),
            blob["share_level"],
            blob["lane"],
            blob["generated_at"],
            blob["orion_version"],
            ingested_at,
        ),
    )
    conn.commit()
    return cursor.lastrowid


def list_projects(conn: sqlite3.Connection) -> list[dict]:
    """Summarize every project that has ingested reports, most recent first.

    Args:
        conn: An open relay-store connection.

    Returns:
        A list of {"project", "report_count", "latest_generated_at"} dicts, ordered
        with the most recently active project first.

    Why:
        This backs the dashboard's index ("which projects, and when did each last
        update?"). Aggregating in SQL (COUNT + MAX grouped by project) is simpler
        and cheaper than pulling every row and counting in Python. Ordering by the
        latest report puts the freshest activity at the top — what a supervisor
        glancing at the dashboard wants to see first. generated_at is an ISO-8601
        UTC string, so a lexical MAX is also the chronological latest.
    """
    rows = conn.execute(
        """
        SELECT project,
               COUNT(*)          AS report_count,
               MAX(generated_at) AS latest_generated_at
        FROM relay_reports
        GROUP BY project
        ORDER BY latest_generated_at DESC, project ASC
        """
    ).fetchall()
    return [
        {
            "project": row["project"],
            "report_count": row["report_count"],
            "latest_generated_at": row["latest_generated_at"],
        }
        for row in rows
    ]


def history(conn: sqlite3.Connection, project: str) -> list[dict]:
    """Return all stored reports for one project, newest first.

    Args:
        conn: An open relay-store connection.
        project: The project name to fetch history for.

    Returns:
        A list of full report dicts (see _row_to_report) ordered newest first.
        Empty when the project has no reports (or does not exist).

    Why:
        Backs the per-project history view. We order by generated_at DESC with id
        DESC as a tiebreaker, so two reports built in the same second still have a
        stable, deterministic order (the later-ingested one first). Returning full
        dicts — not a trimmed projection — keeps one decoder shape across the store
        (DRY) and is negligible at this scale; the render layer chooses what to
        show.
    """
    rows = conn.execute(
        """
        SELECT * FROM relay_reports
        WHERE project = ?
        ORDER BY generated_at DESC, id DESC
        """,
        (project,),
    ).fetchall()
    return [_row_to_report(row) for row in rows]


def get(conn: sqlite3.Connection, report_id: int) -> dict | None:
    """Fetch a single stored report by id, or None if it does not exist.

    Args:
        conn: An open relay-store connection.
        report_id: The report's autoincrement id (from a dashboard link).

    Returns:
        The full report dict (see _row_to_report), or None when no row has that id.

    Why:
        Backs the single-report view. Returning None for a missing id (rather than
        raising) lets the server translate it cleanly into a 404 — a bad/stale link
        is an expected case, not an error.
    """
    row = conn.execute(
        "SELECT * FROM relay_reports WHERE id = ?", (report_id,)
    ).fetchone()
    return _row_to_report(row) if row is not None else None


def _row_to_report(row: sqlite3.Row) -> dict:
    """Decode one relay_reports row into a report dict with structured fields.

    Args:
        row: A sqlite3.Row from relay_reports (named-column access).

    Returns:
        A dict of the report's fields, with sections and participants decoded from
        their stored JSON back into lists.

    Why:
        history() and get() both return the same report shape, so the row→dict
        decode lives in ONE place (DRY). Decoding the JSON columns here means
        callers (and the renderer) get real lists — sections as [title, body]
        pairs, participants as names — instead of raw JSON strings they would have
        to parse themselves.
    """
    return {
        "id": row["id"],
        "project": row["project"],
        "body": row["body"],
        "sections": json.loads(row["sections"]),
        "participants": json.loads(row["participants"]),
        "share_level": row["share_level"],
        "lane": row["lane"],
        "generated_at": row["generated_at"],
        "orion_version": row["orion_version"],
        "ingested_at": row["ingested_at"],
    }


def add_comment(
    conn: sqlite3.Connection,
    report_id: int,
    author: str,
    body: str,
    created_at: str,
) -> int:
    """Append one supervisor comment to a report and return its new row id.

    Args:
        conn: An open relay-store connection.
        report_id: The relay_reports.id this comment hangs off. The server confirms
            the report exists (get()) BEFORE calling this, so this function does not
            re-check — it just inserts.
        author: The self-entered display name, or "" when omitted. Free text, NOT an
            authenticated identity (that is C3); the server has already length-capped it.
        body: The plain-text comment. Already validated non-empty and length-capped by
            the server; stored as-is and escaped only on render.
        created_at: ISO 8601 UTC timestamp of when the relay received the comment.
            Passed in (not generated here) so the server controls the clock and this
            function stays deterministic and easy to test — same pattern as ingest().

    Returns:
        The autoincrement id of the inserted comment row.

    Why:
        Mirrors ingest(): a single INSERT + commit, returning the new id. Comments are
        append-only and flat (no update/delete path) per the v1 model, so this is the
        only write the table ever takes. The author/body validation and the
        report-exists check both live in the server (the inbound boundary), keeping
        this store function a thin, trusted persistence call.
    """
    cursor = conn.execute(
        """
        INSERT INTO report_comments (report_id, author, body, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (report_id, author, body, created_at),
    )
    conn.commit()
    return cursor.lastrowid


def comments_for(conn: sqlite3.Connection, report_id: int) -> list[dict]:
    """Return all comments on one report, oldest first.

    Args:
        conn: An open relay-store connection.
        report_id: The report whose comments to fetch.

    Returns:
        A list of {"id", "report_id", "author", "body", "created_at"} dicts in
        chronological (oldest-first) order. Empty when the report has no comments.

    Why:
        Backs the comments section on the report detail page. We order by id ASC —
        which, because id is a monotonic autoincrement, is insertion (chronological)
        order — so an append-only thread reads top-to-bottom in the order it was
        written. Unlike relay_reports there are no JSON columns to decode, so the rows
        map straight to plain dicts. Returning [] (not None) for a report with no
        comments lets the renderer show a clean empty state without a null check.
    """
    rows = conn.execute(
        """
        SELECT id, report_id, author, body, created_at
        FROM report_comments
        WHERE report_id = ?
        ORDER BY id ASC
        """,
        (report_id,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "report_id": row["report_id"],
            "author": row["author"],
            "body": row["body"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]

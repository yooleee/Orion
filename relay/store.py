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
from datetime import date
from pathlib import Path

from .derive import count_at_risk, milestones, slipping_item_keys

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

-- E2 Inc 2: a project's LIVE checklist — its CURRENT open/done items, mirrored from
-- the user's tasks_file. Unlike relay_reports (append-only history), this is current
-- state: ONE row per project, REPLACED on each push (project is the PRIMARY KEY, and
-- ingest upserts). This is a brand-new table, so "IF NOT EXISTS" creates it on the
-- already-deployed relay with NO column migration — the reason a project-level table
-- is cleaner here than widening relay_reports. items is a JSON array of {text, done}
-- objects in file order (the same shape the blob carries); updated_at is the relay's
-- receive clock (matching ingested_at's provenance meaning).
CREATE TABLE IF NOT EXISTS relay_project_checklists (
    project     TEXT PRIMARY KEY,   -- one current checklist per project
    items       TEXT NOT NULL,      -- JSON array of {text, done} objects, in file order
    updated_at  TEXT NOT NULL       -- ISO 8601 UTC, when the relay last received it
);

-- E2 Inc 3: an APPEND-ONLY log of each checklist item's observed forward state over time
-- — the "remember" half of the forward-looking layer. Unlike relay_project_checklists
-- (current state, one upserted row per project), this ACCUMULATES: one row per item per
-- push, so the dashboard can later derive slippage (a due_date that moved later) and other
-- history. It is a DOWNSTREAM PROJECTION — rebuildable from the pushes, authoring nothing.
-- item_key is the item's STABLE identity (the producer's `key` — the tracker's bare title —
-- else the item text): it must survive a status change, which the status-embedding `text`
-- does not (see the forward-store identity KI). done is 0/1 (sqlite has no bool). A new
-- table, so "IF NOT EXISTS" adds it on the already-deployed relay with no column migration.
CREATE TABLE IF NOT EXISTS relay_observed_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT NOT NULL,
    item_key    TEXT NOT NULL,      -- stable identity (producer key, else item text)
    due_date    TEXT,              -- ISO YYYY-MM-DD, or NULL when the item has no deadline
    done        INTEGER NOT NULL,   -- 0 | 1 (sqlite has no boolean)
    observed_at TEXT NOT NULL       -- ISO 8601 UTC, the relay's receive clock
);

-- The projection/slippage queries fetch one project's items in time order (often for a
-- single item_key); this composite index serves both shapes.
CREATE INDEX IF NOT EXISTS idx_relay_observed_items_project_key
    ON relay_observed_items(project, item_key, observed_at);

-- E2 Inc 4: per-project metadata that is not a report or a checklist. Today it carries one
-- field, `kind`, which splits the dashboard home into "Projects" (real software projects)
-- and "To-dos / Trackers" (general checklists like the applications tracker). The fact is
-- OBSERVED from the user's own orion.toml (an explicit `kind` flag), not inferred — it
-- arrives on each push and is upserted here. A SEPARATE new table (not a column on
-- relay_project_checklists) keeps the relay's no-column-migration property: every prior
-- addition was a fresh "IF NOT EXISTS" table, so an already-deployed relay gains this with
-- no ALTER. kind is a property of the PROJECT (a report-only project has one too — it just
-- defaults), so a project-keyed table is its natural home; default 'project' means an
-- absent row reads as a plain project.
CREATE TABLE IF NOT EXISTS relay_project_meta (
    project TEXT PRIMARY KEY,
    kind    TEXT NOT NULL DEFAULT 'project'   -- "project" | "tracker"
);

-- E2 Inc 4 (4b): a project's OBSERVED DISCIPLINES — the working principles Orion read
-- from the user's own docs (observe-not-originate; the user authored them, Orion only
-- reflects them). Like relay_project_checklists this is CURRENT STATE: ONE row per
-- project, REPLACED on each push (project is the PRIMARY KEY, ingest upserts), NOT
-- append-only history. disciplines is a JSON array of {title, why, scope, source}
-- objects, where scope is "global" | "project" and source is the repo-relative doc the
-- principle was observed in (the dashboard's "observed · <source>" footer). A brand-new
-- "IF NOT EXISTS" table, so the already-deployed relay gains it with NO column migration.
CREATE TABLE IF NOT EXISTS relay_project_disciplines (
    project     TEXT PRIMARY KEY,   -- one current discipline set per project
    disciplines TEXT NOT NULL,      -- JSON array of {title, why, scope, source} objects
    updated_at  TEXT NOT NULL       -- ISO 8601 UTC, when the relay last received it
);

-- E2 Inc 4 (4c): a project's OBSERVED SKILLS — the competencies Orion DERIVED from the
-- project's own activity (languages from tracked files, commit subjects, doc focus),
-- the honest alternative to an authored resume. Like relay_project_disciplines this is
-- CURRENT STATE: ONE row per project, REPLACED on each push (project is the PRIMARY KEY,
-- ingest upserts), NOT history. skills is a JSON array of {name, category, evidence,
-- weight, signals} objects. Unlike disciplines (a per-project flat list), the relay
-- MERGES these across projects into the cross-project "skills comb" — but each project
-- still pushes only its OWN skills, so the per-project row shape is the natural store.
-- A brand-new "IF NOT EXISTS" table, so the already-deployed relay gains it with NO
-- column migration.
CREATE TABLE IF NOT EXISTS relay_project_skills (
    project    TEXT PRIMARY KEY,    -- one current skill set per project
    skills     TEXT NOT NULL,       -- JSON array of {name, category, evidence, weight, signals}
    updated_at TEXT NOT NULL        -- ISO 8601 UTC, when the relay last received it
);

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

-- C3 / multi-party access (Increment 1). relay_users is the per-user identity +
-- credential store. The login credential is a server-minted high-entropy random key;
-- only its VERIFIER = HMAC-SHA256(pepper, key) is stored (the raw key is shown once at
-- creation, never persisted or logged). `active` + `session_version` give STATELESS
-- revocation: set active = 0 and/or bump session_version to invalidate a user's live
-- sessions WITHOUT a server-side session table — the signed cookie carries the
-- session_version it was minted with and is rejected once it no longer matches. `name`
-- is UNIQUE so CLI ops (e.g. revoke <name>) are unambiguous; `key_verifier` is UNIQUE so
-- a login lookup resolves to exactly one row. The role column is an open enum
-- (admin/viewer now; contributor/guest later) so new roles are additive.
CREATE TABLE IF NOT EXISTS relay_users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,        -- display name + CLI handle
    key_verifier    TEXT NOT NULL UNIQUE,        -- HMAC-SHA256(pepper, raw_key), hex
    role            TEXT NOT NULL,               -- "admin" | "viewer"
    active          INTEGER NOT NULL DEFAULT 1,  -- 0 = revoked (login + sessions denied)
    session_version INTEGER NOT NULL DEFAULT 1,  -- bump to force-logout this user
    created_by      TEXT NOT NULL,               -- who provisioned (actor label)
    created_at      TEXT NOT NULL,               -- ISO 8601 UTC
    last_login_at   TEXT                         -- ISO 8601 UTC, NULL until first login
);

-- A viewer's per-project READ scope. Default-deny: a viewer with no rows here sees
-- nothing. An admin ignores this table entirely (it sees all projects). The composite
-- primary key dedupes a repeated (user, project) grant.
CREATE TABLE IF NOT EXISTS relay_user_projects (
    user_id   INTEGER NOT NULL,
    project   TEXT NOT NULL,
    PRIMARY KEY (user_id, project)
);

-- The scope lookup filters by user_id on every authorized request; index it.
CREATE INDEX IF NOT EXISTS idx_relay_user_projects_user ON relay_user_projects(user_id);

-- Append-only audit of admin/provisioning actions (who created/revoked whom, with what
-- role + projects). A multi-party access model needs an accountability trail; this is it.
CREATE TABLE IF NOT EXISTS relay_admin_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor       TEXT NOT NULL,   -- who performed it (e.g. "admin-token")
    action      TEXT NOT NULL,   -- "create_user" | "revoke_user" | …
    target_user TEXT NOT NULL,   -- the affected user's name
    role        TEXT NOT NULL,   -- role involved, or ""
    projects    TEXT NOT NULL,   -- JSON array of projects, or "[]"
    created_at  TEXT NOT NULL    -- ISO 8601 UTC
);
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
    # WAL mode lets dashboard READS proceed concurrently with a writer (a login
    # touching last_login_at, an ingest, a comment), instead of readers and the writer
    # blocking each other. It is a per-database persistent setting, so running it on
    # every open is idempotent and cheap. Multi-user read traffic (Increment 1) makes
    # this worth it; the busy timeout above still covers the brief writer-vs-writer lock.
    conn.execute("PRAGMA journal_mode=WAL")
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


def upsert_checklist(
    conn: sqlite3.Connection, project: str, items: list, updated_at: str
) -> None:
    """Replace a project's live checklist with the items from the latest push.

    Args:
        conn: An open relay-store connection.
        project: The project the checklist belongs to.
        items: The current checklist as a list of {"text": str, "done": bool} dicts
            (the validated shape the blob carries), in file order. May be empty — an
            enabled-but-empty checklist legitimately clears the project's prior list.
        updated_at: ISO 8601 UTC timestamp of when the relay received this push.

    Why:
        The live checklist is CURRENT STATE, not history, so each push REPLACES the
        project's row rather than appending. ON CONFLICT(project) DO UPDATE makes that
        a single idempotent statement: first push inserts, every later push overwrites
        the same row. We re-encode items as JSON for the TEXT column for the same
        reason sections/participants are encoded — sqlite has no list type — and store
        it verbatim (already redacted upstream, already validated by the server).
    """
    conn.execute(
        """
        INSERT INTO relay_project_checklists (project, items, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(project) DO UPDATE SET
            items = excluded.items,
            updated_at = excluded.updated_at
        """,
        (project, json.dumps(items), updated_at),
    )
    conn.commit()


def get_checklist(conn: sqlite3.Connection, project: str) -> list | None:
    """Return a project's live checklist items, or None if it has none.

    Args:
        conn: An open relay-store connection.
        project: The project to fetch the checklist for.

    Returns:
        The checklist as a list of {"text", "done"} dicts (decoded from JSON), or None
        when the project has no checklist row. None (no row) is deliberately distinct
        from [] (a row with an empty list) so a caller can tell "never had a checklist"
        from "checklist enabled but currently empty".

    Why:
        Backs the dashboard's per-project / report-page checklist view. Decoding the
        JSON here (like _row_to_report) means the renderer gets real dicts, not a raw
        string it would have to parse. Returning None for a missing project lets the
        renderer simply omit the block rather than special-casing an error.
    """
    row = conn.execute(
        "SELECT items FROM relay_project_checklists WHERE project = ?", (project,)
    ).fetchone()
    return json.loads(row["items"]) if row is not None else None


def upsert_disciplines(
    conn: sqlite3.Connection, project: str, disciplines: list, updated_at: str
) -> None:
    """Replace a project's observed disciplines with the latest push.

    Args:
        conn: An open relay-store connection.
        project: The project the disciplines belong to.
        disciplines: The current disciplines as a list of
            {"title", "why", "scope", "source"} dicts (the validated shape the push
            carries). May be empty — an enabled-but-empty push legitimately clears the
            project's prior set.
        updated_at: ISO 8601 UTC timestamp of when the relay received this push.

    Why:
        Disciplines are CURRENT STATE, not history, so each push REPLACES the project's
        row rather than appending — identical to upsert_checklist. ON CONFLICT(project)
        DO UPDATE makes it one idempotent statement: first push inserts, later pushes
        overwrite. Stored verbatim (already redacted and validated upstream), JSON-encoded
        for the TEXT column because sqlite has no list type.
    """
    conn.execute(
        """
        INSERT INTO relay_project_disciplines (project, disciplines, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(project) DO UPDATE SET
            disciplines = excluded.disciplines,
            updated_at = excluded.updated_at
        """,
        (project, json.dumps(disciplines), updated_at),
    )
    conn.commit()


def get_disciplines(conn: sqlite3.Connection, project: str) -> list | None:
    """Return a project's observed disciplines, or None if it has none.

    Args:
        conn: An open relay-store connection.
        project: The project to fetch disciplines for.

    Returns:
        The disciplines as a list of {"title", "why", "scope", "source"} dicts (decoded
        from JSON), or None when the project has no disciplines row. None (no row) is
        deliberately distinct from [] (a row with an empty list) so a caller can tell
        "never pushed disciplines" from "pushed, but none observed".

    Why:
        Backs the dashboard's Disciplines section. Decoding the JSON here (like
        get_checklist) hands the serializer real dicts, not a raw string. Returning None
        for a project that never pushed lets the serializer simply skip it.
    """
    row = conn.execute(
        "SELECT disciplines FROM relay_project_disciplines WHERE project = ?",
        (project,),
    ).fetchone()
    return json.loads(row["disciplines"]) if row is not None else None


def disciplines_projects(conn: sqlite3.Connection) -> list[str]:
    """Return the names of every project that has pushed disciplines, sorted.

    Args:
        conn: An open relay-store connection.

    Returns:
        A sorted list of project names that have a disciplines row. Empty when none.

    Why:
        The Disciplines section enumerates exactly the projects that have disciplines —
        NOT latest_report_per_project, which only knows projects with a report or a live
        checklist and would miss a disciplines-only project. A dedicated enumeration keeps
        the section correct and independent of the report/checklist surfaces.
    """
    rows = conn.execute(
        "SELECT project FROM relay_project_disciplines ORDER BY project"
    ).fetchall()
    return [row["project"] for row in rows]


def upsert_skills(
    conn: sqlite3.Connection, project: str, skills: list, updated_at: str
) -> None:
    """Replace a project's observed skills with the latest push.

    Args:
        conn: An open relay-store connection.
        project: The project the skills belong to.
        skills: The current skills as a list of
            {"name", "category", "evidence", "weight", "signals"} dicts (the validated
            shape the push carries). May be empty — an enabled-but-empty push legitimately
            clears the project's prior set.
        updated_at: ISO 8601 UTC timestamp of when the relay received this push.

    Why:
        Skills are CURRENT STATE, not history, so each push REPLACES the project's row
        rather than appending — identical to upsert_disciplines. ON CONFLICT(project) DO
        UPDATE makes it one idempotent statement. Stored verbatim (already redacted and
        validated upstream), JSON-encoded for the TEXT column because sqlite has no list type.
    """
    conn.execute(
        """
        INSERT INTO relay_project_skills (project, skills, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(project) DO UPDATE SET
            skills = excluded.skills,
            updated_at = excluded.updated_at
        """,
        (project, json.dumps(skills), updated_at),
    )
    conn.commit()


def get_skills(conn: sqlite3.Connection, project: str) -> list | None:
    """Return a project's observed skills, or None if it has none.

    Args:
        conn: An open relay-store connection.
        project: The project to fetch skills for.

    Returns:
        The skills as a list of {"name", "category", "evidence", "weight", "signals"}
        dicts (decoded from JSON), or None when the project has no skills row. None (no
        row) is deliberately distinct from [] (a row with an empty list).

    Why:
        Backs the cross-project skills comb. Decoding the JSON here (like get_disciplines)
        hands the serializer real dicts, not a raw string. Returning None for a project
        that never pushed lets the serializer simply skip it.
    """
    row = conn.execute(
        "SELECT skills FROM relay_project_skills WHERE project = ?",
        (project,),
    ).fetchone()
    return json.loads(row["skills"]) if row is not None else None


def skills_projects(conn: sqlite3.Connection) -> list[str]:
    """Return the names of every project that has pushed skills, sorted.

    Args:
        conn: An open relay-store connection.

    Returns:
        A sorted list of project names that have a skills row. Empty when none.

    Why:
        The comb enumerates exactly the projects that have skills — NOT
        latest_report_per_project, which would miss a skills-only project. A dedicated
        enumeration keeps the section correct and independent of the report/checklist
        surfaces, mirroring disciplines_projects.
    """
    rows = conn.execute(
        "SELECT project FROM relay_project_skills ORDER BY project"
    ).fetchall()
    return [row["project"] for row in rows]


def set_project_kind(conn: sqlite3.Connection, project: str, kind: str) -> None:
    """Record a project's kind ("project" | "tracker"), upserting the meta row.

    Args:
        conn: An open relay-store connection.
        project: The project the kind belongs to.
        kind: The project kind, already validated by the server to be a known value.

    Why:
        The home splits projects from trackers, and that distinction is an explicit flag in
        the user's orion.toml that rides each push (observe-not-originate: the relay records
        what config says, it does not guess). Like upsert_checklist this is CURRENT STATE —
        one row per project, REPLACED on each push — so ON CONFLICT(project) DO UPDATE makes
        it a single idempotent statement: first push inserts, later pushes overwrite. Stored
        separately from the checklist (its own table) so an already-deployed relay needs no
        column migration.
    """
    conn.execute(
        """
        INSERT INTO relay_project_meta (project, kind)
        VALUES (?, ?)
        ON CONFLICT(project) DO UPDATE SET kind = excluded.kind
        """,
        (project, kind),
    )
    conn.commit()


def get_project_kind(conn: sqlite3.Connection, project: str) -> str:
    """Return a project's kind, defaulting to "project" when none was recorded.

    Args:
        conn: An open relay-store connection.
        project: The project to look up.

    Returns:
        The stored kind ("project" | "tracker"), or "project" when the project has no meta
        row (a report-only project, or one pushed by a producer predating the flag).

    Why:
        The default is the safe, common case: anything that has not been explicitly marked a
        tracker is a project, so an old push or a missing row reads as a plain project rather
        than an error. Backs the per-project serializer (the portfolio query reads kind via a
        LEFT JOIN instead, to stay one round-trip).
    """
    row = conn.execute(
        "SELECT kind FROM relay_project_meta WHERE project = ?", (project,)
    ).fetchone()
    return row["kind"] if row is not None else "project"


def record_observations(
    conn: sqlite3.Connection, project: str, items: list, observed_at: str
) -> None:
    """Append one observation row per checklist item (the forward-store's "remember").

    Args:
        conn: An open relay-store connection.
        project: The project the items belong to.
        items: The checklist items as validated wire dicts ({"text", "done"[, "due_date"]
            [, "key"]}), the same list upsert_checklist receives.
        observed_at: ISO 8601 UTC timestamp of when the relay received this push.

    Why:
        Where relay_project_checklists keeps only CURRENT state (one upserted row), this is
        the APPEND-ONLY history that lets the dashboard later see a deadline slip or an item
        sit open past due. Every item becomes a row stamped with the receive clock, so the
        record is a faithful, rebuildable projection of what each push claimed over time —
        it observes, it does not author. The stable identity is `item_key`: the producer's
        `key` (the tracker's bare title) when present, else the item `text`. We fall back to
        text because tasks/table items carry no status in their text, so it is already
        stable; only the tracker's status-embedding application text needs the separate key.
        done is stored as 0/1 since sqlite has no boolean. executemany keeps the whole push
        one statement + one commit.
    """
    rows = [
        (
            project,
            item.get("key") or item["text"],
            item.get("due_date"),
            1 if item.get("done") else 0,
            observed_at,
        )
        for item in items
    ]
    conn.executemany(
        """
        INSERT INTO relay_observed_items (project, item_key, due_date, done, observed_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()


def observed_history(conn: sqlite3.Connection, project: str) -> list[dict]:
    """Return a project's observation rows, oldest first (the append-only projection).

    Args:
        conn: An open relay-store connection.
        project: The project whose observation history to read.

    Returns:
        A list of {"item_key", "due_date", "done", "observed_at"} dicts, ordered by
        observed_at then id (insertion order within a timestamp), oldest first. done is
        decoded back to a bool. Empty when the project has no observations.

    Why:
        The read side of the projection — what slippage derivation (Unit 4) and the
        "rebuild the current state from history" property are checked against. Ordering
        oldest-first makes "the latest observation per item_key" a simple last-wins fold,
        and lets a slippage check walk an item_key's due_date forward in time. Kept a plain
        per-project read (no item_key filter yet) — that filter is the seam Unit 4 adds.
    """
    rows = conn.execute(
        """
        SELECT item_key, due_date, done, observed_at
        FROM relay_observed_items
        WHERE project = ?
        ORDER BY observed_at ASC, id ASC
        """,
        (project,),
    ).fetchall()
    return [
        {
            "item_key": row["item_key"],
            "due_date": row["due_date"],
            "done": bool(row["done"]),
            "observed_at": row["observed_at"],
        }
        for row in rows
    ]


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


def latest_report_per_project(
    conn: sqlite3.Connection, today: date | None = None
) -> list[dict]:
    """Summarize every project plus its single latest report, most recent first.

    Args:
        conn: An open relay-store connection.
        today: The reference date (in the relay's display zone) used to count both the
            at-risk and the slipping checklist items. None (the default) skips both
            derivations, leaving "checklist_at_risk" and "checklist_slipping" None — so a
            caller that does not care about the forward-looking badges (or a test) is
            unaffected.

    Returns:
        A list of dicts, one per project that has a report OR a live checklist, each:
        {"project", "kind", "report_count", "latest_generated_at", "latest_report_id",
        "latest_body", "checklist_updated_at", "checklist_done", "checklist_total",
        "checklist_at_risk", "checklist_slipping", "nearest_milestone"}, ordered with the
        most recently active project first. "kind" is "project" | "tracker" (E2 Inc 4),
        defaulting to "project" when the project has no recorded meta row. For a checklist-only project (a live checklist but
        zero reports) the latest-report fields are None and report_count is 0; for a
        report-only project checklist_updated_at and the checklist counts are None.
        "checklist_at_risk" is None when the project has no checklist OR when `today` was not
        supplied; "checklist_slipping" (from the observation history) is None only when
        `today` was not supplied. "nearest_milestone" is {"group", "nearest_due"} for the
        soonest-due milestone, or None when the project has no grouped+dated milestone or
        `today` was not supplied.

    Why:
        This backs the dashboard's portfolio HOME — a cross-project "see everything at
        once" overview that, unlike list_projects (count + timestamp only), also carries
        the latest report's id and body so the home can show a one-line headline per
        project and link straight to that report. It is a SEPARATE helper rather than a
        widening of list_projects: list_projects has its own documented contract and
        callers, and keeping the two apart means neither's shape drifts under the other.

        The driving row set is PROJECT-driven, not report-driven: the union of projects
        that have a report OR a live checklist. This is what lets a dashboard-only
        project (a live checklist but no reports — e.g. the applications tracker) appear
        on the home at all; querying FROM relay_reports alone dropped it entirely (no
        report → no row → no card → reachable only by direct URL). The latest-report
        fields are LEFT-JOINed onto that set, so they are NULL for a checklist-only
        project.

        "Latest" is defined EXACTLY as history() defines newest — ORDER BY generated_at
        DESC, id DESC — so the home's latest report agrees with the project page's
        history()[0]. The id tiebreak matters when two reports share a generated_at
        second (or arrive out of generation order): picking MAX(id) alone would select
        the latest-INGESTED, which can differ from the latest-GENERATED; the correlated
        subquery below picks the row history() would call first, keeping the two views
        consistent. report_count comes from a grouped COUNT LEFT-JOINed on project
        (COALESCEd to 0 for the checklist-only case). The outer ORDER BY puts the
        freshest project on top using COALESCE(latest_generated_at, checklist_updated_at)
        — so the two kinds interleave by recency — then project name as a stable
        tiebreak. Both timestamps are ISO-8601 UTC strings, so a lexical sort is also
        chronological.
    """
    rows = conn.execute(
        """
        -- The row set is every project that has a report OR a live checklist. UNION
        -- (not UNION ALL) dedupes a project that has both, so each project is one row.
        WITH projects(project) AS (
            SELECT project FROM relay_reports
            UNION
            SELECT project FROM relay_project_checklists
        )
        SELECT p.project,
               COALESCE(cnt.report_count, 0) AS report_count,
               r.id           AS latest_report_id,
               r.body         AS latest_body,
               r.generated_at AS latest_generated_at,
               pc.items       AS checklist_items,
               pc.updated_at  AS checklist_updated_at,
               -- E2 Inc 4: the project/tracker split. COALESCE so an absent meta row (a
               -- report-only project, or a pre-flag push) reads as a plain "project".
               COALESCE(pm.kind, 'project') AS kind
        FROM projects p
        -- LEFT JOIN the latest report: NULL for a checklist-only project. The correlated
        -- subquery picks the exact row history() calls newest (generated_at DESC, id
        -- DESC), so the home agrees with the project page. r.project IS NULL when the
        -- project has no reports, which is fine — the latest-* fields just come back NULL.
        LEFT JOIN relay_reports r
          ON r.project = p.project
         AND r.id = (
            SELECT id FROM relay_reports r2
            WHERE r2.project = p.project
            ORDER BY generated_at DESC, id DESC
            LIMIT 1
        )
        -- LEFT JOIN the grouped count (NULL → 0 via COALESCE above for checklist-only).
        LEFT JOIN (SELECT project, COUNT(*) AS report_count
                   FROM relay_reports GROUP BY project) cnt
          ON cnt.project = p.project
        -- LEFT JOIN the live checklist: NULL for a report-only project (badge omitted).
        -- The project key is the checklist PK, so this adds at most one row per project.
        LEFT JOIN relay_project_checklists pc
          ON pc.project = p.project
        -- LEFT JOIN the project kind: NULL (→ 'project' via COALESCE above) when unmarked.
        LEFT JOIN relay_project_meta pm
          ON pm.project = p.project
        ORDER BY COALESCE(r.generated_at, pc.updated_at) DESC, p.project ASC
        """
    ).fetchall()
    result = []
    for row in rows:
        # Precompute the done/total counts here (decode once) so the renderer just
        # presents "X/Y done" without parsing JSON. None for both when the project has
        # no checklist row — the renderer reads that as "omit the badge".
        items_json = row["checklist_items"]
        checklist_done = checklist_total = checklist_at_risk = None
        nearest_milestone = None
        if items_json is not None:
            items = json.loads(items_json)
            checklist_total = len(items)
            checklist_done = sum(1 for item in items if item.get("done"))
            # Forward-looking (E2 Inc 3): count overdue/due-soon items for the card's
            # at-risk badge, reusing the SAME decoded items (one decode). Only when a
            # reference date was supplied — derivation needs a "today", and the count
            # logic lives in the pure derive module so the badge and the per-item render
            # agree on what "at risk" means.
            if today is not None:
                checklist_at_risk = count_at_risk(items, today)
                # The portfolio "next milestone" hint (Unit 5): the milestone group whose
                # nearest OPEN deadline comes soonest, from the same decoded items. None when
                # the project has no grouped+dated milestone (the card omits the line).
                nearest_milestone = _nearest_milestone(items, today)
        # Forward-looking slippage (E2 Inc 3 Unit 4): count items slipping per the OBSERVED
        # HISTORY (a deadline that moved later, or one lingering open past due). This reads
        # the append-only observation log, NOT the current checklist row, so it is computed
        # outside the items block and gated only on a reference date. One observed_history
        # read per project (small N; batchable later if the portfolio grows large).
        checklist_slipping = (
            len(slipping_item_keys(observed_history(conn, row["project"]), today))
            if today is not None
            else None
        )
        result.append(
            {
                "project": row["project"],
                "kind": row["kind"],
                "report_count": row["report_count"],
                "latest_generated_at": row["latest_generated_at"],
                "latest_report_id": row["latest_report_id"],
                "latest_body": row["latest_body"],
                "checklist_updated_at": row["checklist_updated_at"],
                "checklist_done": checklist_done,
                "checklist_total": checklist_total,
                "checklist_at_risk": checklist_at_risk,
                "checklist_slipping": checklist_slipping,
                "nearest_milestone": nearest_milestone,
            }
        )
    return result


def _nearest_milestone(items: list, today: date) -> dict | None:
    """Return the milestone group whose nearest open deadline comes soonest, or None.

    Args:
        items: A project's live checklist items (decoded wire dicts).
        today: The reference date (display zone), forwarded to the milestone derivation.

    Returns:
        {"group": str, "nearest_due": str} for the milestone with the EARLIEST nearest_due
        across the project, or None when no milestone has a parseable open deadline.

    Why:
        The portfolio card shows a single "next milestone" hint — which section has the
        soonest outstanding deadline — so the home needs just that one group + date, not the
        full per-group breakdown (that lives on the project page). We derive the full
        milestone list (one source of truth) and pick the earliest dated one; ISO date
        strings sort chronologically, so a min over nearest_due is correct. None when nothing
        is both grouped and dated, which the renderer reads as "omit the hint".
    """
    dated = [m for m in milestones(items, today) if m["nearest_due"] is not None]
    if not dated:
        return None
    soonest = min(dated, key=lambda m: m["nearest_due"])
    return {"group": soonest["group"], "nearest_due": soonest["nearest_due"]}


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


def comments_for_project(
    conn: sqlite3.Connection, project: str, since_id: int = 0
) -> list[dict]:
    """Return a project's comments newer than `since_id`, oldest first (C2 pull-back).

    Args:
        conn: An open relay-store connection.
        project: The project name whose comments to fetch. Matched exactly against
            relay_reports.project.
        since_id: Return only comments with id strictly greater than this. Defaults
            to 0, which (since the autoincrement id starts at 1) returns ALL of the
            project's comments — the "first check" / `--all` case.

    Returns:
        A list of {"id", "report_id", "author", "body", "created_at"} dicts — the
        same shape comments_for returns — across every report in the project, in
        ascending-id (chronological) order. Empty when the project has no newer
        comments (or no reports at all).

    Why:
        The local pull-back fetches by PROJECT, because the local side never recorded
        the relay-side comment/report ids (the push discards them) — project is the
        handle the client actually holds. Comments are stored flat (report_comments
        has no project column), so this JOINs through relay_reports to resolve the
        comment->project link the schema can't express directly. The `id > since_id`
        filter is the unread cursor: ids are a monotonic autoincrement, so comparing
        ids is robust with no clock/precision/tie issues a created_at filter would
        have. ORDER BY c.id ASC means the LAST element is the highest id, which the
        endpoint uses as the watermark to advance to. Parameterized binds keep both
        `project` and `since_id` injection-safe.
    """
    rows = conn.execute(
        """
        SELECT c.id, c.report_id, c.author, c.body, c.created_at
        FROM report_comments c
        JOIN relay_reports r ON c.report_id = r.id
        WHERE r.project = ? AND c.id > ?
        ORDER BY c.id ASC
        """,
        (project, since_id),
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


# --- Multi-party access: users, scope, revocation, audit (Increment 1) ---------
# These back the dashboard's per-user auth. The store deals ONLY in the verifier
# string (HMAC of the key) — minting the raw key and computing its verifier is the
# server/auth layer's job, where the pepper lives, so no crypto belongs here. Every
# write commits immediately, matching the rest of this module.


def add_user(
    conn: sqlite3.Connection,
    name: str,
    key_verifier: str,
    role: str,
    projects: list[str],
    created_by: str,
    created_at: str,
) -> int:
    """Insert a new user and their project scope; return the new user id.

    Args:
        conn: An open relay-store connection.
        name: The user's unique display name / CLI handle.
        key_verifier: HMAC-SHA256(pepper, raw_key) — the stored credential verifier
            (the server computed it; the raw key is never passed here).
        role: "admin" or "viewer".
        projects: The viewer's allowed project names (ignored for an admin, which
            sees all; pass [] for an admin). Inserted into relay_user_projects.
        created_by: An actor label for the audit trail (e.g. "admin-token").
        created_at: ISO 8601 UTC timestamp.

    Returns:
        The new relay_users.id.

    Why:
        One call provisions identity + scope together so a half-created user can't
        exist. active/session_version take their schema defaults (1/1). The
        UNIQUE(name) and UNIQUE(key_verifier) constraints make a duplicate name or
        (astronomically unlikely) verifier collision a loud IntegrityError the server
        can map to a clean 4xx, rather than a silent second account. We INSERT OR
        IGNORE the project rows so a caller passing a duplicate project is harmless.
    """
    cursor = conn.execute(
        "INSERT INTO relay_users (name, key_verifier, role, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, key_verifier, role, created_by, created_at),
    )
    user_id = cursor.lastrowid
    for project in projects:
        conn.execute(
            "INSERT OR IGNORE INTO relay_user_projects (user_id, project) VALUES (?, ?)",
            (user_id, project),
        )
    conn.commit()
    return user_id


def get_user_by_verifier(conn: sqlite3.Connection, key_verifier: str) -> sqlite3.Row | None:
    """Look up a user by their credential verifier (the login path).

    Args:
        conn: An open relay-store connection.
        key_verifier: HMAC-SHA256(pepper, raw_key) the server computed from the
            presented key.

    Returns:
        The full user Row, or None when no user has that verifier.

    Why:
        Login resolves a presented key to a user by its verifier. We return the row
        REGARDLESS of `active` so the caller (server) can deny a revoked user
        deliberately; baking the active check in here would hide that decision. The
        UNIQUE(key_verifier) index makes this an exact single-row lookup.
    """
    return conn.execute(
        "SELECT * FROM relay_users WHERE key_verifier = ?", (key_verifier,)
    ).fetchone()


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    """Fetch a user by id (the per-request authorization re-read).

    Args:
        conn: An open relay-store connection.
        user_id: The relay_users.id carried in the session cookie's `sub`.

    Returns:
        The full user Row, or None when the id no longer exists.

    Why:
        AuthZ trusts the DB, not the cookie: every request resolves the cookie's user
        id back to the CURRENT row, so role changes, a revoked `active`, or a bumped
        `session_version` take effect immediately. A None here (deleted user) means
        the session is dead.
    """
    return conn.execute("SELECT * FROM relay_users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """Fetch a user by name (the handle CLI ops like revoke use).

    Args:
        conn: An open relay-store connection.
        name: The user's unique name.

    Returns:
        The full user Row, or None when no user has that name.

    Why:
        The admin operates on users by name (`relay-user revoke <name>`); name is
        UNIQUE so this resolves unambiguously.
    """
    return conn.execute("SELECT * FROM relay_users WHERE name = ?", (name,)).fetchone()


def projects_for_user(conn: sqlite3.Connection, user_id: int) -> list[str]:
    """Return the project names a viewer is scoped to (sorted).

    Args:
        conn: An open relay-store connection.
        user_id: The user whose scope to read.

    Returns:
        The allowed project names, alphabetically. Empty for a viewer with no grants
        (default-deny) — and also for an admin, whose all-access is decided by role,
        NOT by this list.

    Why:
        The single source of a viewer's read scope, consulted on every authorized
        route. Sorted output keeps a filtered index stable and predictable.
    """
    rows = conn.execute(
        "SELECT project FROM relay_user_projects WHERE user_id = ? ORDER BY project",
        (user_id,),
    ).fetchall()
    return [row["project"] for row in rows]


def list_users(conn: sqlite3.Connection) -> list[dict]:
    """List all users (with their project scope) for the admin's CLI view.

    Args:
        conn: An open relay-store connection.

    Returns:
        One dict per user — id, name, role, active, session_version, created_by,
        created_at, last_login_at, and projects (a list) — ordered by name. The
        `key_verifier` is DELIBERATELY excluded: a verifier never leaves the store.

    Why:
        Backs `orion relay-user list`. We omit the verifier so an admin listing can
        never surface credential material, even hashed. The per-user scope query is a
        small N+1, acceptable for this tiny, admin-only table.
    """
    rows = conn.execute(
        "SELECT id, name, role, active, session_version, created_by, created_at, "
        "last_login_at FROM relay_users ORDER BY name"
    ).fetchall()
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "role": row["role"],
            "active": bool(row["active"]),
            "session_version": row["session_version"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "last_login_at": row["last_login_at"],
            "projects": projects_for_user(conn, row["id"]),
        }
        for row in rows
    ]


def update_last_login(conn: sqlite3.Connection, user_id: int, ts: str) -> None:
    """Stamp a user's last successful login time.

    Args:
        conn: An open relay-store connection.
        user_id: The user who just logged in.
        ts: ISO 8601 UTC timestamp (the server's clock).

    Returns:
        None.

    Why:
        A small operational signal (the admin can see who is actually using their
        access, and spot stale grants). Written on each login; not load-bearing for
        auth, so a failure here would never block a login (the server orders it after
        the cookie is set).
    """
    conn.execute("UPDATE relay_users SET last_login_at = ? WHERE id = ?", (ts, user_id))
    conn.commit()


def bump_session_version(conn: sqlite3.Connection, user_id: int) -> None:
    """Invalidate a user's live sessions by advancing their session_version.

    Args:
        conn: An open relay-store connection.
        user_id: The user whose sessions to invalidate.

    Returns:
        None.

    Why:
        STATELESS revocation: a signed cookie embeds the session_version it was minted
        with, and the per-request check rejects it once it no longer matches. Bumping
        the version thus force-logs-out that one user everywhere, with no server-side
        session store and without touching anyone else.
    """
    conn.execute(
        "UPDATE relay_users SET session_version = session_version + 1 WHERE id = ?",
        (user_id,),
    )
    conn.commit()


def revoke_user(conn: sqlite3.Connection, user_id: int) -> None:
    """Deactivate a user and invalidate their live sessions, atomically.

    Args:
        conn: An open relay-store connection.
        user_id: The user to revoke.

    Returns:
        None.

    Why:
        Revoking access must do BOTH in one step: set active = 0 (so a future login
        with the key is denied) and bump session_version (so any cookie already in a
        browser stops working on its next request). Doing them in a single UPDATE
        means there is no window where one took effect but not the other.
    """
    conn.execute(
        "UPDATE relay_users SET active = 0, session_version = session_version + 1 "
        "WHERE id = ?",
        (user_id,),
    )
    conn.commit()


def record_admin_audit(
    conn: sqlite3.Connection,
    actor: str,
    action: str,
    target_user: str,
    role: str,
    projects: list[str],
    created_at: str,
) -> None:
    """Append one row to the admin/provisioning audit trail.

    Args:
        conn: An open relay-store connection.
        actor: Who performed the action (e.g. "admin-token").
        action: A short verb, e.g. "create_user" or "revoke_user".
        target_user: The affected user's name.
        role: The role involved, or "" when not applicable.
        projects: Projects involved (JSON-encoded for the TEXT column; [] is fine).
        created_at: ISO 8601 UTC timestamp.

    Returns:
        None.

    Why:
        A multi-party access model needs accountability: who granted or revoked whom,
        and with what scope. Append-only (no update/delete) so the trail can't be
        quietly rewritten. projects is JSON-encoded for the same reason sections /
        participants are on relay_reports — sqlite has no list type.
    """
    conn.execute(
        "INSERT INTO relay_admin_audit (actor, action, target_user, role, projects, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (actor, action, target_user, role, json.dumps(projects), created_at),
    )
    conn.commit()

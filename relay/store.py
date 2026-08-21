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
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .derive import (
    DUE_SOON_DAYS,
    count_at_risk,
    item_key,
    milestones,
    slipping_item_keys,
)
from .derive import effective_checklist as _derive_effective_checklist

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

-- C3 Inc 2: a project's LIVE checklist PER PRODUCER — the two-person shared base. The
-- aggregate above is last-writer-wins under multiple producers (it shows only whoever pushed
-- most recently); this keeps each identified contributor's OWN current checklist so the
-- dashboard can show one card per producer side by side. Written ALONGSIDE the aggregate (a
-- dual-write), never replacing it. As of C3 Inc 2.5 (KI-30) these per-producer rows drive the
-- displayed badge/progress via the EFFECTIVE checklist (see derive.effective_checklist) once a
-- project has ≥2 active producers; the aggregate is the 0–1-producer fallback (still the sole
-- source for anonymous/single-producer projects).
-- ONE row per (project, author_id), REPLACED on each of that producer's pushes. Only IDENTIFIED
-- producers land here (author_id is NOT NULL); a legacy anonymous push writes the aggregate
-- only. author_name is denormalized (server-derived, copied at write) so the card's label
-- survives the user's later revocation — the same convention relay_discussion_items/relay_reports
-- use. A brand-new table, so "IF NOT EXISTS" adds it on the already-deployed relay with no
-- column migration. items is the same JSON {text, done} shape the aggregate carries.
CREATE TABLE IF NOT EXISTS relay_producer_checklists (
    project     TEXT NOT NULL,      -- the project this producer's checklist belongs to
    author_id   INTEGER NOT NULL,   -- the producing contributor's relay_users id (never NULL here)
    author_name TEXT NOT NULL,      -- server-derived display name, denormalized (survives revocation)
    items       TEXT NOT NULL,      -- JSON array of {text, done} objects, in file order
    updated_at  TEXT NOT NULL,      -- ISO 8601 UTC, when the relay last received this producer's push
    PRIMARY KEY (project, author_id)
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

-- C3 Inc 2.5: a project's OBSERVED DISCIPLINES PER PRODUCER — the disciplines sibling of
-- relay_producer_checklists. The aggregate above is last-writer-wins across producers (two
-- machines syncing different portfolios flip the shared row back and forth, losing provenance
-- that CANNOT be backfilled); this keeps each identified contributor's OWN observed principles.
-- Written ALONGSIDE the aggregate (a dual-write), never replacing it — the aggregate still
-- serves the dashboard's Disciplines section. STORAGE NOW, DISPLAY LATER: these rows are stored
-- and read back by producer_disciplines_for (a seam), but no display surface consumes them yet;
-- per-producer merge/display is deferred as a KI (merging two machines' independently
-- canonicalized sets needs its own design). ONE row per (project, author_id), REPLACED on each
-- of that producer's pushes. Only IDENTIFIED producers land here (author_id NOT NULL); a legacy
-- anonymous push writes the aggregate only. author_name is denormalized (server-derived, copied
-- at write) so it survives revocation. A brand-new "IF NOT EXISTS" table, so the already-deployed
-- relay gains it with NO column migration.
CREATE TABLE IF NOT EXISTS relay_producer_disciplines (
    project     TEXT NOT NULL,      -- the project this producer's disciplines belong to
    author_id   INTEGER NOT NULL,   -- the producing contributor's relay_users id (never NULL here)
    author_name TEXT NOT NULL,      -- server-derived display name, denormalized (survives revocation)
    disciplines TEXT NOT NULL,      -- JSON array of {title, why, scope, source} objects
    updated_at  TEXT NOT NULL,      -- ISO 8601 UTC, when the relay last received this producer's push
    PRIMARY KEY (project, author_id)
);


-- Supervisor-interaction loop (E2 Inc 5, Unit 1). The append-only log behind a
-- per-project two-way discussion between a supervisor and the developer, with Orion as
-- medium + memory only (it authors NOTHING this phase — observe-not-originate). The thread
-- anchors on `project`, not on a report (the report is context inside the thread, not the
-- anchor), so reads need no JOIN. Attribution is FIRST-CLASS and SERVER-DERIVED: author_id
-- is the relay_users.id of the authenticated principal (NULL for the legacy bootstrap admin
-- and for the developer's Bearer machine reply, neither of which has a relay_users row),
-- author_name is the principal's name, and role is the principal's standing in the thread.
-- role is an open TEXT enum ("supervisor" | "developer" | "orion") validated at the server
-- boundary, not here (the store is a dumb writer); "orion" is reserved for the later
-- grounded-responder rung and is unused this phase. Append-only, time-ordered: the log IS
-- the memory, with no edit/delete path. (This is the sole conversation store since KI-28
-- Stage 2 retired report_comments — legacy comments were migrated in as discussion items.)
CREATE TABLE IF NOT EXISTS relay_discussion_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT NOT NULL,       -- the per-project thread anchor
    author_id   INTEGER,             -- relay_users.id; NULL for legacy-admin / machine posts
    author_name TEXT NOT NULL,       -- server-derived display name (never client-supplied)
    role        TEXT NOT NULL,       -- "supervisor" | "developer" | "orion" (orion reserved)
    body        TEXT NOT NULL,       -- plain text; escaped on render
    created_at  TEXT NOT NULL        -- ISO 8601 UTC, when the relay received it
);

-- Discussion items are read per project, oldest-first, often newer-than a watermark id;
-- the composite index keeps both that filter and the since_id pull fast.
CREATE INDEX IF NOT EXISTS idx_relay_discussion_items_project
    ON relay_discussion_items(project, id);

-- C3 / multi-party access (Increment 1), reshaped by the auth revamp (Units 2a-4a).
-- relay_users is the durable ACCOUNT: who someone is, what they may see, and (for an
-- agent) whose behalf they act on. Credentials no longer live here — they are N
-- individually revocable rows in relay_credentials (keys for machines, one active
-- password for an interactive human), so an account outlives any credential it holds.
-- `key_verifier` is a RETIRED column: it is NOT NULL and cannot be dropped under the
-- additive-migration idiom, so every row now carries an inert per-row sentinel that
-- verifies nothing, and no code path reads it (see add_user).
-- `active` + `session_version` give STATELESS revocation: set active = 0 and/or bump
-- session_version to invalidate a user's live sessions WITHOUT a server-side session
-- table — the signed cookie carries the session_version it was minted with and is
-- rejected once it no longer matches. `name` is UNIQUE so CLI ops (e.g. revoke <name>)
-- are unambiguous. The role column is an open enum so new roles stay additive.
CREATE TABLE IF NOT EXISTS relay_users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,        -- display name + CLI handle
    key_verifier    TEXT NOT NULL UNIQUE,        -- RETIRED: inert sentinel, never read
    role            TEXT NOT NULL,               -- admin | viewer | supervisor | contributor
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

-- Auth revamp (Unit 2a): CREDENTIALS split out from the account. An account (relay_users)
-- is the durable identity — name, role, kind, scopes — and a credential is one of N
-- attachable, individually revocable things beneath it. This is what lets one human hold a
-- login password plus two machine keys (a Mac and a WSL2 box) under a SINGLE identity,
-- instead of today's one-key-per-identity model where the identity dies with the key.
--
-- `type` is 'key' (a machine credential; verifier = HMAC-SHA256(pepper, raw_key), the same
-- construction relay_users.key_verifier used) or 'password' (an interactive credential;
-- verifier = an Argon2id encoded hash, which embeds its own salt and parameters — so the
-- one `verifier` column carries both formats without the store needing to know either).
-- `label` is display metadata ('mac', 'wsl2', 'login'); the credential ID is the identifier
-- that revocation targets, because labels are for humans and can repeat over time.
CREATE TABLE IF NOT EXISTS relay_credentials (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,            -- → relay_users.id (no FK: see the note below)
    type         TEXT NOT NULL,               -- "key" | "password"
    label        TEXT NOT NULL,               -- human label, e.g. "mac" / "wsl2" / "login"
    verifier     TEXT NOT NULL,               -- HMAC hex (key) | argon2 encoded hash (password)
    active       INTEGER NOT NULL DEFAULT 1,  -- 0 = revoked (this credential alone)
    created_at   TEXT NOT NULL,               -- ISO 8601 UTC
    last_used_at TEXT                         -- ISO 8601 UTC, NULL until first use
);

-- The Bearer resolver looks a presented key up by its verifier on EVERY authenticated push,
-- so index it. UNIQUE across all key rows regardless of `active`: a revoked verifier stays
-- reserved, so a revoked key can never be re-minted and silently resurrected.
CREATE UNIQUE INDEX IF NOT EXISTS idx_relay_credentials_key_verifier
    ON relay_credentials(verifier) WHERE type = 'key';

-- At most ONE active password per account, enforced by the DB rather than by a read-then-write
-- in application code (amendment 7): two admins setting a password concurrently would both see
-- "no active password" and both insert. A partial unique index makes that race impossible.
CREATE UNIQUE INDEX IF NOT EXISTS idx_relay_credentials_one_active_password
    ON relay_credentials(user_id) WHERE type = 'password' AND active = 1;

-- Labels are unique among an account's ACTIVE credentials, so CLI ops can address a
-- credential by label conveniently. Revoked rows are excluded, so a label is reusable once
-- its credential is retired (replacing a lost 'mac' key with a new 'mac' key).
CREATE UNIQUE INDEX IF NOT EXISTS idx_relay_credentials_active_label
    ON relay_credentials(user_id, label) WHERE active = 1;

-- Every credential lookup for an account filters on user_id; index it.
CREATE INDEX IF NOT EXISTS idx_relay_credentials_user ON relay_credentials(user_id);

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
    # Additive columns introduced after a table first shipped (the store's first ALTER —
    # every prior schema change was a whole new table). Run after the CREATEs so a fresh DB
    # and an already-deployed one converge to the same shape.
    _ensure_columns(conn)
    # Auth revamp (Unit 2a): the store's first DATA migration — copy each pre-revamp account's
    # key into a credential row. Runs after the columns exist, and is a cheap read-only no-op
    # once done (see the function's docstring for why that is safe on a per-request open).
    _migrate_key_credentials(conn)
    return conn


# Columns added to existing tables after their first ship. Keyed by table, each a list of
# (column, SQL type). All are NULLABLE (no default): old rows read NULL, which is exactly the
# "predates attribution" meaning. This is the single canonical place these columns are added —
# they are deliberately NOT in _SCHEMA's CREATE statements, so a fresh DB creates-then-alters
# (negligible) and there is one add-path to reason about, not two.
_ADDITIVE_COLUMNS: dict[str, list[tuple[str, str]]] = {
    # C3 Inc 2: producer attribution — who pushed the report (author_name denormalized so the
    # display name survives revocation, mirroring relay_discussion_items; author_id kept for
    # the link). relay_observed_items records the observing producer for future per-producer
    # slippage — provenance must be captured at write time (it cannot be backfilled).
    "relay_reports": [("author_id", "INTEGER"), ("author_name", "TEXT")],
    "relay_observed_items": [("author_id", "INTEGER")],
    # E1.2 (forward-look): a per-project "due soon" horizon, overriding the module default
    # (derive.DUE_SOON_DAYS = 7) when the user sets it in orion.toml. NULLABLE with no
    # default (omit-when-unset): an absent value reads as NULL, which get_due_soon_days maps
    # to None so the classifier falls back to the 7-day default byte-identically. Added here
    # (not in _SCHEMA) so an already-deployed relay gains the column via one cheap ALTER —
    # relay_project_meta first shipped with only `kind`.
    # Auth revamp (Unit 5): the KB scoping primitive. 'org' means every interactive MEMBER
    # may read this project without an explicit grant; 'restricted' (or NULL, which every
    # pre-Unit-5 row holds) keeps today's grant-only behavior. NULLABLE with no default so
    # the column is additive, and because NULL must read as RESTRICTED — default-deny has to
    # be what ABSENCE means, not merely what a column default says, or a project that never
    # got a meta row would fall open.
    # KB surface (Unit 2): the project's "About" line — its own doc's first paragraph,
    # observed by the producer. NULLABLE with no default (omit-when-unset): NULL reads back
    # as None (get_about), which renders no About band — absent, never an empty band. Added
    # here (not in _SCHEMA) so an already-deployed relay gains it via one cheap ALTER.
    # KB surface (S2.2): whether the project is still running ('active') or finished ('past').
    # DECLARED by an admin, never derived from staleness — quiet is not finished. NULLABLE
    # with no default so the column is additive AND so ABSENCE reads as ACTIVE: every project
    # that predates the column, and every project that never had a meta row, must keep showing
    # exactly where it showed before. (The opposite default would silently sweep the whole
    # dashboard into a "Past projects" drawer on deploy.)
    "relay_project_meta": [
        ("due_soon_days", "INTEGER"),
        ("visibility", "TEXT"),
        ("about", "TEXT"),
        ("lifecycle", "TEXT"),
    ],
    # Auth revamp (Unit 2a): what an account IS, and (for an agent) whose behalf it acts on.
    # `kind` is NULLABLE with no default rather than "human" NOT NULL, per this table's
    # convention — existing rows read NULL, and every reader maps NULL to "human" (the only
    # thing an account could have been before agents existed). `operated_by` points at the
    # operating HUMAN account for an agent, and is NULL for a human. It is deliberately NOT a
    # declared foreign key: this store never enables PRAGMA foreign_keys, so a declared FK
    # would be decorative — the lifecycle rules (an operator must be an active human, deleting
    # an operator with live agents is blocked) are enforced in code at Unit 4a, where they can
    # produce real error messages.
    "relay_users": [("kind", "TEXT"), ("operated_by", "INTEGER")],
}


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Idempotently ADD any missing additive columns (`_ADDITIVE_COLUMNS`) to their tables.

    Args:
        conn: An open relay-store connection (schema already CREATEd).

    Returns:
        None. Mutates the schema in place, committing each ALTER.

    Why:
        `CREATE TABLE IF NOT EXISTS` never adds a column to a table that already exists, so an
        already-deployed relay would keep the old shape forever. This guards each ADD COLUMN
        with `PRAGMA table_info` so it runs once and is a cheap no-op on every subsequent open
        — the store's first real migration seam, kept deliberately tiny (no framework). The
        table and column names come from a hardcoded constant, never a request, so the f-string
        interpolation carries no injection surface (sqlite cannot parameterize DDL identifiers).

        The `table_info` check alone is NOT enough under concurrency, and this store is opened
        per request: two workers opening a not-yet-migrated DB at the same instant both read
        "column missing" and both ALTER, and the loser gets OperationalError("duplicate column
        name"). That surfaces as a 500 on a real request during exactly the redeploy a migration
        happens on. Treating an already-present column as success closes the race without
        introducing locking here — the check-then-act stays, and the DB itself is the arbiter.
    """
    for table, columns in _ADDITIVE_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in existing:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                except sqlite3.OperationalError as exc:
                    # Only the lost-race case is benign; anything else is a real schema error.
                    if "duplicate column name" not in str(exc):
                        raise
    conn.commit()


# The label given to the key credential each pre-revamp account is migrated into. "default"
# rather than a guessed machine name: the store genuinely does not know what the existing key
# is installed on, and inventing "mac" would be a lie the CLI then displays.
_MIGRATED_KEY_LABEL = "default"


def _retired_column_sentinel() -> str:
    """Return a unique, inert placeholder for the retired `relay_users.key_verifier` column.

    Returns:
        A per-row unique string that verifies nothing.

    Why:
        Unit 2b moved real verifiers into `relay_credentials`, but the legacy column is
        NOT NULL and UNIQUE, and dropping a column is outside this store's additive-only
        migration idiom — so every new row must still put SOMETHING there. The value is
        generated from `uuid4`, deliberately independent of the account's actual credential:
        it is not a hash of the key, not derived from it, and not guessable from it. So even
        if some future code path were to read this column again, the value could not
        authenticate anyone. Uniqueness satisfies the UNIQUE constraint without coordination.
    """
    return f"retired-unused-column-{uuid.uuid4().hex}"


class RelayStoreMigrationError(RuntimeError):
    """Raised when the credential backfill cannot establish its postcondition.

    Why:
        The store must FAIL CLOSED. If any account were left without a key credential after
        the backfill, the Unit 2b resolver would silently stop authenticating that account —
        a working key would start returning 401 with no explanation. Refusing to hand back a
        connection turns that silent auth failure into a loud startup failure, which is the
        trade this project's fail-closed invariant asks for.
    """


def _migrate_key_credentials(conn: sqlite3.Connection) -> None:
    """Back-fill one 'key' credential per pre-revamp account, exactly once, under a lock.

    Args:
        conn: An open relay-store connection (schema CREATEd, columns ensured).

    Returns:
        None. Inserts the missing credential rows in a single serialized transaction.

    Raises:
        RelayStoreMigrationError: if any active account still lacks a key credential
            afterwards (the postcondition), so the store never serves a half-migrated DB.

    Why:
        This is the store's first DATA migration — every prior one was DDL (`_ensure_columns`).
        It copies each account's existing `relay_users.key_verifier` into a credential row, so
        every already-provisioned key keeps working with ZERO re-provisioning once Unit 2b's
        resolver reads the credentials table. No key is ever re-minted: the verifier is copied
        as-is, and the raw key it verifies is not knowable here anyway.

        Two properties make it safe to run on EVERY open, which is what `open_relay_store`
        does (it is called per request, not once at boot):

        1. It is CHEAP in the steady state. The read-only "is there anything to do" check runs
           first, outside any lock, and after the one-time migration it always answers no — so
           normal request traffic never takes a write lock here.
        2. It is SERIALIZED and idempotent when there IS work. Two workers opening the store
           concurrently would otherwise both read "no credential exists" and both insert.
           `BEGIN IMMEDIATE` takes the write lock up front so the second waits (the connection's
           5s busy timeout covers the wait), and the `WHERE NOT EXISTS` guard means the loser
           then inserts nothing rather than duplicating. A crash mid-migration simply leaves
           work for the next open to finish — there is no partial state to repair, because the
           transaction commits all-or-nothing.
    """
    # The pre-check is a plain read: no lock, no transaction. In the steady state (every
    # request after the one-time migration) this is the only statement that runs.
    if _accounts_missing_key_credentials(conn) == 0:
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        # Idempotent by construction: the NOT EXISTS subquery skips any account that already
        # has a key credential, so a re-run — or a concurrent loser that waited on the lock —
        # inserts nothing. Matched on type='key' only: a password credential must not make an
        # account look migrated.
        conn.execute(
            """
            INSERT INTO relay_credentials (user_id, type, label, verifier, active, created_at)
            SELECT u.id, 'key', ?, u.key_verifier, u.active, u.created_at
              FROM relay_users AS u
             WHERE NOT EXISTS (
                   SELECT 1 FROM relay_credentials AS c
                    WHERE c.user_id = u.id AND c.type = 'key'
             )
            """,
            (_MIGRATED_KEY_LABEL,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    # Postcondition, checked AFTER the commit against what is actually stored — not inferred
    # from the rowcount, which would only tell us what we tried to write.
    remaining = _accounts_missing_key_credentials(conn)
    if remaining:
        raise RelayStoreMigrationError(
            f"credential backfill left {remaining} account(s) without a key credential; "
            f"refusing to serve a half-migrated store"
        )


def _accounts_missing_key_credentials(conn: sqlite3.Connection) -> int:
    """Count accounts that have no 'key' credential row yet.

    Args:
        conn: An open relay-store connection.

    Returns:
        The number of `relay_users` rows with no matching type='key' credential.

    Why:
        Used twice with deliberately different meanings — as the cheap pre-check that keeps
        the migration off the hot path, and as the postcondition that proves it finished.
        Sharing one query keeps those two answers from ever disagreeing (DRY).
    """
    return conn.execute(
        """
        SELECT COUNT(*) FROM relay_users AS u
         WHERE NOT EXISTS (
               SELECT 1 FROM relay_credentials AS c
                WHERE c.user_id = u.id AND c.type = 'key'
         )
        """
    ).fetchone()[0]


def ingest(
    conn: sqlite3.Connection,
    blob: dict,
    ingested_at: str,
    author_id: int | None = None,
    author_name: str | None = None,
) -> int:
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
        author_id: The relay_users id of the producer who pushed, or None for a legacy
            (anonymous shared-token) push. SERVER-derived from the authenticated key.
        author_name: That producer's display name, snapshotted here so it survives a later
            revocation of the user, or None for a legacy push. Never client-supplied.

    Returns:
        The autoincrement id of the inserted row — the handle the dashboard uses in
        its /report/<id> route.

    Why:
        The relay's job on ingest is simply to persist the blob as-is for later
        rendering. sections and participants are JSON-encoded for storage because
        sqlite has no list/tuple type — the same choice report_history makes for
        recipients — and they are decoded back to structures on read. Returning the
        new id lets the server report 201 Created with a concrete reference. Attribution
        (C3 Inc 2) is denormalized the same way relay_discussion_items does it: the name is
        copied in at write time so the report still shows who pushed it after the user is gone.
    """
    cursor = conn.execute(
        """
        INSERT INTO relay_reports (
            project, body, sections, participants,
            share_level, lane, generated_at, orion_version, ingested_at,
            author_id, author_name
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            author_id,
            author_name,
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


def upsert_producer_checklist(
    conn: sqlite3.Connection,
    project: str,
    author_id: int,
    author_name: str,
    items: list,
    updated_at: str,
) -> None:
    """Replace ONE identified producer's live checklist for a project (C3 Inc 2).

    Args:
        conn: An open relay-store connection.
        project: The project this producer's checklist belongs to.
        author_id: The producing contributor's relay_users id (never None — only identified
            producers get a per-producer checklist; a legacy push writes the aggregate only).
        author_name: That producer's server-derived display name, stored denormalized so the
            card's label survives the user's later revocation.
        items: The producer's current checklist as {"text", "done"[, ...]} dicts, in file order.
            May be empty (an enabled-but-empty checklist legitimately clears this producer's list).
        updated_at: ISO 8601 UTC timestamp of when the relay received this producer's push.

    Why:
        The per-producer sibling of upsert_checklist: current state, one row per
        (project, author_id), REPLACED on each of that producer's pushes via
        ON CONFLICT(project, author_id). It is a DUAL-WRITE beside the aggregate, never a
        replacement. These rows feed the per-producer cards AND, as of C3 Inc 2.5 (KI-30), the
        effective-checklist merge that drives the badge/progress at ≥2 active producers (the
        aggregate is the 0–1-producer fallback). author_name is re-stamped on every push so a
        renamed producer's card tracks the latest name.
    """
    conn.execute(
        """
        INSERT INTO relay_producer_checklists (project, author_id, author_name, items, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(project, author_id) DO UPDATE SET
            author_name = excluded.author_name,
            items = excluded.items,
            updated_at = excluded.updated_at
        """,
        (project, author_id, author_name, json.dumps(items), updated_at),
    )
    conn.commit()


def producer_checklists_for(conn: sqlite3.Connection, project: str) -> list[dict]:
    """Return every producer's live checklist for a project, ordered by producer name.

    Args:
        conn: An open relay-store connection.
        project: The project to fetch per-producer checklists for.

    Returns:
        A list of {"author_id", "author_name", "items", "updated_at",
        "effective_producer_id", "effective_producer_name"} dicts (items JSON-decoded), one
        per ACTIVE identified producer that has pushed a checklist, ordered by author_name
        for stable card positions. Empty when the project has no per-producer checklists
        (never pushed by an identified producer, e.g. legacy-only or older data).
        `updated_at` (this producer's push time) feeds the effective-checklist merge's
        last-writer-per-item ordering (C3 Inc 2.5); the per-producer card consumer ignores it.

        The two `effective_producer_*` fields are Unit 4b's operator-folding inputs:
        `operated_by ?? author_id` and the matching display name. For a human they are just
        that human, so a project with no agents reads exactly as it did before.

    Why:
        Backs the project page's per-producer cards. Ordering by name keeps a card from jumping
        position when a producer re-pushes. Decoding items here (like get_checklist) hands the
        serializer real dicts. Unlike a discussion `author_name` (a historical utterance that must
        keep its author across revocation), a per-producer checklist is CURRENT STATE — a revoked
        contributor is off the project, so their card is stale and should not show. We therefore
        INNER JOIN relay_users and keep only active producers; the row's denormalized author_name
        is still what we display (re-stamped on each push, so it is current for an active producer).

        Unit 4b resolves the effective producer HERE, in the one query that already joins
        relay_users, rather than making the serializer issue a second lookup. Note the
        asymmetry that keeps this honest: `author_name` stays the row's DENORMALIZED value
        (what the producer was called when it pushed), while the operator name comes from
        the LIVE account — so renaming an operator regroups every card immediately, exactly
        as renaming one regroups report attribution in 4a.
    """
    rows = conn.execute(
        """
        SELECT pc.author_id, pc.author_name, pc.items, pc.updated_at,
               u.operated_by, op.name AS operator_name
        FROM relay_producer_checklists pc
        JOIN relay_users u ON u.id = pc.author_id
        LEFT JOIN relay_users op ON op.id = u.operated_by
        WHERE pc.project = ? AND u.active = 1
        ORDER BY pc.author_name
        """,
        (project,),
    ).fetchall()
    return [
        {
            "author_id": row["author_id"],
            "author_name": row["author_name"],
            "items": json.loads(row["items"]),
            "updated_at": row["updated_at"],
            # An agent folds into its operator; a human is its own effective producer.
            # `or` is safe rather than sloppy here: relay_users.id is AUTOINCREMENT, so a
            # real operated_by is never 0 — the only falsy value it takes is NULL.
            "effective_producer_id": row["operated_by"] or row["author_id"],
            "effective_producer_name": row["operator_name"] or row["author_name"],
        }
        for row in rows
    ]


def effective_checklist(conn: sqlite3.Connection, project: str) -> list | None:
    """Return the checklist that should drive a project's displayed badge/progress.

    Args:
        conn: An open relay-store connection.
        project: The project to resolve the effective checklist for.

    Returns:
        The per-producer-merged item list when the project has ≥2 active identified
        producers; otherwise the aggregate checklist UNCHANGED (a list, or None when the
        project has no checklist row at all — the None is preserved for existence checks).

    Why:
        The single read-side entry point for KI-30 (C3 Inc 2.5, Unit 1.1). Every display
        surface (portfolio badge, project page, report snapshot, scheduling) routes checklist
        reads through here instead of get_checklist, so they can never disagree about the
        merged numbers. The decode-then-derive split is the module's pattern: this thin
        wrapper does the I/O (fetch aggregate + per-producer rows) and hands both to the pure
        derive.effective_checklist, which owns the ≥2 gate and the merge. At 0–1 producers it
        returns the aggregate byte-identically, so single-producer / anonymous deployments are
        unchanged.
    """
    return _derive_effective_checklist(
        get_checklist(conn, project), producer_checklists_for(conn, project)
    )


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


def project_disciplines(conn: sqlite3.Connection, project: str) -> dict | None:
    """Return a project's observed disciplines with their freshness stamp, or None.

    Args:
        conn: An open relay-store connection.
        project: The project to fetch disciplines for.

    Returns:
        {"cards": list, "updated_at": str} where `cards` is the stored disciplines (a list
        of {"title", "why", "scope", "source"} dicts decoded from JSON) and `updated_at` is
        the ISO 8601 UTC time the relay last received them. None when the project has no
        disciplines row. None (no row) is deliberately distinct from a {"cards": []} result
        (a row with an empty list) so a caller can tell "never pushed" from "pushed, none
        observed".

    Why:
        Backs the project page's "Working agreements" section (Unit 5): the section shows a
        project's discipline cards plus a "updated <date>" freshness line, so the reader
        needs BOTH the cards and updated_at. Reading both columns in one query (rather than
        a separate cards read + updated_at read) keeps it a single PK lookup. Decoding the
        JSON here (like get_checklist) hands the serializer real dicts, not a raw string.
    """
    row = conn.execute(
        "SELECT disciplines, updated_at FROM relay_project_disciplines WHERE project = ?",
        (project,),
    ).fetchone()
    if row is None:
        return None
    return {"cards": json.loads(row["disciplines"]), "updated_at": row["updated_at"]}


def upsert_producer_disciplines(
    conn: sqlite3.Connection,
    project: str,
    author_id: int,
    author_name: str,
    disciplines: list,
    updated_at: str,
) -> None:
    """Replace ONE identified producer's observed disciplines for a project (C3 Inc 2.5).

    Args:
        conn: An open relay-store connection.
        project: The project this producer's disciplines belong to.
        author_id: The producing contributor's relay_users id (never None — only identified
            producers get a per-producer row; a legacy push writes the aggregate only).
        author_name: That producer's server-derived display name, stored denormalized so the
            row survives the user's later revocation.
        disciplines: The producer's current disciplines as {"title", "why", "scope", "source"}
            dicts (the validated shape the push carries). May be empty (an enabled-but-empty
            push legitimately clears this producer's set).
        updated_at: ISO 8601 UTC timestamp of when the relay received this producer's push.

    Why:
        The disciplines sibling of upsert_producer_checklist: current state, one row per
        (project, author_id), REPLACED on each of that producer's pushes via
        ON CONFLICT(project, author_id). It is a DUAL-WRITE beside the aggregate, never a
        replacement — the aggregate row still serves the Disciplines section. STORAGE NOW,
        DISPLAY LATER: no surface reads this yet (provenance can't be backfilled, so it must be
        captured now). author_name is re-stamped on every push so it tracks a renamed producer.
    """
    conn.execute(
        """
        INSERT INTO relay_producer_disciplines (project, author_id, author_name, disciplines, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(project, author_id) DO UPDATE SET
            author_name = excluded.author_name,
            disciplines = excluded.disciplines,
            updated_at = excluded.updated_at
        """,
        (project, author_id, author_name, json.dumps(disciplines), updated_at),
    )
    conn.commit()


def producer_disciplines_for(conn: sqlite3.Connection, project: str) -> list[dict]:
    """Return every producer's observed disciplines for a project, ordered by producer name.

    Args:
        conn: An open relay-store connection.
        project: The project to fetch per-producer disciplines for.

    Returns:
        A list of {"author_id", "author_name", "disciplines", "updated_at"} dicts (disciplines
        JSON-decoded), one per ACTIVE identified producer that has pushed disciplines, ordered by
        author_name. Empty when the project has no per-producer disciplines (legacy-only or never
        pushed by an identified producer).

    Why:
        The read side of the per-producer disciplines store (C3 Inc 2.5). It is a SEAM — stored
        and readable now, but no display surface consumes it yet (storage now, display later; the
        per-producer merge is a deferred KI), the same posture Inc 2 took with the
        observation author_id. Like producer_checklists_for, per-producer disciplines are CURRENT
        STATE, so we INNER JOIN relay_users and keep only ACTIVE producers — a revoked contributor
        is off the project and their stale set should not surface. Ordering by name gives stable
        positions for whatever display later consumes it.
    """
    rows = conn.execute(
        """
        SELECT pd.author_id, pd.author_name, pd.disciplines, pd.updated_at
        FROM relay_producer_disciplines pd
        JOIN relay_users u ON u.id = pd.author_id
        WHERE pd.project = ? AND u.active = 1
        ORDER BY pd.author_name
        """,
        (project,),
    ).fetchall()
    return [
        {
            "author_id": row["author_id"],
            "author_name": row["author_name"],
            "disciplines": json.loads(row["disciplines"]),
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


# --- Project meta knobs: ONE accessor pair behind five per-knob specs (AU1-R P1) -----
#
# relay_project_meta carries five independent per-project knobs — kind, due_soon_days,
# about, visibility, lifecycle. Each arrived as its own set/get pair, and the five pairs
# were near-identical copies: the same single-column upsert, the same one-column SELECT.
# Five copies is where the paydown trigger fires (AU1): a sixth knob would have been a
# sixth copy, and copies are where defaults quietly drift apart.
#
# What is NOT shared is each knob's DEFAULT, and those differ by INTENT, not by accident:
#
#   visibility     fails CLOSED to 'restricted' — absence must never open a project up
#   lifecycle      fails OPEN   to 'active'     — absence must never hide a live project
#   kind           defaults 'project'           — the safe, overwhelmingly common case
#   about          defaults None                — the deliberate "render no band" signal
#   due_soon_days  defaults None                — the deliberate "use the 7-day default"
#
# So the SQL and the shape are shared; the default (and, for the two enum knobs, which
# single stored value is honored) is a per-knob _MetaKnob spec. Each spec sits beside the
# accessors it drives, inside the section whose comment block explains its semantics.
#
# Every public function name survives as a thin wrapper: they are called 26× in server.py,
# 6× in api.py and 137× in the tests, and this change is about removing duplication
# WITHOUT disturbing that surface.


@dataclass(frozen=True)
class _MetaKnob:
    """One relay_project_meta column plus how a read resolves when it is not set.

    Args:
        column: The relay_project_meta column this knob stores. Always a module-private
            constant, never anything derived from a request — see _get_meta_column.
        default: What a read returns when the project has no meta row, and (for an enum
            knob) when the stored value is anything other than `only_value`.
        only_value: For an ENUM knob, the ONE stored value that is honored — anything else
            (SQL NULL, an unrecognized string, a missing row) resolves to `default`. None
            marks the knob a PASSTHROUGH: whatever is stored comes back as-is.

    Why:
        A frozen dataclass rather than a tuple so each field is named where it is used:
        `only_value=VISIBILITY_ORG` states the fail-closed rule outright, where `spec[2]`
        would hide it. Frozen because a knob's default is a settled decision, not state.
    """

    column: str
    default: object
    only_value: object | None = None


def _set_meta_column(
    conn: sqlite3.Connection, project: str, knob: _MetaKnob, value: object
) -> None:
    """Upsert ONE column of a project's meta row, leaving that project's other knobs alone.

    Args:
        conn: An open relay-store connection.
        project: The project whose meta row to write (created if it does not exist).
        knob: The knob being written — supplies the column name.
        value: The value to store. None writes SQL NULL, which is how every "clear it back
            to unset" path works.

    Returns:
        None. Commits before returning, as every setter in this module does.

    Why:
        The one upsert all five knobs shared verbatim. Each knob is CURRENT STATE (one row
        per project, last-writer-wins across producers), so ON CONFLICT(project) DO UPDATE
        makes each write a single idempotent statement.

        Naming ONE column in both halves is what keeps the knobs independent: on INSERT the
        unnamed columns take their schema defaults (`kind` becomes "project", the additive
        columns NULL), and on UPDATE they are not mentioned at all — so a scheduled producer
        push writing About can never reset an admin's visibility or lifecycle decision.
    """
    # knob.column comes from a module-private _MetaKnob constant and never from request
    # data, so interpolating it is safe. `project` and `value` remain bound parameters —
    # the same split the _PROJECT_UNIVERSE_SQL f-strings below rely on.
    conn.execute(
        f"""
        INSERT INTO relay_project_meta (project, {knob.column})
        VALUES (?, ?)
        ON CONFLICT(project) DO UPDATE SET {knob.column} = excluded.{knob.column}
        """,
        (project, value),
    )
    conn.commit()


def _get_meta_column(
    conn: sqlite3.Connection, project: str, knob: _MetaKnob
) -> object:
    """Read ONE column of a project's meta row, resolving "not set" to the knob's default.

    Args:
        conn: An open relay-store connection.
        project: The project to look up.
        knob: The knob being read — supplies the column, the default, and (for an enum
            knob) the single honored value.

    Returns:
        For a PASSTHROUGH knob: the stored value, or `knob.default` when the project has no
        meta row. For an ENUM knob: `knob.only_value` when exactly that was stored, else
        `knob.default`.

    Why:
        Two read shapes, because the knobs mean two different things by "not set". A
        passthrough knob treats SQL NULL as a REAL answer (None is its deliberate "unset"
        signal, which callers map to "no band" / the 7-day default), so NULL comes back as
        None rather than being replaced.

        An enum knob must NOT trust anything it does not recognize: a missing row, a row
        predating the column, a half-run migration and a typo'd value all have to collapse
        to the default. Comparing against ONE honored value makes that true by construction,
        instead of by remembering to enumerate the failure cases at each call site — and it
        is why the default stays per-knob, since visibility must fail closed where lifecycle
        must fail open.
    """
    row = conn.execute(
        # Same safety split as _set_meta_column: private column name interpolated, the
        # untrusted project name bound.
        f"SELECT {knob.column} FROM relay_project_meta WHERE project = ?", (project,)
    ).fetchone()
    if row is None:
        return knob.default
    value = row[knob.column]
    if knob.only_value is not None:
        return knob.only_value if value == knob.only_value else knob.default
    return value


# Whether a project is a plain project or a status-aware tracker. An explicit flag in the
# user's orion.toml that rides each push (observe-not-originate: the relay records what
# config says, it does not guess). Passthrough with a 'project' default — anything not
# explicitly marked a tracker is a project, so an old push or a missing row reads as a
# plain project rather than an error.
_KNOB_KIND = _MetaKnob(column="kind", default="project")


def set_project_kind(conn: sqlite3.Connection, project: str, kind: str) -> None:
    """Record a project's kind ("project" | "tracker"), upserting the meta row.

    Args:
        conn: An open relay-store connection.
        project: The project the kind belongs to.
        kind: The project kind, already validated by the server to be a known value.

    Why:
        The home splits projects from trackers, and that distinction rides each push from
        the user's config. Stored in relay_project_meta rather than beside the checklist so
        an already-deployed relay needed no new table. See _set_meta_column for the upsert
        and why it cannot disturb this project's other knobs.
    """
    _set_meta_column(conn, project, _KNOB_KIND, kind)


def get_project_kind(conn: sqlite3.Connection, project: str) -> str:
    """Return a project's kind, defaulting to "project" when none was recorded.

    Args:
        conn: An open relay-store connection.
        project: The project to look up.

    Returns:
        The stored kind ("project" | "tracker"), or "project" when the project has no meta
        row (a report-only project, or one pushed by a producer predating the flag).

    Why:
        The default is the safe, common case — see _KNOB_KIND. Backs the per-project
        serializer; the portfolio query reads kind via a LEFT JOIN instead, to stay one
        round-trip.
    """
    return _get_meta_column(conn, project, _KNOB_KIND)


# The per-project "due soon" horizon in days (E1.2), overriding derive.DUE_SOON_DAYS (7).
# Passthrough with a None default: None is the deliberate "unset" signal, distinct from any
# real value, and the caller maps it to the 7-day default. So an explicit clear (writing
# NULL) genuinely restores the default rather than leaving a stale horizon behind.
_KNOB_DUE_SOON_DAYS = _MetaKnob(column="due_soon_days", default=None)


def set_due_soon_days(
    conn: sqlite3.Connection, project: str, due_soon_days: int | None
) -> None:
    """Record (or CLEAR) a project's "due soon" horizon (days), upserting the meta row (E1.2).

    Args:
        conn: An open relay-store connection.
        project: The project the horizon belongs to.
        due_soon_days: The per-project due-soon window in days (validated by the server to
            be an int in 1..365), or None to CLEAR it back to unset. None writes SQL NULL,
            which get_due_soon_days reads back as None and the serializers resolve to the
            7-day default — so clearing genuinely restores the default (not a stale value).

    Why:
        The forward-look sibling of `kind`: a per-project knob from the user's orion.toml
        that rides each checklist push. Passing None resets the knob so a producer that
        stops setting it does not leave a stale horizon behind (the set→unset round-trip).
    """
    _set_meta_column(conn, project, _KNOB_DUE_SOON_DAYS, due_soon_days)


# The project's About line — its own doc's first paragraph, observed by the producer (KB
# surface Unit 2). Passthrough with a None default: None is the deliberate "render no About
# band at all" signal, distinct from a real (even empty-looking) string, so a project that
# never set one renders exactly as it did before the band existed.
_KNOB_ABOUT = _MetaKnob(column="about", default=None)


def set_about(conn: sqlite3.Connection, project: str, about: str | None) -> None:
    """Record (or CLEAR) a project's About line, upserting the meta row (KB surface Unit 2).

    Args:
        conn: An open relay-store connection.
        project: The project the About line belongs to.
        about: The project's redacted About text (validated by the server to be a string
            within the length cap), or None to CLEAR it back to unset. None writes SQL NULL,
            which get_about reads back as None and the serializers render as no About band.

    Why:
        The KB-content sibling of due_soon_days: a per-project observed value that rides the
        checklist carriers. Passing None writes NULL so an explicit clear genuinely restores
        "no band" rather than leaving a stale line behind.
    """
    _set_meta_column(conn, project, _KNOB_ABOUT, about)


def get_about(conn: sqlite3.Connection, project: str) -> str | None:
    """Return a project's About line, or None when it has none set (KB surface Unit 2).

    Args:
        conn: An open relay-store connection.
        project: The project to look up.

    Returns:
        The stored About string, or None when the project has no meta row OR the row predates
        the column (NULL) — the omit-when-unset case, rendered as no About band.

    Why:
        None is the deliberate "no band" signal — see _KNOB_ABOUT. Kept a separate
        one-column lookup; the portfolio query reads the column via its existing LEFT JOIN.
    """
    return _get_meta_column(conn, project, _KNOB_ABOUT)


def get_due_soon_days(conn: sqlite3.Connection, project: str) -> int | None:
    """Return a project's "due soon" horizon in days, or None when it has none set (E1.2).

    Args:
        conn: An open relay-store connection.
        project: The project to look up.

    Returns:
        The stored due_soon_days (int), or None when the project has no meta row OR the row
        predates the column (NULL) — the omit-when-unset case.

    Why:
        None is the deliberate "unset" signal — see _KNOB_DUE_SOON_DAYS; the caller maps it
        to derive.DUE_SOON_DAYS (7) so a project that never set the knob keeps today's
        behavior byte-identically. Kept a separate one-column lookup; the portfolio query
        reads the column via its existing LEFT JOIN instead, to stay one round-trip.
    """
    return _get_meta_column(conn, project, _KNOB_DUE_SOON_DAYS)


# --- Project visibility: the KB scoping primitive (auth revamp, Unit 5) --------------
#
# A project is either 'org'-visible (every interactive MEMBER may read it, no grant needed)
# or 'restricted' (grant-only — today's behavior for everyone). Default-deny is the birth
# state: an unset/NULL value and a missing meta row BOTH read as restricted, so making a
# project org-visible is always a deliberate act.
VISIBILITY_ORG = "org"
VISIBILITY_RESTRICTED = "restricted"
VISIBILITIES = (VISIBILITY_ORG, VISIBILITY_RESTRICTED)

# THE CANONICAL PROJECT UNIVERSE. A project exists iff it has at least one report OR a live
# checklist row — the same union latest_report_per_project derives the portfolio from, and
# the same test the /api/projects route's existence-hiding 404 applies. It lives here as one
# constant because Unit 5 needs it in two more places (the visibility mutation's validation
# and the org-visible lookup), and three hand-copied definitions of "exists" would be three
# chances to disagree about what a project IS.
#
# relay_project_meta is deliberately NOT part of the union: meta rows are upserted without an
# existence check and nothing deletes them, so treating a meta row as proof of existence
# would let a typo'd mutation conjure a PHANTOM project onto the dashboard.
_PROJECT_UNIVERSE_SQL = """
    SELECT project FROM relay_reports
    UNION
    SELECT project FROM relay_project_checklists
"""


def project_exists(conn: sqlite3.Connection, project: str) -> bool:
    """Return whether `project` is a real project (has a report or a live checklist).

    Args:
        conn: An open relay-store connection.
        project: The project name to check (untrusted input).

    Returns:
        True when the project is in the canonical universe, else False.

    Why:
        Backs the visibility mutation's validation. Without it, setting visibility on a
        mistyped name would INSERT a meta row for a project that does not exist — harmless
        on its own, but it becomes a phantom the moment any read path trusts meta rows.
        Checking the universe rather than the meta table is what keeps "exists" meaning one
        thing across the relay.
    """
    row = conn.execute(
        f"SELECT 1 FROM ({_PROJECT_UNIVERSE_SQL}) WHERE project = ? LIMIT 1", (project,)
    ).fetchone()
    return row is not None


# The scoping primitive as a knob spec. An ENUM knob (only_value): ONLY an explicitly stored
# 'org' is honored, so a missing row, a pre-Unit-5 NULL, a half-run migration and a typo'd
# value ALL resolve to 'restricted'. That is the fail-closed rule expressed as data rather
# than as a condition someone has to remember to write correctly.
_KNOB_VISIBILITY = _MetaKnob(
    column="visibility", default=VISIBILITY_RESTRICTED, only_value=VISIBILITY_ORG
)


def set_project_visibility(
    conn: sqlite3.Connection, project: str, visibility: str
) -> None:
    """Set a project's visibility ('org' | 'restricted'), upserting the meta row.

    Args:
        conn: An open relay-store connection.
        project: The project to set (the CALLER validates it exists — see project_exists).
        visibility: One of VISIBILITIES, already validated by the server.

    Why:
        One idempotent single-column upsert (see _set_meta_column), so a visibility flip
        never disturbs a project's kind, horizon, About, or lifecycle.
    """
    _set_meta_column(conn, project, _KNOB_VISIBILITY, visibility)


def get_project_visibility(conn: sqlite3.Connection, project: str) -> str:
    """Return a project's visibility, defaulting to 'restricted'.

    Args:
        conn: An open relay-store connection.
        project: The project to look up.

    Returns:
        'org' only when that was explicitly stored; 'restricted' for a missing meta row, a
        row predating the column (NULL), or an explicit 'restricted'.

    Why:
        Fail-closed by construction: every way of "not being told this is org-visible"
        collapses to restricted, so no project becomes readable through absence, a failed
        migration, or a row that was never written. Only an explicit, deliberate 'org' opens
        a project up. _KNOB_VISIBILITY's `only_value` is what encodes that.
    """
    return _get_meta_column(conn, project, _KNOB_VISIBILITY)


def org_visible_projects(conn: sqlite3.Connection) -> set:
    """Return the names of every REAL project marked 'org'-visible.

    Args:
        conn: An open relay-store connection.

    Returns:
        A set of project names a member may read without an explicit grant. Empty until an
        admin deliberately flips a project to 'org'.

    Why:
        Backs the member role's scope. It INTERSECTS the meta table with the canonical
        universe rather than trusting meta rows alone: a stale or mistyped meta row must
        never be able to surface a project name that does not exist. That is defense in
        depth — the mutation already validates — because this function's output is fed
        straight into an authorization decision, and the cost of the join is nil at this
        scale.
    """
    rows = conn.execute(
        f"""
        SELECT pm.project FROM relay_project_meta pm
        WHERE pm.visibility = ?
          AND pm.project IN ({_PROJECT_UNIVERSE_SQL})
        """,
        (VISIBILITY_ORG,),
    ).fetchall()
    return {row["project"] for row in rows}


# --- Project lifecycle: active vs. past (KB surface Inc 2 / S2.2) --------------------
#
# A project is either still running ('active') or finished ('past'). This is DECLARED by an
# admin (`relay-project lifecycle`), never derived from how long a project has been quiet —
# quiet is not finished, and collapsing the two would let a paused project read as shipped.
# It lives relay-side rather than in orion.toml because the relay must keep remembering the
# answer after a project stops being produced and leaves the config entirely.
#
# 'active' is the birth state: an unset/NULL value and a missing meta row BOTH read active,
# so a project only becomes past by a deliberate act. The flag's semantic weight is that a
# past project is excluded from every deadline derivation (the serializers do that) — a
# finished project must never read as overdue.
LIFECYCLE_ACTIVE = "active"
LIFECYCLE_PAST = "past"
LIFECYCLES = (LIFECYCLE_ACTIVE, LIFECYCLE_PAST)


# The active/past flag as a knob spec. Also an ENUM knob, and the instructive counterpart to
# _KNOB_VISIBILITY: identical machinery, OPPOSITE default. Only an explicitly stored 'past' is
# honored, so every form of absence resolves to 'active'. Sharing the accessor while keeping
# the default per-knob is the whole point — one hardcoded "safe default" would have to pick a
# side, and the safe side is different for these two knobs.
_KNOB_LIFECYCLE = _MetaKnob(
    column="lifecycle", default=LIFECYCLE_ACTIVE, only_value=LIFECYCLE_PAST
)

# Every knob on the meta row, for the tests that assert the shared properties (defaults on a
# missing row, defaults on a NULL column, and mutual independence) across ALL knobs. A sixth
# knob added to its section belongs here too, and then inherits that coverage.
_META_KNOBS = (
    _KNOB_KIND,
    _KNOB_DUE_SOON_DAYS,
    _KNOB_ABOUT,
    _KNOB_VISIBILITY,
    _KNOB_LIFECYCLE,
)


def set_project_lifecycle(
    conn: sqlite3.Connection, project: str, lifecycle: str
) -> None:
    """Set a project's lifecycle ('active' | 'past'), upserting the meta row (S2.2).

    Args:
        conn: An open relay-store connection.
        project: The project to set (the CALLER validates it exists — see project_exists).
        lifecycle: One of LIFECYCLES, already validated by the server.

    Why:
        One idempotent single-column upsert (see _set_meta_column), so marking a project past
        never disturbs its kind, horizon, About, or visibility. Reversible by design — setting
        'active' again restores every derivation, so a mistaken flip costs one command, not a
        data repair.
    """
    _set_meta_column(conn, project, _KNOB_LIFECYCLE, lifecycle)


def get_project_lifecycle(conn: sqlite3.Connection, project: str) -> str:
    """Return a project's lifecycle, defaulting to 'active' (S2.2).

    Args:
        conn: An open relay-store connection.
        project: The project to look up.

    Returns:
        'past' only when that was explicitly stored; 'active' for a missing meta row, a row
        predating the column (NULL), or an explicit 'active'.

    Why:
        Absence must mean ACTIVE, by exactly the same reasoning that makes absence mean
        RESTRICTED for visibility — the default has to be what the ABSENCE of a decision
        means, not merely what a column default says. Here the safe default is the opposite
        direction: no project should vanish from the live sections of the dashboard because a
        row was never written or a migration half-ran. Only an explicit, deliberate 'past'
        moves a project out of the active view. See _KNOB_LIFECYCLE.
    """
    return _get_meta_column(conn, project, _KNOB_LIFECYCLE)


def record_observations(
    conn: sqlite3.Connection,
    project: str,
    items: list,
    observed_at: str,
    author_id: int | None = None,
) -> None:
    """Append one observation row per checklist item (the forward-store's "remember").

    Args:
        conn: An open relay-store connection.
        project: The project the items belong to.
        items: The checklist items as validated wire dicts ({"text", "done"[, "due_date"]
            [, "key"]}), the same list upsert_checklist receives.
        observed_at: ISO 8601 UTC timestamp of when the relay received this push.
        author_id: The relay_users id of the producer whose push these observations came from,
            or None for a legacy (anonymous) push. Stamped because observation provenance cannot
            be reconstructed later; read since C3 Inc 2.5 by slipping_item_keys_by_author, which
            partitions each item's stream by producer so interleaved pushes from two machines do
            not corrupt the slippage signal.

    Why:
        Where relay_project_checklists keeps only CURRENT state (one upserted row), this is
        the APPEND-ONLY history that lets the dashboard later see a deadline slip or an item
        sit open past due. Every item becomes a row stamped with the receive clock, so the
        record is a faithful, rebuildable projection of what each push claimed over time —
        it observes, it does not author. The stable identity is `item_key`: the producer's
        `key` (the tracker's bare title) when present, else the item `text`. We fall back to
        text because tasks/table items carry no status in their text, so it is already
        stable; only the tracker's status-embedding application text needs the separate key.
        The identity rule is derive.item_key — the single definition slippage and the
        checklist merge also use, so a stamped observation matches its item on every path.
        done is stored as 0/1 since sqlite has no boolean. executemany keeps the whole push
        one statement + one commit.
    """
    rows = [
        (
            project,
            item_key(item),
            item.get("due_date"),
            1 if item.get("done") else 0,
            observed_at,
            author_id,
        )
        for item in items
    ]
    conn.executemany(
        """
        INSERT INTO relay_observed_items
            (project, item_key, due_date, done, observed_at, author_id)
        VALUES (?, ?, ?, ?, ?, ?)
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
        A list of {"item_key", "due_date", "done", "observed_at", "author_id"} dicts, ordered
        by observed_at then id (insertion order within a timestamp), oldest first. done is
        decoded back to a bool; author_id is the producer who recorded the row (None for a
        legacy/anonymous push). Empty when the project has no observations.

    Why:
        The read side of the projection — what slippage derivation and the "rebuild the current
        state from history" property are checked against. Ordering oldest-first makes "the
        latest observation per item_key" a simple last-wins fold, and lets a slippage check
        walk an item_key's due_date forward in time. author_id is surfaced (C3 Inc 2.5) so
        slipping_item_keys_by_author can partition each item's stream by producer before running
        is_slipping — interleaved two-machine pushes otherwise corrupt the signal.
    """
    rows = conn.execute(
        """
        SELECT item_key, due_date, done, observed_at, author_id
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
            "author_id": row["author_id"],
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

        NOTE (AU1-R P2): the dashboard index it was written for now uses
        latest_report_per_project, so this has NO production caller. It is KEPT on purpose,
        as a test-and-diagnostic probe: nine assertions across tests/test_relay_server.py and
        tests/test_relay_store.py use it to prove "no report row was written" after a rejected
        push, which is exactly the kind of negative check a reports-only summary answers
        cleanly. AU1 flagged it as dead code; it is unused-in-production, which is not the
        same thing. Deleting it would mean rewriting those nine probes for no gain — so if a
        future audit flags it again, this note is the answer.
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
        "checklist_at_risk", "checklist_slipping", "nearest_milestone", "lifecycle"}, ordered
        with the most recently active project first. "kind" is "project" | "tracker" (E2 Inc 4),
        defaulting to "project" when the project has no recorded meta row. "lifecycle" is
        "active" | "past" (S2.2), defaulting to "active" the same way — absence of a decision
        means the project is still running. For a checklist-only project (a live checklist but
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
               COALESCE(pm.kind, 'project') AS kind,
               -- E1.2 (forward-look): the per-project due-soon horizon, NULL when unset. Read
               -- via the SAME meta LEFT JOIN as kind (one round-trip). NOT coalesced here —
               -- NULL is surfaced as-is so the Python side maps it to the DUE_SOON_DAYS default.
               pm.due_soon_days AS due_soon_days,
               -- KB surface (Unit 2): the project's About line, NULL when unset. Same meta
               -- LEFT JOIN; surfaced as-is so the serializer omits the band when NULL.
               pm.about AS about,
               -- KB surface (S2.2): active vs. past. COALESCEd here (like `kind`, unlike
               -- `about`) because absence is not "unset, decide later" — absence IS active,
               -- so the row hands the serializer a definite answer rather than a NULL every
               -- caller would have to re-default.
               COALESCE(pm.lifecycle, 'active') AS lifecycle
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
        # E1.2: the project's due-soon horizon drives count_at_risk / _nearest_milestone below.
        # NULL (unset, or a pre-column row) → the module default, so an un-configured project
        # counts at-risk exactly as before. Resolved once per row and reused by both derivations.
        due_soon_days = (
            row["due_soon_days"] if row["due_soon_days"] is not None else DUE_SOON_DAYS
        )
        if items_json is not None:
            # KI-30 (C3 Inc 2.5): the badge counts derive from the EFFECTIVE checklist, not
            # the last-writer-wins aggregate. Reuse the aggregate we already decoded (avoid a
            # redundant get_checklist) and merge in this project's per-producer copies — one
            # producer_checklists_for query per project, matching the observed_history call
            # below. At 0–1 producers the merge returns the aggregate unchanged.
            aggregate_items = json.loads(items_json)
            items = _derive_effective_checklist(
                aggregate_items, producer_checklists_for(conn, row["project"])
            )
            checklist_total = len(items)
            checklist_done = sum(1 for item in items if item.get("done"))
            # Forward-looking (E2 Inc 3): count overdue/due-soon items for the card's
            # at-risk badge, reusing the SAME decoded items (one decode). Only when a
            # reference date was supplied — derivation needs a "today", and the count
            # logic lives in the pure derive module so the badge and the per-item render
            # agree on what "at risk" means.
            if today is not None:
                checklist_at_risk = count_at_risk(items, today, due_soon_days)
                # The portfolio "next milestone" hint (Unit 5): the milestone group whose
                # nearest OPEN deadline comes soonest, from the same decoded items. None when
                # the project has no grouped+dated milestone (the card omits the line).
                nearest_milestone = _nearest_milestone(items, today, due_soon_days)
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
                # E1.2: the raw per-project horizon (None when unset), carried so the portfolio
                # and scheduling serializers reuse the SAME value this row's at-risk count used,
                # without a second meta lookup. The serializer maps None → the default.
                "due_soon_days": row["due_soon_days"],
                # KB surface (Unit 2): the project's About line (None when unset), carried so
                # _portfolio_entry surfaces it without a second meta lookup.
                "about": row["about"],
                # KB surface (S2.2): "active" | "past", already COALESCEd in the query, so
                # every consumer gets a definite answer and none has to re-default a NULL.
                "lifecycle": row["lifecycle"],
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


def _nearest_milestone(
    items: list, today: date, due_soon_days: int = DUE_SOON_DAYS
) -> dict | None:
    """Return the milestone group whose nearest open deadline comes soonest, or None.

    Args:
        items: A project's live checklist items (decoded wire dicts).
        today: The reference date (display zone), forwarded to the milestone derivation.
        due_soon_days: The project's due-soon horizon, forwarded to milestones() so a
            milestone's at-risk roll-up uses the same window as the rest of the card.

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
    dated = [
        m for m in milestones(items, today, due_soon_days) if m["nearest_due"] is not None
    ]
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
        # C3 Inc 2 attribution. Present on every read via SELECT *; None for legacy/old rows.
        # author_id stays internal (the serializer drops it); author_name goes on the wire.
        "author_id": row["author_id"],
        "author_name": row["author_name"],
    }


def add_discussion_item(
    conn: sqlite3.Connection,
    project: str,
    author_id: int | None,
    author_name: str,
    role: str,
    body: str,
    created_at: str,
) -> int:
    """Append one entry to a project's discussion thread and return its new row id.

    Args:
        conn: An open relay-store connection.
        project: The project this thread belongs to. Matched exactly; this is the
            thread anchor (the report is context inside the thread, not the anchor).
        author_id: The relay_users.id of the authenticated author, or None for the
            legacy bootstrap admin and the developer's Bearer machine reply — neither
            has a relay_users row. Server-derived from the principal, never the request body.
        author_name: The author's display name, server-derived from the authenticated
            principal (never client-supplied — the attribution invariant). Already
            validated by the server.
        role: The author's standing in the thread ("supervisor" | "developer" |
            "orion"). Validated against the allowed set at the server boundary BEFORE
            this call; stored as-is here. "orion" is reserved for the later
            grounded-responder rung and is unused this phase (observe-not-originate).
        body: The plain-text message. Validated non-empty and length-capped by the
            server; stored as-is and escaped only on render.
        created_at: ISO 8601 UTC timestamp of when the relay received the entry. Passed
            in (not generated here) so the server controls the clock and this function
            stays deterministic and easy to test — same pattern as add_comment().

    Returns:
        The autoincrement id of the inserted discussion row.

    Why:
        Mirrors add_comment(): a single INSERT + commit returning the new id. The
        discussion log is append-only (no update/delete path) — the log IS the memory.
        All validation (role allowlist, body limits, scope/existence checks) lives in
        the server, the inbound boundary, keeping this a thin, trusted persistence call.
        author_id/author_name/role being server-derived is what makes attribution
        unforgeable; this function simply trusts what the boundary already proved.
    """
    cursor = conn.execute(
        """
        INSERT INTO relay_discussion_items
            (project, author_id, author_name, role, body, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (project, author_id, author_name, role, body, created_at),
    )
    conn.commit()
    return cursor.lastrowid


def discussion_items_for_project(
    conn: sqlite3.Connection, project: str, since_id: int = 0
) -> list[dict]:
    """Return a project's discussion items newer than `since_id`, oldest first.

    Args:
        conn: An open relay-store connection.
        project: The project name whose thread to fetch. Matched exactly.
        since_id: Return only items with id strictly greater than this. Defaults to 0,
            which (since the autoincrement id starts at 1) returns ALL of the project's
            items — the first-pull / `--all` case.

    Returns:
        A list of {"id", "project", "author_id", "author_name", "role", "body",
        "created_at"} dicts in ascending-id (chronological) order. Empty when the
        project has no newer items (or none at all). The LAST element holds the highest
        id, which the producer pull uses as the watermark to advance to.

    Why:
        Backs every reader of the thread: the dashboard panel, the read endpoint, and
        the CLI watermark pull. Unlike comments_for_project this needs NO JOIN — the
        thread anchors on `project` directly, so the project column resolves the link
        the comment table could only express through relay_reports. The `id > since_id`
        filter is the unread cursor: ids are a monotonic autoincrement, so comparing ids
        is robust with no clock/precision/tie issues a created_at filter would have.
        Returning [] (not None) lets renderers show a clean empty state without a null
        check. Parameterized binds keep both `project` and `since_id` injection-safe.
    """
    rows = conn.execute(
        """
        SELECT id, project, author_id, author_name, role, body, created_at
        FROM relay_discussion_items
        WHERE project = ? AND id > ?
        ORDER BY id ASC
        """,
        (project, since_id),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "project": row["project"],
            "author_id": row["author_id"],
            "author_name": row["author_name"],
            "role": row["role"],
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

# What an ACCOUNT is (auth revamp, Unit 4a). Stored in relay_users.kind, where NULL means
# human — every row predating this unit is NULL, so normalizing NULL → "human" on read is
# what makes the column additive with no backfill. These are DELIBERATELY not named plain
# "kind": relay_project_meta.kind ("project" | "tracker") is an unrelated column on another
# table, and the two must never be confused.
ACCOUNT_KIND_HUMAN = "human"
ACCOUNT_KIND_AGENT = "agent"
ACCOUNT_KINDS = (ACCOUNT_KIND_HUMAN, ACCOUNT_KIND_AGENT)


def account_kind_of(row: sqlite3.Row | dict) -> str:
    """Read an account row's kind, mapping the pre-4a NULL to "human".

    Args:
        row: A relay_users row (or dict) carrying a `kind` key.

    Returns:
        "human" or "agent" — never None.

    Why:
        Every reader needs the same NULL → "human" rule, and spreading `row["kind"] or
        "human"` across the server would make it one edit away from drifting. One
        function owns the normalization so the additive-column decision stays honest.
    """
    return row["kind"] or ACCOUNT_KIND_HUMAN


def add_user(
    conn: sqlite3.Connection,
    name: str,
    key_verifier: str,
    role: str,
    projects: list[str],
    created_by: str,
    created_at: str,
    account_kind: str | None = None,
    operated_by: int | None = None,
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
        account_kind: The ACCOUNT kind — "agent", or None for a human (Unit 4a). Named
            `account_kind` rather than `kind` deliberately: `relay_project_meta.kind`
            ("project" | "tracker") is an unrelated column on another table, and a bare
            `kind` in this module would read ambiguously.
        operated_by: For an agent, the relay_users.id of the operating HUMAN account;
            None for a human. The caller (the server) enforces the lifecycle rules —
            see the _ADDITIVE_COLUMNS note above.

    Returns:
        The new relay_users.id.

    Why:
        One call provisions identity + scope together so a half-created user can't
        exist. active/session_version take their schema defaults (1/1). The
        UNIQUE(name) and UNIQUE(key_verifier) constraints make a duplicate name or
        (astronomically unlikely) verifier collision a loud IntegrityError the server
        can map to a clean 4xx, rather than a silent second account. We INSERT OR
        IGNORE the project rows so a caller passing a duplicate project is harmless.

        Auth revamp (Unit 2b): the real verifier now goes ONLY into `relay_credentials`.
        The legacy `relay_users.key_verifier` column is NOT NULL and cannot be dropped under
        the additive-migration idiom, so it receives an inert per-row SENTINEL instead — a
        value generated independently of any credential, which therefore verifies nothing.
        Nothing reads that column any more (`get_user_by_verifier` and `rotate_key` were
        removed in this unit), so a stale or planted value there cannot authenticate.

        Auth revamp (Unit 4a): `kind`/`operated_by` are written here. A human account
        stores NULL in both, which is exactly what every pre-4a row already holds — so
        existing accounts and newly provisioned humans are indistinguishable, and no
        backfill is needed.
    """
    cursor = conn.execute(
        "INSERT INTO relay_users "
        "(name, key_verifier, role, created_by, created_at, kind, operated_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            name,
            _retired_column_sentinel(),
            role,
            created_by,
            created_at,
            account_kind,
            operated_by,
        ),
    )
    user_id = cursor.lastrowid
    for project in projects:
        conn.execute(
            "INSERT OR IGNORE INTO relay_user_projects (user_id, project) VALUES (?, ?)",
            (user_id, project),
        )
    # The account's first credential. Shares the surrounding implicit transaction (the
    # commit below closes it), so identity + scope + credential still land together or
    # not at all — the "no half-created user" property this function already promised.
    conn.execute(
        "INSERT INTO relay_credentials (user_id, type, label, verifier, created_at) "
        "VALUES (?, 'key', ?, ?, ?)",
        (user_id, _MIGRATED_KEY_LABEL, key_verifier, created_at),
    )
    conn.commit()
    return user_id


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
        created_at, last_login_at, account_kind ("human" | "agent"), operated_by_name
        (the operating human's name for an agent, else None), and projects (a list) —
        ordered by name. The `key_verifier` is DELIBERATELY excluded: a verifier never
        leaves the store.

    Why:
        Backs `orion relay-user list`. We omit the verifier so an admin listing can
        never surface credential material, even hashed. The per-user scope query is a
        small N+1, acceptable for this tiny, admin-only table.

        Unit 4a: a NULL `kind` normalizes to "human" here rather than leaking None to
        callers — every pre-4a row is NULL, and "human" is the only thing an account
        could have been before agents existed. The operator is resolved to a NAME via a
        LEFT JOIN so the admin listing never has to show an internal id.
    """
    rows = conn.execute(
        "SELECT u.id, u.name, u.role, u.active, u.session_version, u.created_by, "
        "u.created_at, u.last_login_at, u.kind, op.name AS operated_by_name "
        "FROM relay_users u "
        "LEFT JOIN relay_users op ON op.id = u.operated_by "
        "ORDER BY u.name"
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
            "account_kind": row["kind"] or ACCOUNT_KIND_HUMAN,
            "operated_by_name": row["operated_by_name"],
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

        Auth revamp (Unit 2a): revoking the account also deactivates every credential
        beneath it, so "the account is revoked" and "none of its credentials work" can
        never disagree. This cannot change behavior in this unit — nothing reads the
        credentials table until Unit 2b — but leaving active credential rows under a
        revoked account would be a trap primed to spring the moment the resolver moves.
    """
    conn.execute(
        "UPDATE relay_users SET active = 0, session_version = session_version + 1 "
        "WHERE id = ?",
        (user_id,),
    )
    conn.execute(
        "UPDATE relay_credentials SET active = 0 WHERE user_id = ?", (user_id,)
    )
    conn.commit()


def set_user_role(conn: sqlite3.Connection, user_id: int, role: str) -> None:
    """Change an account's role and force-log-out its live sessions.

    Args:
        conn: An open relay-store connection.
        user_id: The account whose role to change.
        role: The new role (the caller validates it against the provisionable set).

    Returns:
        None.

    Why:
        Roles were previously fixed at provisioning, so changing one meant delete + re-add —
        which mints a new key the person must be re-issued and drops their grants. Bumping
        `session_version` in the SAME UPDATE is the point of doing it here rather than as a
        bare column write: a role change is an authority change, and a cookie minted under
        the old role must not outlive it (the `revoke_user` guarantee, same reasoning).
    """
    conn.execute(
        "UPDATE relay_users SET role = ?, session_version = session_version + 1 WHERE id = ?",
        (role, user_id),
    )
    conn.commit()


def rename_user(conn: sqlite3.Connection, user_id: int, new_name: str) -> None:
    """Change an account's display name / CLI handle.

    Args:
        conn: An open relay-store connection.
        user_id: The account to rename.
        new_name: The new unique name.

    Returns:
        None. Raises sqlite3.IntegrityError when the name is already taken (UNIQUE).

    Why:
        The account is the durable identity under the credential split, so its name should be
        editable without re-provisioning. It deliberately does NOT rewrite the denormalized
        `author_name` on past reports or discussion items: those columns exist precisely so
        recorded history survives changes to (and deletion of) the account. A rename changes
        who this account is going forward; it does not revise the record. It also does not
        bump `session_version` — the person's authority is unchanged, only their label.
    """
    conn.execute("UPDATE relay_users SET name = ? WHERE id = ?", (new_name, user_id))
    conn.commit()


def grant_projects(
    conn: sqlite3.Connection, user_id: int, projects: list[str]
) -> None:
    """Add one or more projects to an existing user's read/write scope (C3 Inc 2 follow-up).

    Args:
        conn: An open relay-store connection.
        user_id: The user whose scope to expand.
        projects: Project names to grant. Already-granted projects are a no-op.

    Returns:
        None.

    Why:
        `add_user` sets a user's grants only at creation, so a contributor's project set was
        frozen once minted — which stranded a multi-project producer (KI-31). This is the same
        `INSERT OR IGNORE INTO relay_user_projects` loop `add_user` uses, factored out so an
        admin can widen scope later. IGNORE makes it idempotent: re-granting a held project does
        nothing, so the caller need not diff first.
    """
    for project in projects:
        conn.execute(
            "INSERT OR IGNORE INTO relay_user_projects (user_id, project) VALUES (?, ?)",
            (user_id, project),
        )
    conn.commit()


def ungrant_projects(
    conn: sqlite3.Connection, user_id: int, projects: list[str]
) -> list[str]:
    """Remove one or more projects from an existing user's explicit scope (KI-40).

    Args:
        conn: An open relay-store connection.
        user_id: The user whose scope to narrow.
        projects: Project names to ungrant. Names the user does not hold (including
            nonexistent projects) are silently skipped, mirroring grant's tolerance.

    Returns:
        The project names actually removed, sorted alphabetically. Empty when the
        user held none of the requested names (idempotent no-op).

    Why:
        The inverse `grant_projects` never had: scope is a security control, and a
        control that only widens is the wrong shape (KI-40). Reading the held set
        first (rather than trusting rowcount on a bare DELETE) lets the caller
        report removed-vs-requested precisely. Both statements run in one implicit
        transaction with a single commit, so a multi-project ungrant is atomic.
    """
    if not projects:
        # An empty IN () is a sqlite syntax error; the no-op answer is already known.
        return []
    placeholders = ", ".join("?" for _ in projects)
    held = {
        row["project"]
        for row in conn.execute(
            f"SELECT project FROM relay_user_projects WHERE user_id = ? AND project IN ({placeholders})",
            (user_id, *projects),
        )
    }
    removed = sorted(held)
    if removed:
        conn.execute(
            f"DELETE FROM relay_user_projects WHERE user_id = ? AND project IN ({placeholders})",
            (user_id, *projects),
        )
    conn.commit()
    return removed


def active_agents_operated_by(conn: sqlite3.Connection, user_id: int) -> list[str]:
    """List the names of ACTIVE agent accounts that `user_id` operates.

    Args:
        conn: An open relay-store connection.
        user_id: The candidate operator's relay_users.id.

    Returns:
        The operated agents' names, sorted; [] when the account operates none.

    Why:
        Backs the amendment-5 rule that deleting an operator is BLOCKED while active
        agents still reference it. `operated_by` is deliberately not a declared foreign
        key (this store never enables PRAGMA foreign_keys, so an FK would be decorative),
        which means nothing at the DB layer would stop a delete from silently orphaning
        an agent — leaving rows whose operator id resolves to nothing, or worse, to a
        later account that reuses the id. Returning NAMES rather than a bare count lets
        the server name them in the error, so the admin knows exactly what to reassign.

        Only ACTIVE agents block: a revoked agent is already inert, so holding a delete
        hostage to it would be busywork with no safety value.
    """
    rows = conn.execute(
        "SELECT name FROM relay_users WHERE operated_by = ? AND active = 1 ORDER BY name",
        (user_id,),
    ).fetchall()
    return [row["name"] for row in rows]


def set_operated_by(conn: sqlite3.Connection, user_id: int, operator_id: int) -> None:
    """Repoint an agent account at a different operating human.

    Args:
        conn: An open relay-store connection.
        user_id: The agent account to reassign.
        operator_id: The new operating human's relay_users.id.

    Returns:
        None.

    Why:
        The explicit escape hatch for the blocked operator delete (amendment 5): an
        operator with live agents cannot be removed until those agents are reparented.
        This reassigns DISPLAY GROUPING only — every stored report and observation keeps
        the agent's real `author_id`, so provenance is untouched and no history moves.
        The caller validates that the new operator is an active human; this is the
        write, not the policy.

        It deliberately does NOT bump `session_version`: an agent holds no session
        (a contributor cannot log in), so there is nothing to invalidate.
    """
    conn.execute(
        "UPDATE relay_users SET operated_by = ? WHERE id = ?", (operator_id, user_id)
    )
    conn.commit()


def author_attributions(conn: sqlite3.Connection, author_ids) -> dict:
    """Resolve stored author_ids to their {id: (account_kind, operator_name)} for display.

    Args:
        conn: An open relay-store connection.
        author_ids: An iterable of `author_id` values as denormalized onto report rows.
            None entries (legacy anonymous pushes) and duplicates are both tolerated.

    Returns:
        A dict mapping each RESOLVABLE author_id to (account_kind, operated_by_name) —
        e.g. {7: ("agent", "yoo"), 3: ("human", None)}. An id with no live account is
        simply ABSENT from the dict, so callers get a clean `.get(id)` miss.

    Why:
        Attribution is resolved at READ time by joining the live account rather than
        stamped onto the report at write time, so renaming an operator or reassigning an
        agent is immediately correct on every past report — one source of truth, no
        backfill. The accepted trade, recorded honestly: if the account is DELETED the
        report keeps its denormalized `author_name` but loses the badge, because there is
        no longer an account to ask. That matches the stance `delete_user` already takes
        (live state goes, history stays) rather than contradicting it.

        Bulk rather than per-report: a project page serializes its whole report history,
        so a one-at-a-time lookup would be an N+1 on the hottest read path. One query with
        an IN clause covers both call sites (a single report passes a one-element list),
        so there is exactly one place this join is written.

        An ABSENT entry, rather than a ("human", None) fallback, is the load-bearing
        distinction: "this push carried no identity" is a genuinely different fact from "a
        human pushed this", and collapsing them would badge legacy anonymous reports as
        human work the relay never actually attributed.
    """
    ids = {author_id for author_id in author_ids if author_id is not None}
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT u.id, u.kind, op.name AS operated_by_name FROM relay_users u "
        f"LEFT JOIN relay_users op ON op.id = u.operated_by WHERE u.id IN ({placeholders})",
        tuple(ids),
    ).fetchall()
    return {
        row["id"]: (account_kind_of(row), row["operated_by_name"]) for row in rows
    }


def delete_user(conn: sqlite3.Connection, user_id: int) -> None:
    """Hard-delete a user: remove the row + its grants + its live per-producer state.

    Args:
        conn: An open relay-store connection.
        user_id: The user to delete.

    Returns:
        None.

    Why:
        `revoke` sets active = 0 but keeps the row, so the user's `name` (UNIQUE) stays
        permanently occupied and can't be re-provisioned (KI-31). `delete` frees the name by
        removing the row, its `relay_user_projects` grants, and its LIVE per-producer state —
        `relay_producer_checklists` and `relay_producer_disciplines` (C3 Inc 2.5). It
        deliberately LEAVES `relay_reports` and `relay_discussion_items`: those are
        HISTORY, their `author_name` is denormalized (renders after the user is gone) and their
        `author_id` is already dropped from the wire — so a past report or reply keeps its recorded
        author. Live state goes; history stays.

        Auth revamp (Unit 2a): the account's credentials go too. This is not optional
        bookkeeping — key verifiers are UNIQUE across the credentials table, so an orphaned
        row would keep the deleted account's verifier reserved forever and make re-adding
        that same key fail with an IntegrityError.
    """
    conn.execute("DELETE FROM relay_credentials WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM relay_user_projects WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM relay_producer_checklists WHERE author_id = ?", (user_id,))
    conn.execute("DELETE FROM relay_producer_disciplines WHERE author_id = ?", (user_id,))
    conn.execute("DELETE FROM relay_users WHERE id = ?", (user_id,))
    conn.commit()


# --- Credentials: N per account, individually revocable (auth revamp, Unit 2a) ------
# The store deals only in VERIFIERS, exactly as the user accessors above do — minting a raw
# key and hashing a password both belong to the server/auth layer, where the pepper and the
# hashing dependency live. These accessors are written in this unit but not yet CALLED by the
# resolvers: Unit 2b cuts those over. They are here so the schema and its access surface land
# and get tested together, with no behavior change riding along.


def add_credential(
    conn: sqlite3.Connection,
    user_id: int,
    cred_type: str,
    label: str,
    verifier: str,
    created_at: str,
) -> int:
    """Attach a new credential to an account; return its id.

    Args:
        conn: An open relay-store connection.
        user_id: The account this credential belongs to.
        cred_type: "key" (machine) or "password" (interactive).
        label: A human label, unique among the account's ACTIVE credentials.
        verifier: HMAC hex for a key, an Argon2id encoded hash for a password.
        created_at: ISO 8601 UTC timestamp.

    Returns:
        The new relay_credentials.id — the handle revocation targets.

    Why:
        Adding a credential is how a machine is onboarded without touching identity: the
        human keeps one account, and the new box gets its own revocable key. Duplicate
        active labels, a second active password, and a re-used key verifier all raise
        IntegrityError from their indexes rather than being pre-checked here — the DB is the
        one place those races cannot slip through (amendment 7).
    """
    cursor = conn.execute(
        "INSERT INTO relay_credentials (user_id, type, label, verifier, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, cred_type, label, verifier, created_at),
    )
    conn.commit()
    return cursor.lastrowid


def get_credential_by_key_verifier(
    conn: sqlite3.Connection, verifier: str
) -> sqlite3.Row | None:
    """Look up an ACTIVE key credential by its verifier (the Bearer resolution path).

    Args:
        conn: An open relay-store connection.
        verifier: HMAC-SHA256(pepper, presented_key), hex.

    Returns:
        The credential row, or None when no active key matches.

    Why:
        This is what Unit 2b's `_resolve_bearer_principal` will call on every authenticated
        push. It filters on `active` here rather than leaving that to the caller, so a
        revoked key can never resolve through a caller that forgot to check. The account's
        own `active` flag is a separate question the resolver still asks — a live credential
        under a revoked account must not authenticate either.
    """
    return conn.execute(
        "SELECT * FROM relay_credentials WHERE verifier = ? AND type = 'key' AND active = 1",
        (verifier,),
    ).fetchone()


def get_active_password_credential(
    conn: sqlite3.Connection, user_id: int
) -> sqlite3.Row | None:
    """Return an account's active password credential, or None if it has none.

    Args:
        conn: An open relay-store connection.
        user_id: The account to look up.

    Returns:
        The single active password row, or None (the account logs in by key, or not at all).

    Why:
        Unit 3's password login verifies against this row, and "has no password yet" is a
        first-class answer rather than an error — it is the state every account is in until
        an admin sets one, and the state machine accounts stay in permanently. The partial
        unique index guarantees at most one row, so this cannot silently pick between two.
    """
    return conn.execute(
        "SELECT * FROM relay_credentials "
        "WHERE user_id = ? AND type = 'password' AND active = 1",
        (user_id,),
    ).fetchone()


def list_credentials(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """List an account's credentials (newest first), WITHOUT their verifiers.

    Args:
        conn: An open relay-store connection.
        user_id: The account whose credentials to list.

    Returns:
        A list of dicts: id, type, label, active, created_at, last_used_at.

    Why:
        Backs `relay-user key list`, so a human can see what is attached before revoking
        something. The verifier is excluded by construction, exactly as `list_users` excludes
        `key_verifier` — a verifier never leaves the store, and a listing is the easiest place
        to leak one by accident.
    """
    rows = conn.execute(
        "SELECT id, type, label, active, created_at, last_used_at "
        "FROM relay_credentials WHERE user_id = ? ORDER BY id DESC",
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def revoke_credential(conn: sqlite3.Connection, credential_id: int) -> bool:
    """Deactivate ONE credential by id; return whether a row changed.

    Args:
        conn: An open relay-store connection.
        credential_id: The credential to revoke (its id, not its label).

    Returns:
        True when an active credential was deactivated, False when the id was unknown or
        already revoked — so the caller can 404 rather than report a phantom success.

    Why:
        Targets the id, not the label: labels are display metadata and can be reused after a
        revocation, so revoking "the mac key" by name would be ambiguous the moment a
        replacement exists. Deliberately does NOT bump `session_version` (amendment 9) —
        losing a machine key must not log the human out of the dashboard everywhere. Only a
        password change or an account revocation does that.
    """
    cursor = conn.execute(
        "UPDATE relay_credentials SET active = 0 WHERE id = ? AND active = 1",
        (credential_id,),
    )
    conn.commit()
    return cursor.rowcount > 0


def set_password_credential_verifier(
    conn: sqlite3.Connection, credential_id: int, verifier: str
) -> None:
    """Replace a password credential's stored hash in place.

    Args:
        conn: An open relay-store connection.
        credential_id: The password credential to update.
        verifier: The new Argon2id encoded hash.

    Returns:
        None.

    Why:
        Used only for transparent rehashing: when a successful login finds the stored hash
        was computed with outdated Argon2 parameters, the plaintext is in hand for exactly
        that moment, so the hash is upgraded without the person doing anything. It updates
        IN PLACE rather than revoke-and-insert so the credential keeps its id, its
        created_at, and its position under the one-active-password index — the credential is
        the same credential, only its cost parameters changed.
    """
    conn.execute(
        "UPDATE relay_credentials SET verifier = ? WHERE id = ?", (verifier, credential_id)
    )
    conn.commit()


def touch_credential(conn: sqlite3.Connection, credential_id: int, ts: str) -> None:
    """Stamp a credential's last_used_at.

    Args:
        conn: An open relay-store connection.
        credential_id: The credential that just authenticated.
        ts: ISO 8601 UTC timestamp.

    Returns:
        None.

    Why:
        The mirror of `update_last_login` for machine credentials. It answers the question a
        human actually has before revoking something — "is this key still in use, and by
        what?" — which under a multi-credential account is otherwise unanswerable.
    """
    conn.execute(
        "UPDATE relay_credentials SET last_used_at = ? WHERE id = ?", (ts, credential_id)
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


# --- Search (S2.3 / KB Inc 3): read-only retrieval over the two append-only stores ---


def _like_patterns(terms: list[str]) -> list[str]:
    """Turn whitespace-split query terms into escaped `%…%` LIKE patterns.

    Args:
        terms: Non-empty list of raw query terms (already split by the caller).

    Returns:
        One `%term%` pattern per term, with `\\`, `%`, and `_` escaped so they match
        only themselves under `LIKE … ESCAPE '\\'`.

    Why:
        `%` and `_` are wildcards inside a LIKE pattern; a user searching "100%" must
        not match every report. Escaping the escape character itself first keeps a
        literal backslash in a query from re-arming the characters escaped after it.
        Shared by both search functions so the two result classes can never drift to
        different matching rules.
    """
    patterns = []
    for term in terms:
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        patterns.append(f"%{escaped}%")
    return patterns


def _search_where(terms: list[str], allowed: set | None) -> tuple[str, list]:
    """Build the shared WHERE clause + bind params for a body search.

    Args:
        terms: Non-empty list of raw query terms (AND semantics — every term must match).
        allowed: The caller's read scope, exactly as _allowed_projects resolves it:
            None = unrestricted, a set = only those projects (must be non-empty here —
            the empty set is short-circuited by the callers before any SQL).

    Returns:
        (where_sql, params): the WHERE body (without the keyword) and its bind values.

    Raises:
        ValueError: On an empty terms list — the server validates the query first, so
            an empty list is a programming error, not a user input.

    Why:
        Both search functions filter the same way on different tables; building the
        clause once keeps the AND semantics and — load-bearing — the SCOPE FILTER
        identical across the two result classes. Scope lives in the SQL (not a Python
        post-filter) so the LIMIT counts only rows the caller may see. LIKE without
        a COLLATE override is case-insensitive for ASCII; non-ASCII case sensitivity
        is a known, documented limitation at this corpus size.
    """
    if not terms:
        raise ValueError("terms must be non-empty")
    clauses = ["body LIKE ? ESCAPE '\\'"] * len(terms)
    params: list = _like_patterns(terms)
    if allowed is not None:
        placeholders = ", ".join("?" * len(allowed))
        clauses.append(f"project IN ({placeholders})")
        # sorted() gives deterministic SQL/params for a set input (stable to test/log).
        params.extend(sorted(allowed))
    return " AND ".join(clauses), params


def search_reports(
    conn: sqlite3.Connection, terms: list[str], allowed: set | None, limit: int = 50
) -> tuple[list[dict], bool]:
    """Search report bodies for all `terms`, newest first, within `allowed` scope.

    Args:
        conn: An open relay-store connection.
        terms: Non-empty list of raw query terms; a hit's body must contain EVERY term
            (case-insensitive substring — see _search_where for the semantics).
        allowed: Read scope per _allowed_projects: None = every project, a set = only
            those projects, the empty set = nothing (default-deny).
        limit: Maximum rows returned. Defaults to 50 (the settled response cap).

    Returns:
        (hits, capped): full report dicts (the _row_to_report shape history()/get()
        return — the serializer derives title/snippet from the body) ordered
        generated_at DESC with id DESC as tiebreaker, and capped=True when more
        matching rows existed beyond `limit` (never silently truncated).

    Why:
        Backs GET /api/search's report class. Plain LIKE over ~90 reports is instant
        and its semantics are explicit; FTS5 was declined with a revisit trigger
        (thousands of reports, or a real ranking need). Fetching limit+1 rows is how
        `capped` is known without a second COUNT query. The empty scope set returns
        without touching SQL — a zero-grant viewer sees nothing, and "nothing" is
        indistinguishable from a query with no matches (existence-hiding).
    """
    if allowed is not None and not allowed:
        return [], False
    where, params = _search_where(terms, allowed)
    rows = conn.execute(
        f"""
        SELECT * FROM relay_reports
        WHERE {where}
        ORDER BY generated_at DESC, id DESC
        LIMIT ?
        """,
        (*params, limit + 1),
    ).fetchall()
    capped = len(rows) > limit
    return [_row_to_report(row) for row in rows[:limit]], capped


def search_discussions(
    conn: sqlite3.Connection, terms: list[str], allowed: set | None, limit: int = 50
) -> tuple[list[dict], bool]:
    """Search discussion-item bodies for all `terms`, newest first, within `allowed` scope.

    Args:
        conn: An open relay-store connection.
        terms: Non-empty list of raw query terms (same AND/case semantics as reports).
        allowed: Read scope per _allowed_projects (None / set / empty set — see
            search_reports).
        limit: Maximum rows returned. Defaults to 50 (per result class, not shared
            with reports — one class filling up must not crowd out the other).

    Returns:
        (hits, capped): discussion dicts in the discussion_items_for_project shape,
        ordered id DESC (ids are the store's monotonic order — the watermark idiom —
        so "newest first" needs no timestamp comparison), and capped=True when more
        matching rows existed beyond `limit`.

    Why:
        Backs GET /api/search's discussion class. Kept a separate function (not one
        UNION query) so each query stays trivially readable and each class carries
        its own cap flag — the two classes are distinct on the wire by design.
    """
    if allowed is not None and not allowed:
        return [], False
    where, params = _search_where(terms, allowed)
    rows = conn.execute(
        f"""
        SELECT id, project, author_id, author_name, role, body, created_at
        FROM relay_discussion_items
        WHERE {where}
        ORDER BY id DESC
        LIMIT ?
        """,
        (*params, limit + 1),
    ).fetchall()
    capped = len(rows) > limit
    return [dict(row) for row in rows[:limit]], capped

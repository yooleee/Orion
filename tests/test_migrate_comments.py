# =============================================================================
# tests/test_migrate_comments.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the one-time comment->discussion migration tool
#                  (relay/migrate_comments.py) — the parity step of KI-28 Stage 2.
# Role in project: The tool runs against the LIVE relay DB, so its guarantees
#                  (faithful mapping, idempotency, orphan safety, a parity-guarded
#                  drop) must be pinned before it is trusted on production data.
# Test approach: each test builds a real temp store via open_relay_store, seeds
#                reports (ingest) and legacy comments (a hand-built report_comments
#                table, since the schema retired it), then drives the tool through its
#                main() entry point (argv +
#                a fresh connection per invocation, mirroring the two real command
#                runs). Assertions read back through the store's own query helpers.
# =============================================================================

import contextlib
import io
import sqlite3

from relay.migrate_comments import main
from relay.store import discussion_items_for_project, ingest, open_relay_store

# The legacy comment table's DDL. KI-28 Stage 2 removed `report_comments` from the relay
# schema, so open_relay_store no longer creates it and store.add_comment is gone. The
# migration runs against a LEGACY DB that still has this table, so the tests reconstruct it
# by hand (a faithful copy of the retired schema) and seed rows via raw SQL — the same shape
# the migration reads. This keeps the migration test independent of the retired code.
_LEGACY_REPORT_COMMENTS_DDL = """
CREATE TABLE IF NOT EXISTS report_comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id   INTEGER NOT NULL,
    author      TEXT NOT NULL,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def _seed_legacy_comment(conn, report_id, author, body, created_at):
    """Insert one legacy comment row, creating the retired table on first use.

    Args:
        conn: An open relay-store connection.
        report_id: The relay_reports.id the comment hangs off (may be an orphan id).
        author: The self-entered display name, or "" when omitted.
        body: The comment text.
        created_at: ISO 8601 UTC timestamp.

    Why:
        Stands in for the removed store.add_comment so the tests can build the exact
        pre-migration state (a legacy `report_comments` table with rows) that the tool
        migrates from.
    """
    conn.executescript(_LEGACY_REPORT_COMMENTS_DDL)
    conn.execute(
        "INSERT INTO report_comments (report_id, author, body, created_at) VALUES (?, ?, ?, ?)",
        (report_id, author, body, created_at),
    )
    conn.commit()


def _blob(project="demo", *, generated_at="2026-06-18T00:00:00+00:00"):
    """Build a minimal portable-blob dict in the shape ingest expects.

    Args:
        project: The project name the seeded report belongs to.
        generated_at: The blob's build timestamp (varied where ordering matters).

    Why:
        Comments resolve their project by joining through the report they hang off, so
        every test needs at least one real report per project. Kept minimal and inline
        (not via orion.report) so the store test stays independent of the local package,
        matching test_relay_store.py's own _blob helper.
    """
    return {
        "project": project,
        "participants": ["Supervisor A"],
        "share_level": "high_level",
        "lane": "raw",
        "body": "Shipped the seam.",
        "generated_at": generated_at,
        "orion_version": "0.0.0",
        "sections": [["Code activity", "Shipped the seam."]],
    }


def _table_exists(db, name) -> bool:
    """Return True if `name` is a table in the DB, using a RAW connection.

    Args:
        db: Path to the sqlite file.
        name: Table name to check for.

    Why:
        A raw sqlite3.connect reads the on-disk schema as-is, so a "table was dropped"
        assertion is honest and needs no store machinery. (report_comments is not part of
        the relay's _SCHEMA — Unit 0.3a removed it — so open_relay_store does not recreate
        it either; the raw read just keeps this helper independent of that.)
    """
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def test_round_trip_across_two_projects(tmp_path):
    """Every comment becomes a discussion item on the right project, faithfully.

    Why this matters: the core parity promise. Body, author, role, and timestamp must
    survive the migration, and each comment must land on the project of the report it
    hung off — proving the JOIN-through-reports resolution works across projects.
    """
    db = tmp_path / "relay.sqlite3"
    conn = open_relay_store(db)
    alpha = ingest(conn, _blob("alpha"), "2026-06-18T00:00:01+00:00")
    beta = ingest(conn, _blob("beta"), "2026-06-18T00:00:02+00:00")
    _seed_legacy_comment(conn, alpha, "Supervisor A", "Nice progress.", "2026-06-19T10:00:00+00:00")
    _seed_legacy_comment(conn, beta, "Supervisor B", "Ship it.", "2026-06-19T11:00:00+00:00")
    conn.close()

    assert main(["migrate", "--db", str(db)]) == 0

    conn = open_relay_store(db)
    alpha_items = discussion_items_for_project(conn, "alpha")
    beta_items = discussion_items_for_project(conn, "beta")
    conn.close()

    assert len(alpha_items) == 1
    item = alpha_items[0]
    assert item["author_name"] == "Supervisor A"
    assert item["role"] == "supervisor"  # honest best-effort historical mapping
    assert item["author_id"] is None  # legacy comments carried no identity
    assert item["created_at"] == "2026-06-19T10:00:00+00:00"  # timestamp preserved
    assert item["body"] == f"[re: report {alpha}]\nNice progress."
    assert len(beta_items) == 1
    assert beta_items[0]["body"] == f"[re: report {beta}]\nShip it."


def test_blank_author_becomes_anonymous(tmp_path):
    """A comment with an empty author maps to the display name "anonymous".

    Why this matters: comments pre-C3 allowed an empty author. A blank name would
    render as a gap in the thread, so the migration substitutes a real label.
    """
    db = tmp_path / "relay.sqlite3"
    conn = open_relay_store(db)
    rid = ingest(conn, _blob(), "2026-06-18T00:00:01+00:00")
    _seed_legacy_comment(conn, rid, "", "No name given.", "2026-06-19T10:00:00+00:00")
    conn.close()

    assert main(["migrate", "--db", str(db)]) == 0

    conn = open_relay_store(db)
    items = discussion_items_for_project(conn, "demo")
    conn.close()
    assert items[0]["author_name"] == "anonymous"


def test_migrate_is_idempotent(tmp_path):
    """Running migrate twice produces no duplicate discussion rows.

    Why this matters: the migration touches the live DB and may be re-run (after a
    partial run, or by mistake). The four-tuple idempotency key must make the second
    run a no-op — the defining safety property of the tool.
    """
    db = tmp_path / "relay.sqlite3"
    conn = open_relay_store(db)
    rid = ingest(conn, _blob(), "2026-06-18T00:00:01+00:00")
    _seed_legacy_comment(conn, rid, "Supervisor A", "Once only.", "2026-06-19T10:00:00+00:00")
    conn.close()

    assert main(["migrate", "--db", str(db)]) == 0
    assert main(["migrate", "--db", str(db)]) == 0  # second run must add nothing

    conn = open_relay_store(db)
    items = discussion_items_for_project(conn, "demo")
    conn.close()
    assert len(items) == 1


def test_orphan_comment_is_skipped_and_reported(tmp_path):
    """A comment whose report is missing is not migrated and is reported loudly.

    Why this matters: report_id is not an enforced FK, so an orphan is structurally
    possible. It has no project to attach to, so it cannot be migrated — but it must
    never vanish silently. The tool skips it and names it on stderr.
    """
    db = tmp_path / "relay.sqlite3"
    conn = open_relay_store(db)
    rid = ingest(conn, _blob(), "2026-06-18T00:00:01+00:00")
    _seed_legacy_comment(conn, rid, "Supervisor A", "Real one.", "2026-06-19T10:00:00+00:00")
    # report_id 999 has no matching report row — an orphan by construction.
    _seed_legacy_comment(conn, 999, "Ghost", "Orphaned.", "2026-06-19T11:00:00+00:00")
    conn.close()

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert main(["migrate", "--db", str(db)]) == 0

    conn = open_relay_store(db)
    items = discussion_items_for_project(conn, "demo")
    conn.close()
    assert len(items) == 1  # only the real comment migrated
    assert "orphan" in err.getvalue().lower()
    assert "999" in err.getvalue()  # the missing report_id is named


def test_dry_run_writes_nothing(tmp_path):
    """migrate --dry-run reports the plan but inserts no discussion rows.

    Why this matters: the dry-run is how a real run is rehearsed against a backup of
    production before touching live data. It must be strictly read-only.
    """
    db = tmp_path / "relay.sqlite3"
    conn = open_relay_store(db)
    rid = ingest(conn, _blob(), "2026-06-18T00:00:01+00:00")
    _seed_legacy_comment(conn, rid, "Supervisor A", "Preview me.", "2026-06-19T10:00:00+00:00")
    conn.close()

    assert main(["migrate", "--db", str(db), "--dry-run"]) == 0

    conn = open_relay_store(db)
    items = discussion_items_for_project(conn, "demo")
    conn.close()
    assert items == []  # nothing written on a dry run


def test_drop_is_parity_guarded(tmp_path):
    """drop refuses before migration and succeeds (idempotently) after it.

    Why this matters: migrate-and-drop is two deliberate steps. The guard enforces
    "no comment is lost" structurally — it must refuse to drop while any comment is
    unmigrated, then drop cleanly once parity is reached, and no-op on a re-run.
    """
    db = tmp_path / "relay.sqlite3"
    conn = open_relay_store(db)
    rid = ingest(conn, _blob(), "2026-06-18T00:00:01+00:00")
    _seed_legacy_comment(conn, rid, "Supervisor A", "Keep me.", "2026-06-19T10:00:00+00:00")
    conn.close()

    # Guard refuses while the comment is unmigrated, and leaves the table intact.
    assert main(["drop", "--db", str(db)]) == 1
    assert _table_exists(db, "report_comments") is True

    # Migrate to parity, then drop succeeds and removes the table.
    assert main(["migrate", "--db", str(db)]) == 0
    assert main(["drop", "--db", str(db)]) == 0
    assert _table_exists(db, "report_comments") is False

    # A second drop is a clean no-op: report_comments is already gone (the schema no longer
    # recreates it), so the missing-table guards make drop a parity-OK zero-row no-op that
    # never errors.
    assert main(["drop", "--db", str(db)]) == 0
    assert _table_exists(db, "report_comments") is False


def test_duplicate_comments_collapse_and_drop_refuses(tmp_path):
    """Two byte-identical comments collapse to one row, and `drop` then REFUSES.

    Why this matters: the four-tuple key is content-based and created_at is only
    second-precise, so a double-click / retry can produce two genuinely-distinct source
    comments that share a key. migrate can only write one row for them; if `drop` then ran,
    the second comment would be lost forever. This pins the safety net: migrate reports the
    duplicate, and the 1:1 drop guard refuses (naming both ids) rather than destroying data —
    the "no comment is lost" invariant holds even on the collision path.
    """
    db = tmp_path / "relay.sqlite3"
    conn = open_relay_store(db)
    rid = ingest(conn, _blob(), "2026-06-18T00:00:01+00:00")
    # Two DISTINCT rows (different ids) that are byte-identical across the four-tuple.
    _seed_legacy_comment(conn, rid, "Supervisor A", "Nice work.", "2026-06-19T10:00:00+00:00")
    _seed_legacy_comment(conn, rid, "Supervisor A", "Nice work.", "2026-06-19T10:00:00+00:00")
    conn.close()

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert main(["migrate", "--db", str(db)]) == 0

    # Only ONE discussion row was written for the two colliding comments (the key is identity).
    conn = open_relay_store(db)
    items = discussion_items_for_project(conn, "demo")
    conn.close()
    assert len(items) == 1
    assert "duplicate" in err.getvalue().lower()

    # drop must REFUSE — dropping now would lose the collapsed duplicate — and name it.
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert main(["drop", "--db", str(db)]) == 1
    assert "collapsed" in err.getvalue().lower()
    assert _table_exists(db, "report_comments") is True  # source data preserved, not dropped

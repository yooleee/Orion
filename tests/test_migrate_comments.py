# =============================================================================
# tests/test_migrate_comments.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the one-time comment->discussion migration tool
#                  (relay/migrate_comments.py) — the parity step of KI-28 Stage 2.
# Role in project: The tool runs against the LIVE relay DB, so its guarantees
#                  (faithful mapping, idempotency, orphan safety, a parity-guarded
#                  drop) must be pinned before it is trusted on production data.
# Test approach: each test builds a real temp store via open_relay_store, seeds
#                reports (ingest) and comments (add_comment) exactly as the relay
#                would, then drives the tool through its main() entry point (argv +
#                a fresh connection per invocation, mirroring the two real command
#                runs). Assertions read back through the store's own query helpers.
# =============================================================================

import contextlib
import io
import sqlite3

from relay.migrate_comments import main
from relay.store import add_comment, discussion_items_for_project, ingest, open_relay_store


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
        open_relay_store recreates the whole schema on connect (IF NOT EXISTS), which
        would resurrect a just-dropped table before we could observe it gone. A raw
        sqlite3.connect reads the on-disk schema as-is, so the "table was dropped"
        assertion is honest.
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
    add_comment(conn, alpha, "Supervisor A", "Nice progress.", "2026-06-19T10:00:00+00:00")
    add_comment(conn, beta, "Supervisor B", "Ship it.", "2026-06-19T11:00:00+00:00")
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
    add_comment(conn, rid, "", "No name given.", "2026-06-19T10:00:00+00:00")
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
    add_comment(conn, rid, "Supervisor A", "Once only.", "2026-06-19T10:00:00+00:00")
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
    add_comment(conn, rid, "Supervisor A", "Real one.", "2026-06-19T10:00:00+00:00")
    # report_id 999 has no matching report row — an orphan by construction.
    add_comment(conn, 999, "Ghost", "Orphaned.", "2026-06-19T11:00:00+00:00")
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
    add_comment(conn, rid, "Supervisor A", "Preview me.", "2026-06-19T10:00:00+00:00")
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
    add_comment(conn, rid, "Supervisor A", "Keep me.", "2026-06-19T10:00:00+00:00")
    conn.close()

    # Guard refuses while the comment is unmigrated, and leaves the table intact.
    assert main(["drop", "--db", str(db)]) == 1
    assert _table_exists(db, "report_comments") is True

    # Migrate to parity, then drop succeeds and removes the table.
    assert main(["migrate", "--db", str(db)]) == 0
    assert main(["drop", "--db", str(db)]) == 0
    assert _table_exists(db, "report_comments") is False

    # A second drop is a clean no-op (open_relay_store recreates an empty table, which
    # drop then removes again) — re-running the tool must never error.
    assert main(["drop", "--db", str(db)]) == 0
    assert _table_exists(db, "report_comments") is False

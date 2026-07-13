# =============================================================================
# tests/test_drop_retired_tables.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the one-time skills-table drop tool
#                  (relay/drop_retired_tables.py) — the destructive step of the
#                  living-resume retirement (Unit 4).
# Role in project: The tool runs against the LIVE relay DB, so its guarantees
#                  (dry-run by default, backup-before-drop, a LITERAL 2-table
#                  allowlist that cannot touch adjacent tables, idempotency, and a
#                  sentinel guard against the wrong DB) must be pinned before it is
#                  trusted on production data.
# Test approach: build a real temp store via open_relay_store (current schema — no
#                skills tables), then hand-add the RETIRED skills tables + rows (the
#                schema removed them in Unit 3), and drive the tool through its main()
#                entry point (argv + a fresh sqlite connection per invocation, like a
#                real command run). Assertions read the schema back with sqlite_master.
# =============================================================================

import sqlite3

import pytest

from relay.drop_retired_tables import main
from relay.store import open_relay_store

# The retired skills DDLs. Unit 3 removed these from the relay schema, so
# open_relay_store no longer creates them. The tool runs against a LEGACY DB that still
# has them, so the tests reconstruct them by hand (a faithful copy of the retired shape)
# and seed rows via raw SQL — keeping the drop test independent of the removed code.
_LEGACY_SKILLS_DDL = """
CREATE TABLE IF NOT EXISTS relay_project_skills (
    project    TEXT PRIMARY KEY,
    skills     TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS relay_producer_skills (
    project     TEXT NOT NULL,
    author_id   INTEGER NOT NULL,
    author_name TEXT NOT NULL,
    skills      TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (project, author_id)
);
"""


def _seed_legacy_skills(db_path):
    """Create the retired skills tables with a row each in the DB at `db_path`.

    Args:
        db_path: Path to the sqlite store to add the legacy tables to.

    Why:
        Reproduces the pre-drop state a live DB is in after Unit 3 shipped: the code no
        longer creates the skills tables, but the deployed DB still HOLDS them (with
        data) until this tool drops them. Seeding a row lets the dry-run report a real
        count and proves the drop removes populated tables.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_LEGACY_SKILLS_DDL)
        conn.execute(
            "INSERT INTO relay_project_skills (project, skills, updated_at) VALUES (?, ?, ?)",
            ("demo", "[]", "2026-07-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO relay_producer_skills "
            "(project, author_id, author_name, skills, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("demo", 1, "Teammate B", "[]", "2026-07-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def _tables(db_path):
    """Return the set of table names in the DB at `db_path`.

    Args:
        db_path: Path to the sqlite store.

    Returns:
        A set of table names (from sqlite_master).

    Why:
        The tool's whole job is schema shape, so tests assert on the literal set of
        tables present before and after — the most direct check that the RIGHT tables
        went and the adjacent ones stayed.
    """
    conn = sqlite3.connect(db_path)
    try:
        return {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        conn.close()


def _fresh_relay_db(tmp_path):
    """Create a real relay store (current schema) and return its path.

    Args:
        tmp_path: pytest's per-test temp dir.

    Returns:
        The Path to a freshly-initialized relay DB (has relay_reports, relay_project_meta,
        and the disciplines tables — but NOT the skills tables, which Unit 3 removed).

    Why:
        open_relay_store builds the CURRENT schema, giving the tests the exact set of
        adjacent tables the drop must preserve (the sentinel + project-meta + both
        disciplines tables), so "spares the neighbours" is a real assertion, not a mock.
    """
    db_path = tmp_path / "relay.sqlite3"
    open_relay_store(db_path).close()
    return db_path


def test_dry_run_is_the_default_and_drops_nothing(tmp_path, capsys):
    """With no --drop, the tool reports the tables but leaves them in place.

    Why this matters: the destructive action must never be the accident. Running the
    tool with only --db is a safe preview — both skills tables must still exist after.
    """
    db_path = _fresh_relay_db(tmp_path)
    _seed_legacy_skills(db_path)

    rc = main(["--db", str(db_path)])
    assert rc == 0

    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "relay_project_skills (1 row(s))" in out
    # Nothing dropped: both tables survive a dry-run.
    assert {"relay_project_skills", "relay_producer_skills"} <= _tables(db_path)


def test_drop_removes_both_skills_tables_and_backs_up_first(tmp_path, capsys):
    """--drop backs up the DB, then removes exactly the two skills tables.

    Why this matters: this is the real production step. The backup must be written
    (a recoverable safety copy) BEFORE the tables go, and afterwards neither skills
    table may remain.
    """
    db_path = _fresh_relay_db(tmp_path)
    _seed_legacy_skills(db_path)
    backup = tmp_path / "backup.sqlite3"

    rc = main(["--db", str(db_path), "--drop", "--backup", str(backup)])
    assert rc == 0

    # The backup exists and still HAS the skills tables (it predates the drop) — so it
    # is a genuine restore point, not a copy of the post-drop DB.
    assert backup.exists()
    assert {"relay_project_skills", "relay_producer_skills"} <= _tables(backup)
    # The live DB no longer has either skills table.
    assert "relay_project_skills" not in _tables(db_path)
    assert "relay_producer_skills" not in _tables(db_path)


def test_drop_spares_adjacent_meta_and_disciplines_tables(tmp_path):
    """The drop touches ONLY the two skills tables — meta + disciplines survive.

    Why this matters: relay_project_meta and both *_disciplines tables sit right beside
    the skills tables in the schema. The literal allowlist must protect them; this is the
    blast-radius guard.
    """
    db_path = _fresh_relay_db(tmp_path)
    _seed_legacy_skills(db_path)
    must_survive = {
        "relay_reports",
        "relay_project_meta",
        "relay_project_disciplines",
        "relay_producer_disciplines",
    }
    # Sanity: the fresh store really does have these neighbours before we drop.
    assert must_survive <= _tables(db_path)

    main(["--db", str(db_path), "--drop", "--backup", str(tmp_path / "b.sqlite3")])

    assert must_survive <= _tables(db_path)  # every neighbour still present


def test_drop_is_idempotent(tmp_path, capsys):
    """A second --drop (tables already gone) is a clean no-op, not an error.

    Why this matters: re-running a maintenance tool must be safe. After the first drop
    the tool should report "nothing to drop" and exit 0 without needing a backup.
    """
    db_path = _fresh_relay_db(tmp_path)
    _seed_legacy_skills(db_path)
    main(["--db", str(db_path), "--drop", "--backup", str(tmp_path / "b1.sqlite3")])

    # Second run: no skills tables remain. It must not require (or write) a new backup.
    rc = main(["--db", str(db_path)])  # dry-run on the already-clean DB
    assert rc == 0
    out = capsys.readouterr().out
    assert "nothing to drop" in out


def test_refuses_a_db_without_the_sentinel_table(tmp_path):
    """A --db that is not a relay store (no relay_reports) is refused loudly.

    Why this matters: a destructive tool pointed at the wrong file must fail, not
    silently operate. Missing the sentinel table means this is not a relay DB.
    """
    stray = tmp_path / "not-a-relay.sqlite3"
    conn = sqlite3.connect(stray)
    conn.execute("CREATE TABLE something (x INTEGER)")
    conn.commit()
    conn.close()

    with pytest.raises(SystemExit):
        main(["--db", str(stray)])


def test_refuses_to_overwrite_an_existing_backup(tmp_path):
    """--drop refuses when the backup path already exists (never clobber a safety copy).

    Why this matters: an accidental re-invocation with the same backup path must not
    destroy the copy taken on the first run. The tool fails before dropping anything.
    """
    db_path = _fresh_relay_db(tmp_path)
    _seed_legacy_skills(db_path)
    backup = tmp_path / "exists.sqlite3"
    backup.write_text("pretend prior backup")

    with pytest.raises(SystemExit):
        main(["--db", str(db_path), "--drop", "--backup", str(backup)])
    # The drop was refused BEFORE touching the tables, so they are still present.
    assert {"relay_project_skills", "relay_producer_skills"} <= _tables(db_path)

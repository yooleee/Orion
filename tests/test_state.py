# =============================================================================
# tests/test_state.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the state store's delta-marker behavior and history.
# Role in project: The marker logic is what makes a second run say "no new
#                  activity." If get/advance are wrong, reports duplicate or skip.
# =============================================================================

from orion.state import advance_state, get_last_reported, open_state, record_report


def test_first_run_has_no_marker(tmp_path):
    """A fresh store returns None for a project never reported.

    Why this matters: None is the signal for "report full history" on the first
    run; if a fresh project returned something else, the first report would be
    wrong.
    """
    conn = open_state(tmp_path / "state.sqlite3")
    assert get_last_reported(conn, "demo") is None


def test_advance_then_read_round_trips(tmp_path):
    """After advancing, the stored marker is read back exactly.

    Why this matters: this is the core of delta reporting — the next run must see
    precisely the commit the last run advanced to.
    """
    conn = open_state(tmp_path / "state.sqlite3")
    advance_state(conn, "demo", "abc123", "2026-06-13T12:00:00Z")
    assert get_last_reported(conn, "demo") == "abc123"


def test_advance_is_upsert(tmp_path):
    """A second advance updates (does not duplicate) the project's marker.

    Why this matters: a project is reported many times; the marker must move
    forward in place, not accumulate rows or raise on the existing primary key.
    """
    conn = open_state(tmp_path / "state.sqlite3")
    advance_state(conn, "demo", "abc123", "2026-06-13T12:00:00Z")
    advance_state(conn, "demo", "def456", "2026-06-13T13:00:00Z")
    assert get_last_reported(conn, "demo") == "def456"

    # Exactly one row for the project — the upsert updated rather than inserted.
    count = conn.execute(
        "SELECT COUNT(*) FROM project_state WHERE project = ?", ("demo",)
    ).fetchone()[0]
    assert count == 1


def test_record_report_persists_history(tmp_path):
    """A recorded report is stored with its redacted body and recipient list.

    Why this matters: the history is the audit trail; it must capture what was
    sent (already redacted) and to whom, for a future "show last report".
    """
    conn = open_state(tmp_path / "state.sqlite3")
    record_report(
        conn,
        "demo",
        "Shipped the config loader.",
        ["Alex", "Sam"],
        "2026-06-13T12:00:00Z",
    )
    row = conn.execute(
        "SELECT project, summary, recipients FROM report_history"
    ).fetchone()
    assert row[0] == "demo"
    assert row[1] == "Shipped the config loader."
    # recipients are stored as a JSON array string.
    assert "Alex" in row[2] and "Sam" in row[2]

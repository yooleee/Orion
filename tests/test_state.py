# =============================================================================
# tests/test_state.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the state store's delta-marker behavior and history.
# Role in project: The marker logic is what makes a second run say "no new
#                  activity." If get/advance are wrong, reports duplicate or skip.
# =============================================================================

from orion.state import (
    _BUSY_TIMEOUT_SECONDS,
    get_comment_watermark,
    get_last_report_time,
    get_marker,
    open_state,
    record_report,
    set_comment_watermark,
    set_marker,
)


def test_get_last_report_time(tmp_path):
    """Returns None until a report is recorded, then the latest sent_at.

    Why this matters: `orion status` reads this for staleness — None must mean
    "never reported", and a second report must move the time forward (MAX(sent_at)),
    while a different project stays None.
    """
    conn = open_state(tmp_path / "state.sqlite3")
    assert get_last_report_time(conn, "demo") is None
    record_report(conn, "demo", "first", ["Alex"], "2026-06-01T10:00:00+00:00")
    record_report(conn, "demo", "second", ["Alex"], "2026-06-02T10:00:00+00:00")
    assert get_last_report_time(conn, "demo") == "2026-06-02T10:00:00+00:00"
    assert get_last_report_time(conn, "other") is None


def test_first_run_has_no_marker(tmp_path):
    """A fresh store returns None for a (project, collector) never reported.

    Why this matters: None is the signal for "report full history" on the first
    run; if a fresh project returned something else, the first report would be
    wrong.
    """
    conn = open_state(tmp_path / "state.sqlite3")
    assert get_marker(conn, "demo", "git") is None


def test_open_state_sets_busy_timeout(tmp_path):
    """open_state opens the connection with a busy timeout, not the default.

    Why this matters: a pre-push hook on rapid successive commits can fire two
    `report` runs at once; both open the state DB to write a marker/history row.
    Without a busy timeout the later writer raises "database is locked" and that
    run's report is silently lost (the hook always exits 0). sqlite3.connect's
    `timeout` sets the connection's busy_timeout (ms), which we read back to
    confirm the fix is wired — the later writer now waits instead of failing.
    """
    conn = open_state(tmp_path / "state.sqlite3")
    busy_timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert busy_timeout_ms == int(_BUSY_TIMEOUT_SECONDS * 1000)


def test_set_then_get_round_trips(tmp_path):
    """After setting, the stored marker is read back exactly.

    Why this matters: this is the core of delta reporting — the next run must see
    precisely the marker the last run advanced to.
    """
    conn = open_state(tmp_path / "state.sqlite3")
    set_marker(conn, "demo", "git", "abc123", "2026-06-13T12:00:00Z")
    assert get_marker(conn, "demo", "git") == "abc123"


def test_set_marker_is_upsert(tmp_path):
    """A second set updates (does not duplicate) the (project, collector) marker.

    Why this matters: a signal is reported many times; its marker must move
    forward in place, not accumulate rows or raise on the existing primary key.
    """
    conn = open_state(tmp_path / "state.sqlite3")
    set_marker(conn, "demo", "git", "abc123", "2026-06-13T12:00:00Z")
    set_marker(conn, "demo", "git", "def456", "2026-06-13T13:00:00Z")
    assert get_marker(conn, "demo", "git") == "def456"

    # Exactly one row for this (project, collector) — the upsert updated.
    count = conn.execute(
        "SELECT COUNT(*) FROM collector_markers WHERE project = ? AND collector = ?",
        ("demo", "git"),
    ).fetchone()[0]
    assert count == 1


def test_markers_are_per_collector(tmp_path):
    """Two collectors on the same project track independent markers.

    Why this matters: the whole point of Phase 2's per-collector store is that
    advancing git must not disturb where tasks left off (or vice versa). If they
    shared a slot, reporting code activity would wrongly mark to-dos as reported.
    """
    conn = open_state(tmp_path / "state.sqlite3")
    set_marker(conn, "demo", "git", "abc123", "2026-06-13T12:00:00Z")
    set_marker(conn, "demo", "tasks", '["Ship it"]', "2026-06-13T12:00:00Z")

    assert get_marker(conn, "demo", "git") == "abc123"
    assert get_marker(conn, "demo", "tasks") == '["Ship it"]'

    # Advancing one leaves the other untouched.
    set_marker(conn, "demo", "git", "def456", "2026-06-13T13:00:00Z")
    assert get_marker(conn, "demo", "tasks") == '["Ship it"]'


def test_backfill_migrates_legacy_last_commit(tmp_path):
    """A pre-Phase-2 project_state row is migrated into collector_markers on open.

    Why this matters: upgrading a live state DB must NOT reset a project's git
    delta and re-send its whole history. We simulate a Phase-1 DB by writing a
    legacy project_state row directly, then assert a fresh open exposes it as the
    git marker via the new helper.
    """
    db = tmp_path / "state.sqlite3"
    conn = open_state(db)
    # Simulate a Phase-1 row that only the legacy table knows about.
    conn.execute(
        "INSERT INTO project_state (project, last_commit, last_reported) "
        "VALUES (?, ?, ?)",
        ("legacy", "oldsha", "2026-06-13T12:00:00Z"),
    )
    conn.commit()
    conn.close()

    # Reopening triggers the idempotent backfill.
    conn2 = open_state(db)
    assert get_marker(conn2, "legacy", "git") == "oldsha"


def test_backfill_does_not_clobber_advanced_marker(tmp_path):
    """Backfill never overwrites a git marker already advanced post-upgrade.

    Why this matters: INSERT OR IGNORE must leave a newer collector_markers value
    alone, or every reopen would drag git's marker back to the stale legacy sha.
    """
    db = tmp_path / "state.sqlite3"
    conn = open_state(db)
    conn.execute(
        "INSERT INTO project_state (project, last_commit, last_reported) "
        "VALUES (?, ?, ?)",
        ("legacy", "oldsha", "2026-06-13T12:00:00Z"),
    )
    conn.commit()
    # Advance past the legacy value under the new helper.
    set_marker(conn, "legacy", "git", "newsha", "2026-06-14T12:00:00Z")
    conn.close()

    conn2 = open_state(db)
    assert get_marker(conn2, "legacy", "git") == "newsha"


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


# --- C2 pull-back: the local comment-unread watermark --------------------------


def test_comment_watermark_defaults_to_zero(tmp_path):
    """A (project, relay) never pulled returns watermark 0.

    Why this matters: 0 is the "seen nothing" sentinel the first pull relies on —
    relay comment ids start at 1, so since_id=0 fetches the full history. If a fresh
    pair returned anything else, the first pull would silently skip early comments.
    """
    conn = open_state(tmp_path / "state.sqlite3")
    assert get_comment_watermark(conn, "demo", "https://relay.test/ingest") == 0


def test_comment_watermark_round_trips(tmp_path):
    """A set watermark is read back for the same (project, relay).

    Why this matters: the cursor only works if what we record is what the next pull
    reads — set the high-water mark, then get it back unchanged.
    """
    conn = open_state(tmp_path / "state.sqlite3")
    url = "https://relay.test/ingest"
    set_comment_watermark(conn, "demo", url, 42, "2026-06-19T12:00:00+00:00")
    assert get_comment_watermark(conn, "demo", url) == 42


def test_comment_watermark_is_upsert(tmp_path):
    """Setting the watermark twice updates the same row (advances, not duplicates).

    Why this matters: each successful pull advances the cursor in place. A second set
    must overwrite the first (one row per (project, relay)), so the watermark always
    reflects the latest pull rather than accumulating stale rows.
    """
    conn = open_state(tmp_path / "state.sqlite3")
    url = "https://relay.test/ingest"
    set_comment_watermark(conn, "demo", url, 5, "2026-06-19T12:00:00+00:00")
    set_comment_watermark(conn, "demo", url, 9, "2026-06-19T13:00:00+00:00")

    assert get_comment_watermark(conn, "demo", url) == 9
    # Exactly one row for the pair — the second set updated, not inserted.
    (count,) = conn.execute(
        "SELECT COUNT(*) FROM comment_watermark WHERE project = ? AND relay_url = ?",
        ("demo", url),
    ).fetchone()
    assert count == 1


def test_comment_watermark_is_scoped_per_project_and_relay(tmp_path):
    """Watermarks are independent across projects AND across relays for one project.

    Why this matters: the key is (project, relay_url), not project alone. Two projects
    must not share a cursor, and pointing the SAME project at a different relay must
    start fresh — ids from one relay's store are meaningless against another's, so a
    shared cursor would skip or re-show comments. We set three distinct pairs and
    confirm each reads back its own value.
    """
    conn = open_state(tmp_path / "state.sqlite3")
    relay_a = "https://a.relay.test/ingest"
    relay_b = "https://b.relay.test/ingest"

    set_comment_watermark(conn, "demo", relay_a, 10, "2026-06-19T12:00:00+00:00")
    set_comment_watermark(conn, "other", relay_a, 20, "2026-06-19T12:00:00+00:00")
    set_comment_watermark(conn, "demo", relay_b, 30, "2026-06-19T12:00:00+00:00")

    assert get_comment_watermark(conn, "demo", relay_a) == 10   # this project+relay
    assert get_comment_watermark(conn, "other", relay_a) == 20  # different project
    assert get_comment_watermark(conn, "demo", relay_b) == 30   # same project, other relay

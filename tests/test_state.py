# =============================================================================
# tests/test_state.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the state store's delta-marker behavior and history.
# Role in project: The marker logic is what makes a second run say "no new
#                  activity." If get/advance are wrong, reports duplicate or skip.
# =============================================================================

from orion.state import (
    _BUSY_TIMEOUT_SECONDS,
    get_cache,
    get_discussion_watermark,
    get_last_checklist_push,
    get_last_report_time,
    get_marker,
    open_state,
    record_checklist_push,
    record_report,
    set_cache,
    set_discussion_watermark,
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


def test_get_last_checklist_push(tmp_path):
    """Returns None until a push is recorded, then the LATEST (pushed_at, content_hash).

    Why this matters: `checklist-push --all --due` (E1.3) reads this to gate a scheduled
    push on both cadence (pushed_at) and content change (content_hash). None must mean
    "never pushed" (first-run due), and a second push must supersede the first — proven
    by recording two rows and asserting the reader returns the SECOND pair, not the first
    and not a max-timestamp mix. A different project stays None (per-project isolation).
    """
    conn = open_state(tmp_path / "state.sqlite3")
    assert get_last_checklist_push(conn, "demo") is None

    record_checklist_push(conn, "demo", "hash-one", "2026-07-01T10:00:00+00:00")
    record_checklist_push(conn, "demo", "hash-two", "2026-07-02T10:00:00+00:00")
    # The latest row wins (id DESC): its timestamp AND its hash, as a correlated pair.
    assert get_last_checklist_push(conn, "demo") == (
        "2026-07-02T10:00:00+00:00",
        "hash-two",
    )
    assert get_last_checklist_push(conn, "other") is None


def test_get_last_checklist_push_uses_insertion_order_not_timestamp(tmp_path):
    """The latest push is the last one RECORDED, even if its timestamp is not the max.

    Why this matters: get_last_checklist_push orders by id (insertion order), not by
    MAX(pushed_at), so it stays correct if two pushes ever share a timestamp or a clock
    correction makes a later push carry an earlier stamp. We record a newer push with a
    deliberately EARLIER timestamp and assert its (earlier) pair is returned — the read
    reflects "what we pushed most recently," which is what the change-gate compares.
    """
    conn = open_state(tmp_path / "state.sqlite3")
    record_checklist_push(conn, "demo", "hash-old", "2026-07-02T10:00:00+00:00")
    # A later push whose timestamp is EARLIER than the previous row's.
    record_checklist_push(conn, "demo", "hash-new", "2026-07-01T09:00:00+00:00")
    assert get_last_checklist_push(conn, "demo") == (
        "2026-07-01T09:00:00+00:00",
        "hash-new",
    )


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


def test_open_state_creates_no_legacy_project_state_table(tmp_path):
    """A fresh state DB has no `project_state` table (KI-8: the vestigial table is gone).

    Why this matters: `project_state` existed only to source a one-time Phase-1→Phase-2
    git-marker backfill, which has long since run on every live DB. Dropping it means a
    fresh open must not recreate it — this pins that the schema no longer carries the
    dead table (and that the removed backfill left no reference behind).
    """
    conn = open_state(tmp_path / "state.sqlite3")
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert "project_state" not in tables
    # The live marker table is still present (sanity: we dropped the right thing).
    assert "collector_markers" in tables


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


# --- E2 Inc 5: the local discussion-unread watermark ---------------------------
# The supervisor-interaction loop's cursor — the live read-cursor since KI-28 Stage 2
# retired the comment pull-back. A SEPARATE table from the (now-orphaned) comment_watermark,
# so we re-pin its defaults, round-trip, upsert, and per-(project, relay) scoping.


def test_discussion_watermark_defaults_to_zero(tmp_path):
    """A (project, relay) never pulled returns watermark 0 (the first-pull sentinel)."""
    conn = open_state(tmp_path / "state.sqlite3")
    assert get_discussion_watermark(conn, "demo", "https://relay.test/ingest") == 0


def test_discussion_watermark_round_trips_and_upserts(tmp_path):
    """A set watermark reads back; a second set advances the same row, never duplicates."""
    conn = open_state(tmp_path / "state.sqlite3")
    url = "https://relay.test/ingest"
    set_discussion_watermark(conn, "demo", url, 5, "2026-06-28T12:00:00+00:00")
    set_discussion_watermark(conn, "demo", url, 9, "2026-06-28T13:00:00+00:00")
    assert get_discussion_watermark(conn, "demo", url) == 9
    (count,) = conn.execute(
        "SELECT COUNT(*) FROM discussion_watermark WHERE project = ? AND relay_url = ?",
        ("demo", url),
    ).fetchone()
    assert count == 1



# --- collector_cache: the content-hash cache (E2 Inc 4 slice 4b) ------------------


def test_cache_round_trips_hash_and_value(tmp_path):
    """After set, get returns the stored (content_hash, value) for that key.

    Why this matters: the disciplines collector compares the stored hash to the
    current doc's hash to decide whether to skip the LLM. Both fields must survive.
    """
    conn = open_state(tmp_path / "state.sqlite3")
    assert get_cache(conn, "demo", "disciplines", "/abs/CLAUDE.md") is None
    set_cache(
        conn, "demo", "disciplines", "/abs/CLAUDE.md", "hash1", '[{"title":"X"}]',
        "2026-06-27T10:00:00+00:00",
    )
    assert get_cache(conn, "demo", "disciplines", "/abs/CLAUDE.md") == ("hash1", '[{"title":"X"}]')


def test_cache_upsert_overwrites_same_key(tmp_path):
    """Re-setting one key replaces its row (bounded: one row per key), not appends.

    Why this matters: an edited doc must overwrite its prior cache entry so the cache
    stays bounded and never serves a stale value for the same input identity.
    """
    conn = open_state(tmp_path / "state.sqlite3")
    set_cache(conn, "demo", "disciplines", "k", "h1", "v1", "2026-06-27T10:00:00+00:00")
    set_cache(conn, "demo", "disciplines", "k", "h2", "v2", "2026-06-27T11:00:00+00:00")
    assert get_cache(conn, "demo", "disciplines", "k") == ("h2", "v2")


def test_cache_is_keyed_by_project_collector_and_key(tmp_path):
    """Entries are independent across project, collector, and key.

    Why this matters: one project's (or collector's) cache must never shadow another's
    — the composite primary key keeps them separate.
    """
    conn = open_state(tmp_path / "state.sqlite3")
    set_cache(conn, "demo", "disciplines", "k", "h", "demo-val", "2026-06-27T10:00:00+00:00")
    set_cache(conn, "other", "disciplines", "k", "h", "other-val", "2026-06-27T10:00:00+00:00")
    assert get_cache(conn, "demo", "disciplines", "k") == ("h", "demo-val")
    assert get_cache(conn, "other", "disciplines", "k") == ("h", "other-val")

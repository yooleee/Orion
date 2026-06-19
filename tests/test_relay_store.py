# =============================================================================
# tests/test_relay_store.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the relay's own SQLite store — idempotent open,
#                  ingest, and the read queries (list/history/get) the dashboard
#                  renders from.
# Role in project: The relay store is the hosted half's persistence and is
#                  deliberately independent of orion.state. These tests pin that a
#                  pushed blob round-trips faithfully (sections/participants as
#                  structures, not strings), that history/list order correctly, and
#                  that a missing id is a clean None (→ a 404 later), so CP6/CP7 can
#                  build on a trustworthy store.
# Test approach: blob dicts are hand-built to the portable contract's shape (NOT
#                via orion.report) so the store test mirrors the store's own
#                independence from orion — CP6 proves the real end-to-end contract.
# =============================================================================

from relay.store import (
    add_comment,
    comments_for,
    comments_for_project,
    get,
    history,
    ingest,
    list_projects,
    open_relay_store,
)


def _blob(project="demo", *, generated_at="2026-06-18T00:00:00+00:00", sections=None):
    """Build a portable-blob dict in the shape serialize_blob produces.

    Args:
        project: The project name for the blob.
        generated_at: The blob's build timestamp (varied to test ordering).
        sections: The (title, body) sections as a list of 2-element lists; defaults
            to one section when None.

    Why:
        Centralizes blob construction so each test varies only the field it checks
        (DRY). Built as a plain dict — not through orion.report — to keep this test
        independent of the local package, exactly as the store itself is.
    """
    if sections is None:
        sections = [["Code activity", "Shipped the seam."]]
    return {
        "project": project,
        "participants": ["Alex", "Sam"],
        "share_level": "high_level",
        "lane": "raw",
        "body": "Shipped the seam.",
        "source_marker": "",
        "generated_at": generated_at,
        "orion_version": "0.0.0",
        "sections": sections,
    }


def test_open_is_idempotent(tmp_path):
    """Opening the same store twice succeeds and preserves data.

    Why this matters: open_relay_store runs the schema on every open (IF NOT
    EXISTS). Re-opening must not error or wipe existing rows — the server may open
    the store on every start, so a second open has to be a safe no-op over real
    data.
    """
    db = tmp_path / "relay.sqlite3"
    conn = open_relay_store(db)
    new_id = ingest(conn, _blob(), "2026-06-18T00:00:01+00:00")
    conn.close()

    # Re-open the same file: schema re-applied, and the earlier row still there.
    conn2 = open_relay_store(db)
    assert get(conn2, new_id) is not None


def test_ingest_then_get_round_trips_all_fields(tmp_path):
    """A blob ingested then fetched by id returns every field, decoded.

    Why this matters: this is the store's core promise — what the dashboard renders
    must equal what was pushed. We especially pin that sections and participants
    come back as STRUCTURES (lists), not the JSON strings they are stored as, so
    the renderer gets real (title, body) pairs and names.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    blob = _blob(sections=[["Code activity", "Did X."], ["Notes", "Thinking Y."]])

    new_id = ingest(conn, blob, "2026-06-18T01:02:03+00:00")
    report = get(conn, new_id)

    assert report["id"] == new_id
    assert report["project"] == "demo"
    assert report["body"] == "Shipped the seam."
    assert report["share_level"] == "high_level"
    assert report["lane"] == "raw"
    assert report["generated_at"] == "2026-06-18T00:00:00+00:00"
    assert report["orion_version"] == "0.0.0"
    assert report["ingested_at"] == "2026-06-18T01:02:03+00:00"
    # The JSON columns decode back to structures, not strings.
    assert report["participants"] == ["Alex", "Sam"]
    assert report["sections"] == [["Code activity", "Did X."], ["Notes", "Thinking Y."]]


def test_get_missing_id_returns_none(tmp_path):
    """Fetching an id that does not exist returns None, not an error.

    Why this matters: a stale or hand-typed /report/<id> link is an expected case.
    Returning None lets the server render a clean 404 instead of crashing — the
    behavior CP7's 404 test depends on.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    assert get(conn, 999) is None


def test_history_returns_a_projects_reports_newest_first(tmp_path):
    """history() returns only the named project's reports, ordered newest first.

    Why this matters: the per-project history view shows the most recent update at
    the top and must not bleed in another project's reports. We ingest two reports
    for 'demo' (different timestamps) and one for 'other', then check demo's history
    is exactly its two, newest first.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    ingest(conn, _blob("demo", generated_at="2026-06-18T08:00:00+00:00"), "2026-06-18T08:00:01+00:00")
    ingest(conn, _blob("demo", generated_at="2026-06-18T09:00:00+00:00"), "2026-06-18T09:00:01+00:00")
    ingest(conn, _blob("other", generated_at="2026-06-18T10:00:00+00:00"), "2026-06-18T10:00:01+00:00")

    demo_history = history(conn, "demo")

    assert [r["project"] for r in demo_history] == ["demo", "demo"]  # no 'other'
    # Newest generated_at first.
    assert demo_history[0]["generated_at"] == "2026-06-18T09:00:00+00:00"
    assert demo_history[1]["generated_at"] == "2026-06-18T08:00:00+00:00"


def test_history_for_unknown_project_is_empty(tmp_path):
    """history() for a project with no reports is an empty list.

    Why this matters: the dashboard may be asked for a project that has never
    pushed; an empty list (not an error) lets the view show a clean empty state.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    assert history(conn, "never-seen") == []


def test_list_projects_counts_and_orders_by_latest(tmp_path):
    """list_projects() summarizes each project with a count and its latest time,
    most-recently-active first.

    Why this matters: this backs the dashboard index. We ingest two reports for
    'alpha' and one (more recent) for 'beta', and check the counts are right and
    that 'beta' — active most recently — sorts ahead of 'alpha'.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    ingest(conn, _blob("alpha", generated_at="2026-06-18T08:00:00+00:00"), "2026-06-18T08:00:01+00:00")
    ingest(conn, _blob("alpha", generated_at="2026-06-18T09:00:00+00:00"), "2026-06-18T09:00:01+00:00")
    ingest(conn, _blob("beta", generated_at="2026-06-18T12:00:00+00:00"), "2026-06-18T12:00:01+00:00")

    projects = list_projects(conn)

    assert [p["project"] for p in projects] == ["beta", "alpha"]  # beta most recent
    by_name = {p["project"]: p for p in projects}
    assert by_name["alpha"]["report_count"] == 2
    assert by_name["beta"]["report_count"] == 1
    assert by_name["alpha"]["latest_generated_at"] == "2026-06-18T09:00:00+00:00"


# --- C2: report comments (append-only, flat) ----------------------------------


def test_add_comment_then_fetch_round_trips(tmp_path):
    """A comment added to a report comes back from comments_for with every field.

    Why this matters: this is the comment store's core promise — what the dashboard
    renders must equal what was posted. We ingest a real report, attach one comment,
    and confirm all four user/clock fields survive the round-trip (author, body,
    created_at, and the report_id linkage), since the renderer reads each of them.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    report_id = ingest(conn, _blob(), "2026-06-18T00:00:01+00:00")

    comment_id = add_comment(
        conn, report_id, "Alex", "Looks great, ship it.", "2026-06-18T10:00:00+00:00"
    )

    comments = comments_for(conn, report_id)
    assert len(comments) == 1
    only = comments[0]
    assert only["id"] == comment_id
    assert only["report_id"] == report_id
    assert only["author"] == "Alex"
    assert only["body"] == "Looks great, ship it."
    assert only["created_at"] == "2026-06-18T10:00:00+00:00"


def test_comments_are_oldest_first(tmp_path):
    """comments_for returns a report's comments in chronological (insertion) order.

    Why this matters: an append-only thread must read top-to-bottom in the order it
    was written. We add three comments and assert they come back in insertion order
    (id ASC), which the renderer relies on to lay the thread out correctly.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    report_id = ingest(conn, _blob(), "2026-06-18T00:00:01+00:00")

    add_comment(conn, report_id, "Alex", "first", "2026-06-18T10:00:00+00:00")
    add_comment(conn, report_id, "Sam", "second", "2026-06-18T11:00:00+00:00")
    add_comment(conn, report_id, "", "third (anonymous)", "2026-06-18T12:00:00+00:00")

    bodies = [c["body"] for c in comments_for(conn, report_id)]
    assert bodies == ["first", "second", "third (anonymous)"]


def test_comments_are_scoped_to_their_report(tmp_path):
    """comments_for(report) returns only that report's comments, not another's.

    Why this matters: comments hang off a specific report_id; a comment on report A
    must never leak into report B's thread. We ingest two reports, comment on each,
    and confirm each fetch sees only its own.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    report_a = ingest(conn, _blob("alpha"), "2026-06-18T00:00:01+00:00")
    report_b = ingest(conn, _blob("beta"), "2026-06-18T00:00:02+00:00")

    add_comment(conn, report_a, "Alex", "on A", "2026-06-18T10:00:00+00:00")
    add_comment(conn, report_b, "Sam", "on B", "2026-06-18T11:00:00+00:00")

    assert [c["body"] for c in comments_for(conn, report_a)] == ["on A"]
    assert [c["body"] for c in comments_for(conn, report_b)] == ["on B"]


def test_comments_for_report_with_none_is_empty(tmp_path):
    """A report with no comments returns an empty list, not None.

    Why this matters: the render layer shows a clean "No comments yet" empty state;
    returning [] (not None) lets it iterate without a null check, matching how
    history() handles a project with no reports.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    report_id = ingest(conn, _blob(), "2026-06-18T00:00:01+00:00")
    assert comments_for(conn, report_id) == []


# --- C2 pull-back: comments_for_project (by-project pull + since_id cursor) ----


def test_comments_for_project_spans_reports_and_scopes_to_project(tmp_path):
    """comments_for_project gathers a project's comments across reports, excluding others.

    Why this matters: the pull-back fetches by PROJECT, not by a report id the client
    doesn't hold. A project can have several reports each with comments, and a comment
    on a DIFFERENT project must never bleed in. We ingest two reports for 'alpha' and
    one for 'beta', comment on each, and confirm alpha's pull returns exactly its two
    comments (across both its reports), oldest first.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    alpha_1 = ingest(conn, _blob("alpha"), "2026-06-18T00:00:01+00:00")
    alpha_2 = ingest(conn, _blob("alpha"), "2026-06-18T00:00:02+00:00")
    beta_1 = ingest(conn, _blob("beta"), "2026-06-18T00:00:03+00:00")

    add_comment(conn, alpha_1, "Alex", "on alpha report 1", "2026-06-18T10:00:00+00:00")
    add_comment(conn, beta_1, "Sam", "on beta", "2026-06-18T10:30:00+00:00")
    add_comment(conn, alpha_2, "Jo", "on alpha report 2", "2026-06-18T11:00:00+00:00")

    comments = comments_for_project(conn, "alpha")
    # Both alpha comments, across its two reports, in ascending-id (chronological) order.
    assert [c["body"] for c in comments] == ["on alpha report 1", "on alpha report 2"]
    # The report_id linkage is preserved (the comments came from different reports).
    assert [c["report_id"] for c in comments] == [alpha_1, alpha_2]


def test_comments_for_project_since_id_returns_only_newer(tmp_path):
    """since_id returns only comments with a strictly greater id (the unread cursor).

    Why this matters: this is the watermark mechanism. After a client has seen up to
    comment id N, the next pull passes since_id=N and must get back only what came
    after — never the already-seen ones, and never missing a new one. We add three
    comments, then pull with since_id set to the second's id and expect only the third.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    report_id = ingest(conn, _blob("demo"), "2026-06-18T00:00:01+00:00")
    c1 = add_comment(conn, report_id, "Alex", "first", "2026-06-18T10:00:00+00:00")
    c2 = add_comment(conn, report_id, "Sam", "second", "2026-06-18T11:00:00+00:00")
    c3 = add_comment(conn, report_id, "Jo", "third", "2026-06-18T12:00:00+00:00")

    # since_id defaults to 0 → everything.
    assert [c["id"] for c in comments_for_project(conn, "demo")] == [c1, c2, c3]
    # since_id = c2 → only the strictly-newer c3.
    newer = comments_for_project(conn, "demo", since_id=c2)
    assert [c["body"] for c in newer] == ["third"]
    assert newer[0]["id"] == c3


def test_comments_for_project_unknown_or_caught_up_is_empty(tmp_path):
    """An unknown project, or one with nothing newer than since_id, returns [].

    Why this matters: "no new replies" and "no such project" both map to a clean empty
    list (not None, not an error), which the endpoint turns into a 200 empty response
    and the client reads as "nothing new". We check both: a never-seen project, and a
    real project pulled with since_id at its newest comment.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    report_id = ingest(conn, _blob("demo"), "2026-06-18T00:00:01+00:00")
    last = add_comment(conn, report_id, "Alex", "only", "2026-06-18T10:00:00+00:00")

    assert comments_for_project(conn, "never-seen") == []
    assert comments_for_project(conn, "demo", since_id=last) == []

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

import sqlite3
from datetime import date

import pytest

from relay.store import (
    add_comment,
    add_user,
    bump_session_version,
    comments_for,
    comments_for_project,
    get,
    get_checklist,
    get_user_by_id,
    get_user_by_name,
    get_user_by_verifier,
    history,
    ingest,
    latest_report_per_project,
    list_projects,
    list_users,
    observed_history,
    open_relay_store,
    projects_for_user,
    record_admin_audit,
    record_observations,
    revoke_user,
    update_last_login,
    upsert_checklist,
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


def test_latest_report_per_project_carries_count_and_latest_body(tmp_path):
    """latest_report_per_project() returns each project's newest report id+body, ordered.

    Why this matters: this backs the portfolio HOME. Unlike list_projects (count +
    timestamp only), it must also surface the LATEST report's id and body so the home can
    show a one-line headline and link to that report. We ingest two reports for 'alpha'
    (varying the body so we can tell which one is returned) and one more-recent for 'beta',
    then check the counts, the project order (most-recent first), and that 'alpha' carries
    its SECOND report's body — the newest by generated_at.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    a1 = _blob("alpha", generated_at="2026-06-18T08:00:00+00:00")
    a1["body"] = "First alpha update."
    ingest(conn, a1, "2026-06-18T08:00:01+00:00")
    a2 = _blob("alpha", generated_at="2026-06-18T09:00:00+00:00")
    a2["body"] = "Second alpha update."
    latest_alpha_id = ingest(conn, a2, "2026-06-18T09:00:01+00:00")
    b1 = _blob("beta", generated_at="2026-06-18T12:00:00+00:00")
    b1["body"] = "Beta update."
    ingest(conn, b1, "2026-06-18T12:00:01+00:00")

    rows = latest_report_per_project(conn)

    assert [r["project"] for r in rows] == ["beta", "alpha"]  # beta most recent
    by_name = {r["project"]: r for r in rows}
    assert by_name["alpha"]["report_count"] == 2
    assert by_name["beta"]["report_count"] == 1
    # alpha resolves to its NEWEST report (by generated_at) — id and body both.
    assert by_name["alpha"]["latest_report_id"] == latest_alpha_id
    assert by_name["alpha"]["latest_body"] == "Second alpha update."
    assert by_name["alpha"]["latest_generated_at"] == "2026-06-18T09:00:00+00:00"


def test_latest_report_per_project_matches_history_when_ingest_order_differs(tmp_path):
    """The "latest" report agrees with history()[0] even if a later-ingested report is OLDER.

    Why this matters: "newest" must mean newest by generated_at (tie-broken by id), the
    SAME rule history() uses — not simply the last-ingested row. Here a backfilled report
    (ingested second, but with an EARLIER generated_at) must NOT win. If the query used
    MAX(id) it would pick the backfill and disagree with the project page's history()[0];
    this pins that they stay consistent.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    # Ingested FIRST but generated LATER — this is the true newest.
    newer = _blob("demo", generated_at="2026-06-18T10:00:00+00:00")
    newer["body"] = "The newest update."
    ingest(conn, newer, "2026-06-18T10:00:01+00:00")
    # Ingested SECOND but generated EARLIER (a backfill) — must NOT be chosen as latest.
    older = _blob("demo", generated_at="2026-06-18T07:00:00+00:00")
    older["body"] = "An older, backfilled update."
    ingest(conn, older, "2026-06-18T11:00:00+00:00")

    rows = latest_report_per_project(conn)
    assert len(rows) == 1
    # Same report history() would return first — consistency between the two views.
    assert rows[0]["latest_body"] == history(conn, "demo")[0]["body"]
    assert rows[0]["latest_body"] == "The newest update."


def test_latest_report_per_project_empty_store_is_empty(tmp_path):
    """An empty store yields an empty list (a fresh relay's portfolio shows nothing).

    Why this matters: a brand-new relay with no pushes must render a clean empty-state,
    not error — so the helper returns [] rather than raising.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    assert latest_report_per_project(conn) == []


# --- E2 Inc 2: the live checklist (current state, one row per project) ----------
# upsert_checklist REPLACES (not appends), get_checklist decodes back to dicts, and
# latest_report_per_project carries precomputed done/total counts for the card badge.


def _items(*pairs):
    """Build a checklist as a list of {text, done} dicts.

    Why: the checklist's wire/stored shape is named objects; a tiny helper keeps each
    test to the items it cares about. Each arg is a (text, done) tuple.
    """
    return [{"text": text, "done": done} for text, done in pairs]


def test_upsert_checklist_replaces_not_appends(tmp_path):
    """A second upsert overwrites the project's checklist rather than adding a row.

    Why this matters: the live checklist is CURRENT STATE — one row per project. If
    upsert appended, get_checklist would return stale items, and the badge would
    double-count. We upsert twice and assert only the second push survives.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    upsert_checklist(conn, "demo", _items(("Old item", True)), "2026-06-25T00:00:00+00:00")
    upsert_checklist(
        conn,
        "demo",
        _items(("New A", False), ("New B", True)),
        "2026-06-25T01:00:00+00:00",
    )

    assert get_checklist(conn, "demo") == _items(("New A", False), ("New B", True))


def test_get_checklist_missing_project_returns_none(tmp_path):
    """A project with no checklist row returns None, not an error or empty list.

    Why this matters: None means "this project never had a checklist" (feature off),
    which the renderer reads as "omit the block". It must be distinct from [] below.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    assert get_checklist(conn, "demo") is None


def test_get_checklist_empty_list_is_distinct_from_none(tmp_path):
    """An upserted empty checklist returns [] — distinct from a missing project's None.

    Why this matters: an enabled-but-empty checklist ([]) legitimately clears a
    project's prior list. The store must preserve the [] vs None distinction so the
    renderer can tell "checklist enabled, no items" from "no checklist at all".
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    upsert_checklist(conn, "demo", [], "2026-06-25T00:00:00+00:00")
    assert get_checklist(conn, "demo") == []


def test_latest_report_per_project_carries_checklist_counts(tmp_path):
    """The portfolio overview surfaces precomputed done/total per project.

    Why this matters: the portfolio card shows an "X/Y done" badge. The store
    precomputes the counts (decoding the JSON once) so the renderer just presents
    them. A project with a checklist carries real counts; a project without one
    carries None for both, which the renderer reads as "omit the badge".
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    # 'alpha' has reports AND a checklist (2 of 3 done); 'beta' has reports only.
    ingest(conn, _blob("alpha", generated_at="2026-06-25T09:00:00+00:00"), "2026-06-25T09:00:01+00:00")
    upsert_checklist(
        conn,
        "alpha",
        _items(("Done one", True), ("Done two", True), ("Still open", False)),
        "2026-06-25T09:00:02+00:00",
    )
    ingest(conn, _blob("beta", generated_at="2026-06-25T08:00:00+00:00"), "2026-06-25T08:00:01+00:00")

    by_name = {r["project"]: r for r in latest_report_per_project(conn)}

    assert by_name["alpha"]["checklist_done"] == 2
    assert by_name["alpha"]["checklist_total"] == 3
    # No checklist row for beta → both None (badge omitted).
    assert by_name["beta"]["checklist_done"] is None
    assert by_name["beta"]["checklist_total"] is None


def test_latest_report_per_project_counts_at_risk_when_today_given(tmp_path):
    """With a reference date, the portfolio row carries the overdue/due-soon count (E2 Inc 3).

    Why this matters: the at-risk badge needs a precomputed count, derived against "today"
    in the display zone. The store reuses its single items decode to count alongside
    done/total, so the badge and the per-item render share one definition of "at risk".
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    upsert_checklist(
        conn,
        "demo",
        [
            {"text": "Overdue", "done": False, "due_date": "2026-06-20"},
            {"text": "Due soon", "done": False, "due_date": "2026-06-28"},
            {"text": "Far off", "done": False, "due_date": "2026-12-01"},
            {"text": "Done past", "done": True, "due_date": "2026-06-01"},
        ],
        "2026-06-26T00:00:00+00:00",
    )

    row = {r["project"]: r for r in latest_report_per_project(conn, today=date(2026, 6, 26))}["demo"]

    # Overdue + due-soon = 2; the far-future and the done item are not at risk.
    assert row["checklist_at_risk"] == 2


def test_latest_report_per_project_nearest_milestone_when_today_given(tmp_path):
    """The portfolio row carries the soonest-due milestone group as a {group, nearest_due} hint.

    Why this matters: the home's "next milestone" line needs one precomputed group + date.
    Across two milestones, the one with the earliest OPEN deadline wins — here "Applications"
    (Jun 28) beats "Chores" (Aug 01) — and a done item's earlier date does not pull it in.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    upsert_checklist(
        conn,
        "demo",
        [
            {"text": "App A", "done": True, "due_date": "2026-06-10", "group": "Applications"},
            {"text": "App B", "done": False, "due_date": "2026-06-28", "group": "Applications"},
            {"text": "Chore", "done": False, "due_date": "2026-08-01", "group": "Chores"},
        ],
        "2026-06-26T00:00:00+00:00",
    )

    row = {r["project"]: r for r in latest_report_per_project(conn, today=date(2026, 6, 26))}["demo"]

    assert row["nearest_milestone"] == {"group": "Applications", "nearest_due": "2026-06-28"}


def test_latest_report_per_project_nearest_milestone_none_without_today(tmp_path):
    """Without a reference date (or no grouped+dated item), the hint is None.

    Why this matters: `today` gates the derivation like the other forward fields, and a
    checklist with no milestone (ungrouped) or no open deadline yields None so the card
    simply omits the line.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    upsert_checklist(
        conn, "demo", _items(("Open", False)), "2026-06-26T00:00:00+00:00"
    )

    no_today = {r["project"]: r for r in latest_report_per_project(conn)}["demo"]
    assert no_today["nearest_milestone"] is None

    # Even WITH today, an ungrouped checklist has no milestone → still None.
    with_today = {r["project"]: r for r in latest_report_per_project(conn, today=date(2026, 6, 26))}["demo"]
    assert with_today["nearest_milestone"] is None


def test_latest_report_per_project_at_risk_is_none_without_today(tmp_path):
    """Without a reference date, at-risk derivation is skipped → checklist_at_risk is None.

    Why this matters: `today` is the seam for the derivation. A caller that does not pass it
    (or a test of the existing done/total behavior) must be unaffected — the field is None,
    which the renderer reads as "omit the badge", not "0 at risk".
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    upsert_checklist(
        conn, "demo", _items(("Open", False)), "2026-06-26T00:00:00+00:00"
    )

    row = {r["project"]: r for r in latest_report_per_project(conn)}["demo"]

    assert row["checklist_at_risk"] is None


def test_latest_report_per_project_includes_checklist_only_projects(tmp_path):
    """A project with a live checklist but ZERO reports still appears on the portfolio.

    Why this matters: this is the whole point of the fix. A dashboard-only project (a
    pushed checklist, no git, never report-ed — exactly the applications tracker) used
    to vanish from the home because the query ran FROM relay_reports, so family had no
    card to click. The row set is now project-driven (report OR checklist), so the
    checklist-only project shows up with report_count 0, None for every latest_* field,
    a populated checklist_updated_at to use as its last-activity time, and real
    done/total counts for the badge.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    upsert_checklist(
        conn,
        "applications",
        _items(("Applied to X", True), ("Heard back from Y", False)),
        "2026-06-25T10:00:00+00:00",
    )

    by_name = {r["project"]: r for r in latest_report_per_project(conn)}

    assert "applications" in by_name
    card = by_name["applications"]
    assert card["report_count"] == 0
    assert card["latest_report_id"] is None
    assert card["latest_body"] is None
    assert card["latest_generated_at"] is None
    assert card["checklist_updated_at"] == "2026-06-25T10:00:00+00:00"
    assert card["checklist_done"] == 1
    assert card["checklist_total"] == 2


def test_latest_report_per_project_interleaves_checklist_only_by_recency(tmp_path):
    """Checklist-only and report-bearing projects sort together by last activity.

    Why this matters: the home orders freshest-first across BOTH kinds. The order key is
    COALESCE(latest_generated_at, checklist_updated_at), so a checklist-only project with
    a fresher updated_at must outrank an older report, and an older checklist must fall
    below a newer report. We place a report between two checklist-only timestamps and
    assert the interleaving. A report+checklist project appears exactly once (UNION
    dedupe), keyed by its report time.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    # Checklist-only, freshest.
    upsert_checklist(conn, "fresh_list", _items(("a", False)), "2026-06-25T12:00:00+00:00")
    # Report-only, middle.
    ingest(conn, _blob("mid_report", generated_at="2026-06-25T11:00:00+00:00"), "2026-06-25T11:00:01+00:00")
    # Checklist-only, oldest.
    upsert_checklist(conn, "old_list", _items(("b", True)), "2026-06-25T10:00:00+00:00")
    # Report+checklist on one project — must appear ONCE, keyed by its report time.
    ingest(conn, _blob("both", generated_at="2026-06-25T09:00:00+00:00"), "2026-06-25T09:00:01+00:00")
    upsert_checklist(conn, "both", _items(("c", False)), "2026-06-25T08:00:00+00:00")

    rows = latest_report_per_project(conn)
    assert [r["project"] for r in rows] == ["fresh_list", "mid_report", "old_list", "both"]
    # 'both' is deduped to a single row (UNION, not UNION ALL).
    assert [r["project"] for r in rows].count("both") == 1


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


# --- Multi-party access: users, scope, revocation, audit (Increment 1) ---------
# These pin the per-user identity store the dashboard's auth/authZ build on: that a
# user + scope round-trip, that uniqueness is enforced, that the stateless-revocation
# levers (active + session_version) behave, and that a verifier never leaks via list.


def _store(tmp_path):
    """Open a fresh relay store (DRY across the user-store tests)."""
    return open_relay_store(tmp_path / "relay.sqlite3")


def test_add_user_round_trips_with_defaults_and_scope(tmp_path):
    """A provisioned user comes back with its fields, default flags, and project scope.

    Why this matters: this is the core identity record auth resolves on every login
    and request; the active/session_version DEFAULTS (1/1) and the scope rows must be
    exactly what add_user wrote.
    """
    conn = _store(tmp_path)
    uid = add_user(
        conn, "Alex", "verifier-alex", "viewer",
        ["orion", "incubator"], "admin-token", "2026-06-24T00:00:00+00:00",
    )
    row = get_user_by_id(conn, uid)
    assert row["name"] == "Alex"
    assert row["role"] == "viewer"
    assert row["active"] == 1            # default: active
    assert row["session_version"] == 1   # default: first session generation
    assert row["last_login_at"] is None  # not logged in yet
    # Scope round-trips, sorted.
    assert projects_for_user(conn, uid) == ["incubator", "orion"]


def test_get_user_by_verifier_and_name_resolve_same_row(tmp_path):
    """Login (by verifier) and CLI (by name) resolve to the same user.

    Why this matters: auth looks up by verifier; admin ops look up by name. Both must
    hit the one row so the two surfaces never disagree about who a user is.
    """
    conn = _store(tmp_path)
    uid = add_user(conn, "Sam", "verifier-sam", "viewer", [], "admin-token", "2026-06-24T00:00:00+00:00")
    assert get_user_by_verifier(conn, "verifier-sam")["id"] == uid
    assert get_user_by_name(conn, "Sam")["id"] == uid


def test_lookups_miss_return_none(tmp_path):
    """Unknown verifier/id/name are a clean None, never an error.

    Why this matters: a wrong key (no such verifier) or a dead session (deleted id)
    must degrade to "not authenticated", which the server keys off a None.
    """
    conn = _store(tmp_path)
    assert get_user_by_verifier(conn, "nope") is None
    assert get_user_by_id(conn, 999) is None
    assert get_user_by_name(conn, "ghost") is None


def test_duplicate_name_is_rejected(tmp_path):
    """A duplicate user name raises (UNIQUE), preventing an ambiguous second account.

    Why this matters: name is the admin's handle for revoke/list; two "Alex"es would
    make those ops ambiguous, so the store refuses it loudly.
    """
    conn = _store(tmp_path)
    add_user(conn, "Alex", "verifier-1", "viewer", [], "admin-token", "2026-06-24T00:00:00+00:00")
    with pytest.raises(sqlite3.IntegrityError):
        add_user(conn, "Alex", "verifier-2", "viewer", [], "admin-token", "2026-06-24T00:00:00+00:00")


def test_duplicate_verifier_is_rejected(tmp_path):
    """The same credential verifier cannot back two users (UNIQUE).

    Why this matters: a verifier is the login key's fingerprint; sharing it across
    users would make a single key authenticate as two identities.
    """
    conn = _store(tmp_path)
    add_user(conn, "Alex", "same-verifier", "viewer", [], "admin-token", "2026-06-24T00:00:00+00:00")
    with pytest.raises(sqlite3.IntegrityError):
        add_user(conn, "Sam", "same-verifier", "viewer", [], "admin-token", "2026-06-24T00:00:00+00:00")


def test_list_users_omits_verifier_and_includes_scope(tmp_path):
    """list_users returns scope + flags but NEVER the key verifier, ordered by name.

    Why this matters: an admin listing must never surface credential material (even
    hashed), and should show each user's scope. Ordering by name keeps it readable.
    """
    conn = _store(tmp_path)
    add_user(conn, "Sam", "verifier-sam", "viewer", ["orion"], "admin-token", "2026-06-24T00:00:00+00:00")
    add_user(conn, "Alex", "verifier-alex", "admin", [], "admin-token", "2026-06-24T00:00:00+00:00")
    users = list_users(conn)
    assert [u["name"] for u in users] == ["Alex", "Sam"]  # sorted by name
    assert all("key_verifier" not in u for u in users)    # verifier never exposed
    sam = next(u for u in users if u["name"] == "Sam")
    assert sam["projects"] == ["orion"] and sam["active"] is True


def test_bump_session_version_increments(tmp_path):
    """Bumping a user's session_version advances it (the stateless-logout lever).

    Why this matters: the signed cookie embeds the session_version it was minted with;
    bumping it is what makes an outstanding cookie stop validating on its next request.
    """
    conn = _store(tmp_path)
    uid = add_user(conn, "Alex", "verifier-alex", "viewer", [], "admin-token", "2026-06-24T00:00:00+00:00")
    bump_session_version(conn, uid)
    assert get_user_by_id(conn, uid)["session_version"] == 2


def test_revoke_user_deactivates_and_bumps_version_atomically(tmp_path):
    """Revoking sets active=0 AND bumps session_version in one step.

    Why this matters: revocation must both deny a future login (active=0) and kill any
    live cookie (session_version bump); doing both leaves no window where one applies
    but not the other.
    """
    conn = _store(tmp_path)
    uid = add_user(conn, "Alex", "verifier-alex", "viewer", [], "admin-token", "2026-06-24T00:00:00+00:00")
    revoke_user(conn, uid)
    row = get_user_by_id(conn, uid)
    assert row["active"] == 0 and row["session_version"] == 2


def test_update_last_login_stamps_the_time(tmp_path):
    """A login stamps last_login_at (an operational signal, not an auth gate).

    Why this matters: the admin can see who actually uses their access and spot stale
    grants; it starts NULL and is set on first login.
    """
    conn = _store(tmp_path)
    uid = add_user(conn, "Alex", "verifier-alex", "viewer", [], "admin-token", "2026-06-24T00:00:00+00:00")
    update_last_login(conn, uid, "2026-06-24T12:00:00+00:00")
    assert get_user_by_id(conn, uid)["last_login_at"] == "2026-06-24T12:00:00+00:00"


def test_record_admin_audit_appends_a_row(tmp_path):
    """record_admin_audit writes an append-only trail row with JSON-encoded projects.

    Why this matters: the multi-party model needs accountability — who provisioned or
    revoked whom, with what scope — and it must be queryable after the fact.
    """
    conn = _store(tmp_path)
    record_admin_audit(
        conn, "admin-token", "create_user", "Alex", "viewer",
        ["orion", "incubator"], "2026-06-24T00:00:00+00:00",
    )
    row = conn.execute(
        "SELECT actor, action, target_user, role, projects FROM relay_admin_audit"
    ).fetchone()
    assert row["actor"] == "admin-token" and row["action"] == "create_user"
    assert row["target_user"] == "Alex" and row["role"] == "viewer"
    # projects is JSON-encoded for the TEXT column.
    import json
    assert json.loads(row["projects"]) == ["orion", "incubator"]


# --- observed-state history (E2 Inc 3 Unit 3 — the forward-store's "remember") -------


def _obs(text, done, due_date=None, key=None):
    """Build a checklist wire-item dict (the shape record_observations consumes).

    Why: mirrors the producer's optional-field shape — due_date / key present only when set
    — so tests exercise the real "key absent → fall back to text" path.
    """
    item = {"text": text, "done": done}
    if due_date is not None:
        item["due_date"] = due_date
    if key is not None:
        item["key"] = key
    return item


def test_record_observations_accumulates_across_pushes(tmp_path):
    """Each push APPENDS observation rows rather than replacing — history accumulates.

    Why this matters: this is the difference from relay_project_checklists (current state,
    upserted). The forward-store must keep every push's observation so slippage/history can
    be derived later. Two pushes of the same item yield two rows, oldest first.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    record_observations(conn, "demo", [_obs("A", False, key="A")], "2026-06-25T00:00:00+00:00")
    record_observations(conn, "demo", [_obs("A", True, key="A")], "2026-06-26T00:00:00+00:00")

    hist = observed_history(conn, "demo")

    assert len(hist) == 2
    # Oldest first; done decoded back to a real bool.
    assert [h["done"] for h in hist] == [False, True]
    assert [h["observed_at"] for h in hist] == [
        "2026-06-25T00:00:00+00:00",
        "2026-06-26T00:00:00+00:00",
    ]


def test_observed_item_key_stable_across_a_status_change(tmp_path):
    """A tracker application keeps ONE item_key as its status (and text) changes.

    Why this matters: the whole reason for a producer-emitted key. The application's text
    embeds status ("App - Not started" → "App - Submitted"), so a text-keyed store would see
    two different items and lose the deadline history. With key=title, both pushes land under
    the SAME item_key, so slippage (Unit 4) can track the one item — and see its deadline move
    later — across the status change.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    record_observations(
        conn,
        "demo",
        [_obs("App - Not started", False, due_date="2026-07-01", key="App")],
        "2026-06-20T00:00:00+00:00",
    )
    record_observations(
        conn,
        "demo",
        [_obs("App - Submitted", True, due_date="2026-07-15", key="App")],
        "2026-06-26T00:00:00+00:00",
    )

    hist = observed_history(conn, "demo")

    # One identity across both pushes, not two — despite the text changing.
    assert {h["item_key"] for h in hist} == {"App"}
    # And the deadline's move-later is visible in time order (the slippage signal).
    assert [(h["due_date"], h["done"]) for h in hist] == [
        ("2026-07-01", False),
        ("2026-07-15", True),
    ]


def test_observed_history_rebuilds_current_state_as_latest_per_key(tmp_path):
    """Folding the history (newest observation per key) reconstructs the live checklist.

    Why this matters: the store is a PROJECTION — rebuildable from the append-only record.
    Taking the latest observation for each item_key must match the final pushed state, which
    is what makes "remember" a faithful downstream view rather than authored truth.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    record_observations(
        conn,
        "demo",
        [_obs("A - Not started", False, key="A"), _obs("B", False)],
        "2026-06-25T00:00:00+00:00",
    )
    record_observations(
        conn,
        "demo",
        [_obs("A - Submitted", True, due_date="2026-07-01", key="A"), _obs("B", True)],
        "2026-06-26T00:00:00+00:00",
    )

    # Fold oldest→newest, so the last write per item_key wins (a simple projection).
    latest = {}
    for h in observed_history(conn, "demo"):
        latest[h["item_key"]] = (h["done"], h["due_date"])

    assert latest == {"A": (True, "2026-07-01"), "B": (True, None)}


def test_record_observations_falls_back_to_text_when_no_key(tmp_path):
    """An item with no `key` is identified by its text (tasks/table items are stable).

    Why this matters: only tracker applications need the separate key; a plain checkbox item
    carries no status in its text, so the store keys it by text — no producer change for the
    common case, mirroring KI-6's text identity.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    record_observations(conn, "demo", [_obs("Plain task", False)], "2026-06-26T00:00:00+00:00")

    assert observed_history(conn, "demo")[0]["item_key"] == "Plain task"


def test_observed_history_empty_for_project_with_no_observations(tmp_path):
    """A project with no recorded observations returns an empty list, not an error.

    Why this matters: a project that has never pushed a checklist simply has no forward
    history; the read must degrade to [] so callers (and Unit 4's derivation) handle it cleanly.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    assert observed_history(conn, "demo") == []


def test_latest_report_per_project_counts_slipping_from_history(tmp_path):
    """The portfolio row carries a slipping count derived from the observation history.

    Why this matters: the "N slipping" badge needs a precomputed count. Two pushes postpone
    item A's deadline (2026-07-01 → 2026-07-15) — A is slipping — while the project's live
    checklist makes it appear in the portfolio set. The count must be 1.
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    push1 = [{"text": "A - Not started", "done": False, "due_date": "2026-07-01", "key": "A"}]
    push2 = [{"text": "A - In progress", "done": False, "due_date": "2026-07-15", "key": "A"}]
    # Mirror the server: upsert the live checklist AND append observations on each push.
    upsert_checklist(conn, "demo", push1, "2026-06-20T00:00:00+00:00")
    record_observations(conn, "demo", push1, "2026-06-20T00:00:00+00:00")
    upsert_checklist(conn, "demo", push2, "2026-06-25T00:00:00+00:00")
    record_observations(conn, "demo", push2, "2026-06-25T00:00:00+00:00")

    row = {r["project"]: r for r in latest_report_per_project(conn, today=date(2026, 6, 26))}["demo"]

    assert row["checklist_slipping"] == 1


def test_latest_report_per_project_slipping_is_none_without_today(tmp_path):
    """Without a reference date, slippage derivation is skipped → checklist_slipping is None.

    Why this matters: `today` gates the derivation (mirroring at-risk). A caller that omits it
    (or a test of the existing counts) is unaffected — None reads as "omit the badge".
    """
    conn = open_relay_store(tmp_path / "relay.sqlite3")
    upsert_checklist(conn, "demo", _items(("A", False)), "2026-06-26T00:00:00+00:00")

    row = {r["project"]: r for r in latest_report_per_project(conn)}["demo"]

    assert row["checklist_slipping"] is None

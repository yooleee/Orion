# =============================================================================
# tests/test_relay_api.py
# -----------------------------------------------------------------------------
# Responsible for: Pinning the PURE JSON serializers (relay/api.py) — the shapes
#                  the dashboard SPA consumes (me / portfolio / project / report).
# Role in project: api.py is the SPA<->relay seam (docs/dashboard-api-contract.md).
#                  These tests fix every wire shape against fixed inputs + a fixed
#                  "today", so a contract drift (a renamed/missing field, a wrong
#                  projects-vs-trackers split, a mis-derived state) fails here before
#                  it reaches the SPA. The serializers are pure (data in, dict out),
#                  so no server or DB is needed — the HTTP wiring is covered in
#                  test_relay_server.py.
# =============================================================================

from datetime import date
from zoneinfo import ZoneInfo

import relay.api as api

# A fixed reference date so every derived state is deterministic (no real clock).
_TODAY = date(2026, 6, 26)
_LA = ZoneInfo("America/Los_Angeles")


def _item(text, *, done=False, due_date=None, key=None, group=None, status=None):
    """Build a checklist wire dict, attaching optional fields only when given.

    Why: mirrors the producer's optional-field shape (an absent field is a missing key,
    not None), so the serializer is exercised on the real "key absent" path.
    """
    item = {"text": text, "done": done}
    if due_date is not None:
        item["due_date"] = due_date
    if key is not None:
        item["key"] = key
    if group is not None:
        item["group"] = group
    if status is not None:
        item["status"] = status
    return item


# --- serialize_me: identity / scope / context --------------------------------


def test_me_open_relay_is_unauthenticated_and_unrestricted():
    """An ungated relay reports not-gated, anonymous, and unrestricted scope.

    Why this matters: the SPA decides whether to force a login from /api/me. On a bare
    loopback relay nobody logs in and everything is visible, so the SPA must NOT redirect.
    """
    me = api.serialize_me(gated=False, principal=None, allowed=None, display_tz=_LA)
    assert me["gated"] is False
    assert me["authenticated"] is False
    assert me["identity"] is None
    assert me["scope"] == {"unrestricted": True, "projects": None}
    assert me["display_tz"] == "America/Los_Angeles"
    assert me["showcase_enabled"] is False


def test_me_admin_is_identified_and_unrestricted():
    """An admin principal surfaces name+role and unrestricted scope."""
    me = api.serialize_me(
        gated=True,
        principal={"user_id": 1, "role": "admin", "name": "Teammate B"},
        allowed=None,
        display_tz=_LA,
    )
    assert me["authenticated"] is True
    assert me["identity"] == {"name": "Teammate B", "role": "admin"}
    assert me["scope"]["unrestricted"] is True


def test_me_viewer_scope_is_the_sorted_granted_projects():
    """A scoped viewer reports unrestricted=False and the sorted granted project list."""
    me = api.serialize_me(
        gated=True,
        principal={"user_id": 2, "role": "viewer", "name": "Mum"},
        allowed={"orion", "sample-app"},
        display_tz=_LA,
    )
    assert me["identity"] == {"name": "Mum", "role": "viewer"}
    assert me["scope"] == {
        "unrestricted": False,
        "projects": ["orion", "sample-app"],  # sorted
    }


def test_me_showcase_enabled_flag_passes_through():
    """showcase_enabled rides the /api/me shape so the SPA can show the public link.

    Why this matters: the "Public showcase" sidebar link is shown only when the relay
    actually exposes the surface. A default of False keeps existing callers (and the open
    loopback case above) unchanged; passing True must surface True.
    """
    me = api.serialize_me(
        gated=True,
        principal={"user_id": 1, "role": "admin", "name": "Teammate B"},
        allowed=None,
        display_tz=_LA,
        showcase_enabled=True,
    )
    assert me["showcase_enabled"] is True


# --- serialize_showcase: the public, no-login curated cards -------------------


def _showcase_row(project, *, done, total, report_count=3, latest_body="", blurb=""):
    """Build a curated-Showcase entry: a latest_report_per_project row + a blurb.

    Why: the showcase serializer reads only a handful of row fields (the counts, the
    report count, the body for the headline fallback) plus the curated blurb the server
    attaches from the allowlist — so the fixture carries exactly those.
    """
    return {
        "project": project,
        "checklist_done": done,
        "checklist_total": total,
        "report_count": report_count,
        "latest_body": latest_body,
        "blurb": blurb,
    }


def test_showcase_card_status_is_active_until_complete_then_shipped():
    """A card reads "active" until every item is done, then "shipped".

    Why this matters: the status pill is DERIVED from completion (observe-and-reframe),
    not authored — so the pill can never disagree with the progress bar beside it.
    """
    out = api.serialize_showcase(
        [
            _showcase_row("orion", done=6, total=15),  # 40% -> active
            _showcase_row("sample-app", done=4, total=4),  # 100% -> shipped
        ]
    )
    cards = {c["name"]: c for c in out["projects"]}
    assert cards["orion"]["status"] == "active"
    assert cards["orion"]["progress"] == {"done": 6, "total": 15, "pct": 40}
    assert cards["sample-app"]["status"] == "shipped"
    assert cards["sample-app"]["progress"]["pct"] == 100


def test_showcase_preserves_allowlist_order_and_report_count():
    """Cards come back in the order the server passed them, carrying the report count."""
    out = api.serialize_showcase(
        [
            _showcase_row("b", done=1, total=2, report_count=7),
            _showcase_row("a", done=0, total=1, report_count=1),
        ]
    )
    assert [c["name"] for c in out["projects"]] == ["b", "a"]  # not re-sorted
    assert out["projects"][0]["report_count"] == 7


def test_showcase_description_prefers_blurb_then_headline():
    """A curated blurb wins; without one, the latest report's headline stands in.

    Why this matters: the operator's editorial copy is the intended public description,
    but an allowlisted project with no blurb yet must still read sensibly rather than
    blank — so the observed headline is the graceful fallback.
    """
    out = api.serialize_showcase(
        [
            _showcase_row(
                "curated", done=1, total=2, blurb="A local-first tracker.",
                latest_body="Headline that should be ignored\nbody",
            ),
            _showcase_row(
                "fallback", done=1, total=2, blurb="",
                latest_body="Observed headline\nmore body",
            ),
            _showcase_row("bare", done=1, total=2, blurb="", latest_body=""),
        ]
    )
    cards = {c["name"]: c for c in out["projects"]}
    assert cards["curated"]["description"] == "A local-first tracker."
    assert cards["fallback"]["description"] == "Observed headline"
    assert cards["bare"]["description"] == ""  # no blurb, no body -> empty, not an error


def test_showcase_card_exposes_only_summary_fields():
    """A card carries ONLY summary facts — no checklist, reports, comments, or deadlines.

    Why this matters: the Showcase is public and no-login, so the privacy boundary is the
    SHAPE of this dict. Pinning the exact key set means a future field added to the
    portfolio/project serializers can't silently leak to anonymous viewers — this test
    fails the moment the public card grows a key.
    """
    out = api.serialize_showcase([_showcase_row("orion", done=6, total=15)])
    card = out["projects"][0]
    assert set(card) == {"name", "description", "status", "progress", "report_count"}
    for leaked in ("checklist", "comments", "reports", "next_due", "at_risk", "body"):
        assert leaked not in card


def test_showcase_empty_input_is_empty_projects():
    """No allowlisted projects -> an empty list, not an error (disabled is handled upstream)."""
    assert api.serialize_showcase([]) == {"projects": []}


# --- serialize_portfolio: the projects-vs-trackers split ----------------------


def _project_row():
    """A latest_report_per_project row (+items) for a real project with a checklist."""
    return {
        "project": "orion",
        "kind": "project",
        "checklist_done": 6,
        "checklist_total": 15,
        "checklist_at_risk": 2,
        "checklist_slipping": 0,
        "latest_generated_at": "2026-06-26T10:00:00+00:00",
        "latest_report_id": 26,
        "latest_body": "Orion progress update\nmore detail here",
        "checklist_updated_at": None,
        "items": [_item("Ship the API", due_date="2026-06-29")],  # due_soon
    }


def _tracker_row():
    """A latest_report_per_project row (+items) for a checklist-only tracker."""
    return {
        "project": "applications",
        "kind": "tracker",
        "checklist_done": 0,
        "checklist_total": 3,
        "checklist_at_risk": 2,
        "checklist_slipping": 1,
        "latest_generated_at": None,
        "latest_report_id": None,
        "latest_body": None,
        "checklist_updated_at": "2026-06-26T09:00:00+00:00",
        "items": [
            _item("Hack Your Summer", due_date="2026-06-24", key="Hack Your Summer"),
            _item("Claude Corps Fellow", due_date="2026-06-30", key="Claude Corps Fellow"),
            _item("Far thing", due_date="2026-08-01"),
        ],
    }


def test_portfolio_splits_projects_from_trackers_by_kind():
    """A project lands in `projects`, a tracker in `trackers` — the core IA split.

    Why this matters: the home's two sections are driven by `kind`, decided server-side.
    A project must never appear under To-dos and vice versa.
    """
    out = api.serialize_portfolio([_project_row(), _tracker_row()], None, _TODAY)
    assert [p["name"] for p in out["projects"]] == ["orion"]
    assert [t["name"] for t in out["trackers"]] == ["applications"]
    assert out["scope"] == {"unrestricted": True, "projects": None}


def test_portfolio_project_row_carries_headline_progress_and_facts():
    """A project row exposes headline, progress%, at-risk/slipping counts, and next_due."""
    out = api.serialize_portfolio([_project_row()], None, _TODAY)
    row = out["projects"][0]
    assert row["headline"] == "Orion progress update"  # body's first line
    assert row["report_id"] == 26
    assert row["progress"] == {"done": 6, "total": 15, "pct": 40}
    assert row["at_risk"] == 2
    assert row["slipping"] == 0
    assert row["next_due"] == {"due_date": "2026-06-29", "state": "due_soon"}
    assert row["updated_at"] == "2026-06-26T10:00:00+00:00"


def test_portfolio_tracker_row_carries_segments_and_ordered_chips():
    """A tracker row adds the segmented-bar buckets and overdue-first at-risk chips."""
    out = api.serialize_portfolio([_tracker_row()], None, _TODAY)
    row = out["trackers"][0]
    assert row["item_count"] == 3
    # overdue(1) + due_soon(1) + remaining-open(1) + done(0) tile the 3 items.
    assert row["segments"] == {"overdue": 1, "due_soon": 1, "remaining": 1, "done": 0}
    # chips: overdue before due_soon, labelled by the stable key.
    assert [(c["state"], c["label"]) for c in row["at_risk_items"]] == [
        ("overdue", "Hack Your Summer"),
        ("due_soon", "Claude Corps Fellow"),
    ]
    # next_due is the soonest open deadline (the overdue one) with its state.
    assert row["next_due"] == {"due_date": "2026-06-24", "state": "overdue"}
    # a tracker has no project-only fields.
    assert "headline" not in row and "report_id" not in row


def test_portfolio_progress_pct_is_none_when_no_items():
    """A report-only project (no checklist) reports total 0 and pct None, not a crash."""
    row = {
        "project": "barebones",
        "kind": "project",
        "checklist_done": None,
        "checklist_total": None,
        "checklist_at_risk": None,
        "checklist_slipping": None,
        "latest_generated_at": "2026-06-25T00:00:00+00:00",
        "latest_report_id": 9,
        "latest_body": "Shipped the detection layer",
        "checklist_updated_at": None,
        "items": None,
    }
    out = api.serialize_portfolio([row], None, _TODAY)
    p = out["projects"][0]
    assert p["progress"] == {"done": 0, "total": 0, "pct": None}
    assert p["at_risk"] == 0 and p["slipping"] == 0
    assert p["next_due"] is None


def test_portfolio_scope_echoes_a_viewers_grants():
    """When scoped, the response echoes the viewer's sorted granted projects."""
    out = api.serialize_portfolio([_project_row()], {"orion"}, _TODAY)
    assert out["scope"] == {"unrestricted": False, "projects": ["orion"]}


# --- serialize_project: full detail ------------------------------------------


def test_project_detail_assembles_stats_milestones_checklist_reports_discussions():
    """The project shape carries stats, milestones (with slipping), checklist, reports, discussions."""
    reports = [
        {
            "id": 26,
            "project": "orion",
            "body": "Update",
            "sections": [["SHIPPED", "x"], ["NOTES", "y"]],
            "participants": ["Alex"],
            "share_level": "high_level",
            "lane": "structured",
            "generated_at": "2026-06-26T10:00:00+00:00",
            "orion_version": "0.0.0",
            "ingested_at": "2026-06-26T10:01:00+00:00",
        }
    ]
    checklist = [
        _item("Task X", due_date="2026-06-28", key="todo-x", group="G"),  # slipping below
        _item("Done thing", done=True, group="G"),
        _item("Overdue thing", due_date="2026-06-20"),  # overdue, ungrouped
    ]
    # Two observations of todo-x with the deadline moved LATER → slipping.
    observations = [
        {"item_key": "todo-x", "due_date": "2026-06-20", "done": False, "observed_at": "2026-06-22T00:00:00+00:00"},
        {"item_key": "todo-x", "due_date": "2026-06-28", "done": False, "observed_at": "2026-06-26T00:00:00+00:00"},
    ]
    # A two-turn discussion thread: a supervisor message and the developer's reply. The
    # store dict carries author_id + project, which the wire shape drops (see assertion).
    discussions = [
        {"id": 1, "project": "orion", "author_id": 7, "author_name": "Supervisor A",
         "role": "supervisor", "body": "How's auth?", "created_at": "2026-06-26T09:00:00+00:00"},
        {"id": 2, "project": "orion", "author_id": None, "author_name": "orion-cli",
         "role": "developer", "body": "Landed.", "created_at": "2026-06-26T12:00:00+00:00"},
    ]
    out = api.serialize_project(
        name="orion",
        kind="project",
        reports=reports,
        checklist=checklist,
        observations=observations,
        producer_checklists=[],
        discussions=discussions,
        disciplines=None,
        today=_TODAY,
    )
    assert out["name"] == "orion" and out["kind"] == "project"
    assert out["description"] is None  # gap 5
    assert out["stats"]["progress"] == {"done": 1, "total": 3, "pct": 33}
    assert out["stats"]["reports_count"] == 1
    # next_due is the soonest open deadline across the checklist (the overdue 06-20).
    assert out["stats"]["next_due"] == {"due_date": "2026-06-20", "state": "overdue"}

    # The milestone group "G" rolls up its two items and flags slipping (todo-x slipped).
    g = next(m for m in out["milestones"] if m["group"] == "G")
    assert g["done"] == 1 and g["total"] == 2
    assert g["slipping"] is True

    # The checklist rows carry per-item state + slipping membership.
    by_text = {r["text"]: r for r in out["checklist"]}
    assert by_text["Task X"]["state"] == "due_soon" and by_text["Task X"]["slipping"] is True
    assert by_text["Done thing"]["state"] == "done"
    assert by_text["Overdue thing"]["state"] == "overdue" and by_text["Overdue thing"]["slipping"] is False

    # Reports summary: title (body headline) + section count + empty source_tags (gap 4).
    # number is the per-project ordinal — 1 here (this project has a single report).
    assert out["reports"][0] == {
        "id": 26,
        "number": 1,
        "title": "Update",
        "generated_at": "2026-06-26T10:00:00+00:00",
        "lane": "structured",
        "share_level": "high_level",
        "section_count": 2,
        "author_name": None,  # C3 Inc 2: null for an unattributed report
        "source_tags": [],
    }
    # Discussions (E2 Inc 5): a REAL role per item (gap 7 closed for this surface), oldest
    # first. author_id and project are dropped from the wire (the badge needs name + role).
    assert out["discussions"] == [
        {"id": 1, "author_name": "Supervisor A", "role": "supervisor", "body": "How's auth?",
         "created_at": "2026-06-26T09:00:00+00:00"},
        {"id": 2, "author_name": "orion-cli", "role": "developer", "body": "Landed.",
         "created_at": "2026-06-26T12:00:00+00:00"},
    ]


def test_project_detail_marks_slipping_per_producer_stream():
    """Each producer card marks slippage from its OWN stream; aggregate + milestone use the union.

    Why this matters: the C3 Inc 2.5 fix. Item X (grouped in "G") was postponed in producer 1's
    observation stream but stayed steady in producer 2's. Producer 1's card must flag X slipping,
    producer 2's must not, and the aggregate checklist row + milestone roll-up must flag it (the
    project-wide union). This is what stops one machine's slip from smearing across every card
    while still surfacing it at the project level.
    """
    checklist = [_item("Task X", due_date="2026-06-28", key="todo-x", group="G")]
    # Producer 1 postponed todo-x (06-20 → 06-28) → slipping in stream 1; producer 2 kept it
    # steady at 06-28 across two pushes → not slipping in stream 2.
    observations = [
        {"item_key": "todo-x", "due_date": "2026-06-20", "done": False,
         "observed_at": "2026-06-22T00:00:00+00:00", "author_id": 1},
        {"item_key": "todo-x", "due_date": "2026-06-28", "done": False,
         "observed_at": "2026-06-26T00:00:00+00:00", "author_id": 1},
        {"item_key": "todo-x", "due_date": "2026-06-28", "done": False,
         "observed_at": "2026-06-22T00:00:00+00:00", "author_id": 2},
        {"item_key": "todo-x", "due_date": "2026-06-28", "done": False,
         "observed_at": "2026-06-26T00:00:00+00:00", "author_id": 2},
    ]
    producer_checklists = [
        {"author_id": 1, "author_name": "Producer One",
         "items": [_item("Task X", due_date="2026-06-28", key="todo-x", group="G")]},
        {"author_id": 2, "author_name": "Producer Two",
         "items": [_item("Task X", due_date="2026-06-28", key="todo-x", group="G")]},
    ]
    out = api.serialize_project(
        name="orion",
        kind="project",
        reports=[],
        checklist=checklist,
        observations=observations,
        producer_checklists=producer_checklists,
        discussions=[],
        disciplines=None,
        today=_TODAY,
    )

    # Aggregate row + milestone use the union → X slipping.
    assert out["checklist"][0]["slipping"] is True
    assert next(m for m in out["milestones"] if m["group"] == "G")["slipping"] is True

    # Each producer card marks slippage from its OWN stream only.
    cards = {c["author_name"]: c for c in out["producer_checklists"]}
    assert cards["Producer One"]["items"][0]["slipping"] is True  # postponed in stream 1
    assert cards["Producer Two"]["items"][0]["slipping"] is False  # steady in stream 2
    # The internal author_id is never emitted on a card.
    assert "author_id" not in cards["Producer One"]


def test_project_detail_emits_in_progress_state_and_passes_status_through():
    """A tracker item's structured status drives in_progress state + ships raw on the row (gap 8).

    Why this matters: this is the closure of gap 8. An open, undated item the producer marked
    in_progress used to collapse to not_started; it must now report state "in_progress". An
    open item with a near deadline keeps its deadline urgency (overdue/due_soon LEADS the
    single state), while the raw `status` still rides the row so the tracker's circular arc is
    independent of that derived state. A done item reports "done" and carries its submitted
    status. An item with no status keeps the old behaviour and a null `status`.
    """
    checklist = [
        _item("Started, undated", status="in_progress"),  # the gap-8 case
        _item("Started but overdue", due_date="2026-06-20", status="in_progress"),
        _item("Submitted app", done=True, status="submitted"),
        _item("Plain open item"),  # no status → not_started, status null
    ]
    out = api.serialize_project(
        name="apps",
        kind="tracker",
        reports=[],
        checklist=checklist,
        observations=[],
        producer_checklists=[],
        discussions=[],
        disciplines=None,
        today=_TODAY,
    )
    by_text = {r["text"]: r for r in out["checklist"]}
    # The gap-8 case: open + undated + in_progress now reports in_progress (was not_started).
    assert by_text["Started, undated"]["state"] == "in_progress"
    assert by_text["Started, undated"]["status"] == "in_progress"
    # Deadline urgency leads the single derived state, but raw status still rides the row.
    assert by_text["Started but overdue"]["state"] == "overdue"
    assert by_text["Started but overdue"]["status"] == "in_progress"
    # Done items report done and carry their submitted status for the label nuance.
    assert by_text["Submitted app"]["state"] == "done"
    assert by_text["Submitted app"]["status"] == "submitted"
    # No status ⇒ old behaviour and a null status (back-compat with status-less producers).
    assert by_text["Plain open item"]["state"] == "not_started"
    assert by_text["Plain open item"]["status"] is None


def test_project_detail_handles_no_checklist():
    """A project with reports but no checklist yields empty progress + no milestones."""
    out = api.serialize_project(
        name="orion",
        kind="project",
        reports=[],
        checklist=None,
        observations=[],
        producer_checklists=[],
        discussions=[],
        disciplines=None,
        today=_TODAY,
    )
    assert out["stats"]["progress"] == {"done": 0, "total": 0, "pct": None}
    assert out["stats"]["next_due"] is None
    assert out["milestones"] == [] and out["checklist"] == []


# --- serialize_report: body + rail + nav -------------------------------------


def _report(report_id=26):
    return {
        "id": report_id,
        "project": "orion",
        "body": "Orion progress update\nShipped a four-part slice.",
        "sections": [["SHIPPED", "..."], ["DIRECTION", "..."]],
        "participants": ["Alex", "Sam"],
        "share_level": "high_level",
        "lane": "structured",
        "generated_at": "2026-06-26T10:00:00+00:00",
        "orion_version": "0.0.0",
        "ingested_at": "2026-06-26T10:01:00+00:00",
    }


def test_report_detail_title_is_body_headline_not_section_label():
    """The display title is the body's first line, distinct from the section labels."""
    out = api.serialize_report(
        report=_report(),
        checklist=None,
        history=[_report()],
        today=_TODAY,
    )
    assert out["title"] == "Orion progress update"  # NOT "SHIPPED"
    assert out["sections"] == [["SHIPPED", "..."], ["DIRECTION", "..."]]
    # participants become {name, role:null} (gap 3); source_tags empty (gap 4).
    assert out["participants"] == [
        {"name": "Alex", "role": None},
        {"name": "Sam", "role": None},
    ]
    assert out["source_tags"] == []


def test_report_detail_checklist_snapshot_counts_and_states():
    """The rail snapshot reports done/total and per-row state."""
    checklist = [
        _item("Done", done=True),
        _item("Soon", due_date="2026-06-29"),
        _item("Late", due_date="2026-06-20"),
    ]
    out = api.serialize_report(
        report=_report(),
        checklist=checklist,
        history=[_report()],
        today=_TODAY,
    )
    snap = out["checklist_snapshot"]
    assert snap["done"] == 1 and snap["total"] == 3
    assert [r["state"] for r in snap["rows"]] == ["done", "due_soon", "overdue"]


def test_report_detail_nav_points_prev_to_older_next_to_newer():
    """nav.prev_* is the older neighbour, next_* the newer; ids route, numbers label."""
    # history is newest-first: 27, 26, 25. Per-PROJECT ordinals: 25→1, 26→2, 27→3.
    history = [_report(27), _report(26), _report(25)]
    out = api.serialize_report(
        report=_report(26), checklist=None, history=history, today=_TODAY
    )
    # The middle report: older=25 (#1), newer=27 (#3). ids drive links, numbers the labels.
    assert out["nav"] == {
        "prev_id": 25, "prev_number": 1, "next_id": 27, "next_number": 3
    }

    # The newest report has a previous (older) but no next (newer).
    out_latest = api.serialize_report(
        report=_report(27), checklist=None, history=history, today=_TODAY
    )
    assert out_latest["nav"] == {
        "prev_id": 26, "prev_number": 2, "next_id": None, "next_number": None
    }


def test_report_number_is_per_project_ordinal_not_global_id():
    """A report's display `number` is its 1-based position in THIS project, not the global id.

    Why this matters: the relay `id` is a single global autoincrement across all projects, so
    per project the ids are gappy (e.g. orion #1-5 then #18-29). The dashboard shows a
    per-project ordinal (oldest = 1) for legibility while `id` stays the routing identity.
    """
    # A project whose three reports took non-contiguous global ids (29, 18, 5), newest-first.
    history = [_report(29), _report(18), _report(5)]
    # Oldest (id 5) is #1, then 18 → #2, newest 29 → #3 — independent of the global ids.
    nums = {
        r["id"]: api.serialize_report(
            report=r, checklist=None, history=history, today=_TODAY
        )["number"]
        for r in history
    }
    assert nums == {5: 1, 18: 2, 29: 3}


# --- serialize_scheduling: cross-project deadline buckets --------------------


def test_scheduling_buckets_open_dated_items_with_source_and_excludes_the_rest():
    """Open, dated items bucket by deadline urgency; done/undated are excluded.

    Why this matters: Scheduling is the cross-project "by when" lens. Only OPEN, DATED
    items belong on a timeline — a done item or an undated one has no place — and each
    must carry where it came from (source name + kind) so the SPA can tag it ◇/⊟. The
    bucket must agree with the same overdue/due_soon/upcoming classification the rest of
    the dashboard uses (today = 2026-06-26).
    """
    projects = [
        {
            "name": "alpha",
            "kind": "project",
            "items": [
                _item("Past thing", due_date="2026-06-20"),       # overdue
                _item("Soon thing", due_date="2026-06-29"),       # this week (<=7d)
                _item("Far thing", due_date="2026-08-01"),        # later (>7d)
                _item("Undated open"),                            # excluded (no date)
                _item("Done past", done=True, due_date="2026-06-19"),  # excluded (done)
            ],
            "observations": [],
        },
        {
            "name": "applications",
            "kind": "tracker",
            # The tracker embeds status in text; key is the clean title used as the label.
            "items": [_item("App (job) - In progress", due_date="2026-06-22", key="App (job)")],
            "observations": [],
        },
    ]
    out = api.serialize_scheduling(projects, _TODAY)

    # OVERDUE holds both past-due open items, soonest (most overdue) first.
    overdue = out["buckets"]["overdue"]
    assert [r["due_date"] for r in overdue] == ["2026-06-20", "2026-06-22"]
    assert [r["label"] for r in overdue] == ["Past thing", "App (job)"]  # tracker uses key
    # Source tag carries name + kind so the SPA can render ◇ project / ⊟ tracker.
    assert overdue[1]["source"] == {"name": "applications", "kind": "tracker"}
    assert overdue[0]["source"] == {"name": "alpha", "kind": "project"}

    assert [r["label"] for r in out["buckets"]["this_week"]] == ["Soon thing"]
    assert [r["label"] for r in out["buckets"]["later"]] == ["Far thing"]

    # The undated-open and the done item never appear in any bucket.
    all_labels = [r["label"] for b in out["buckets"].values() for r in b]
    assert "Undated open" not in all_labels and "Done past" not in all_labels

    # Summary counts mirror the buckets; nothing is slipping here.
    assert out["summary"] == {"overdue": 2, "due_this_week": 1, "slipping": 0}


def test_scheduling_marks_and_counts_slipping_items():
    """An item whose deadline moved later (slipping) is flagged + counted in the summary.

    Why this matters: the design's summary shows "↝ N slipping". Slippage reuses the same
    observation-history derivation the project page uses, so the count agrees across views.
    """
    projects = [
        {
            "name": "alpha",
            "kind": "project",
            "items": [_item("Slipping task", due_date="2026-06-29", key="todo-x")],
            # Two observations of todo-x with the deadline pushed LATER → slipping.
            "observations": [
                {"item_key": "todo-x", "due_date": "2026-06-20", "done": False,
                 "observed_at": "2026-06-22T00:00:00+00:00"},
                {"item_key": "todo-x", "due_date": "2026-06-29", "done": False,
                 "observed_at": "2026-06-26T00:00:00+00:00"},
            ],
        }
    ]
    out = api.serialize_scheduling(projects, _TODAY)
    row = out["buckets"]["this_week"][0]
    assert row["slipping"] is True
    assert out["summary"]["slipping"] == 1


def test_scheduling_empty_when_nothing_open_and_dated():
    """No open dated items anywhere → empty buckets and zeroed summary (not an error)."""
    projects = [{"name": "alpha", "kind": "project", "items": [_item("x")], "observations": []}]
    out = api.serialize_scheduling(projects, _TODAY)
    assert out["buckets"] == {"overdue": [], "this_week": [], "later": []}
    assert out["summary"] == {"overdue": 0, "due_this_week": 0, "slipping": 0}


# --- serialize_project: "Working agreements" disciplines (Unit 5) ----------------


def _disc(title, why="why", scope="project", source="CLAUDE.md"):
    """Build one stored discipline dict (the {title, why, scope, source} push shape)."""
    return {"title": title, "why": why, "scope": scope, "source": source}


def _project(disciplines):
    """Serialize a minimal project carrying only the given disciplines value.

    Args:
        disciplines: the store.project_disciplines result ({"cards", "updated_at"} or None).

    Why: the disciplines wiring is the only thing under test here, so everything else is
    empty — keeps the assertions about the "disciplines" field alone.
    """
    return api.serialize_project(
        name="orion",
        kind="project",
        reports=[],
        checklist=None,
        observations=[],
        producer_checklists=[],
        discussions=[],
        disciplines=disciplines,
        today=_TODAY,
    )


def test_project_disciplines_emit_cards_and_freshness_dropping_scope():
    """The project carries its discipline cards (scope dropped) plus the freshness stamp.

    Why this matters: the "Working agreements" section renders every one of a project's
    cards regardless of the model's global/project scope, and shows an "updated <date>"
    line — so serialize_project must emit {cards: [{title, why, source}], updated_at}.
    """
    out = _project(
        {
            "cards": [
                _disc("Local-first", scope="global", source="CLAUDE.md"),
                _disc("Observe, not originate", scope="project", source="design/README.md"),
            ],
            "updated_at": "2026-06-27T10:00:00+00:00",
        }
    )
    assert out["disciplines"] == {
        "cards": [
            {"title": "Local-first", "why": "why", "source": "CLAUDE.md"},
            {"title": "Observe, not originate", "why": "why", "source": "design/README.md"},
        ],
        "updated_at": "2026-06-27T10:00:00+00:00",
    }


def test_project_disciplines_null_when_absent():
    """A project that never pushed disciplines (None) serializes to a null field.

    Why this matters: null lets the SPA omit the section entirely rather than render an
    empty "Working agreements" heading.
    """
    assert _project(None)["disciplines"] is None


def test_project_disciplines_null_when_empty_cleared_set():
    """A cleared set (empty cards) also serializes to null — no empty section is shown.

    Why this matters: a doc that once stated principles but no longer does clears to [];
    like the never-pushed case, that should hide the section, not show an empty one.
    """
    assert _project({"cards": [], "updated_at": "2026-06-27T10:00:00+00:00"})["disciplines"] is None

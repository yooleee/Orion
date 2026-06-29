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
        principal={"user_id": 1, "role": "admin", "name": "Yusuf"},
        allowed=None,
        display_tz=_LA,
    )
    assert me["authenticated"] is True
    assert me["identity"] == {"name": "Yusuf", "role": "admin"}
    assert me["scope"]["unrestricted"] is True


def test_me_viewer_scope_is_the_sorted_granted_projects():
    """A scoped viewer reports unrestricted=False and the sorted granted project list."""
    me = api.serialize_me(
        gated=True,
        principal={"user_id": 2, "role": "viewer", "name": "Mum"},
        allowed={"orion", "barebones-ai-village"},
        display_tz=_LA,
    )
    assert me["identity"] == {"name": "Mum", "role": "viewer"}
    assert me["scope"] == {
        "unrestricted": False,
        "projects": ["barebones-ai-village", "orion"],  # sorted
    }


def test_me_showcase_enabled_flag_passes_through():
    """showcase_enabled rides the /api/me shape so the SPA can show the public link.

    Why this matters: the "Public showcase" sidebar link is shown only when the relay
    actually exposes the surface. A default of False keeps existing callers (and the open
    loopback case above) unchanged; passing True must surface True.
    """
    me = api.serialize_me(
        gated=True,
        principal={"user_id": 1, "role": "admin", "name": "Yusuf"},
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
            _showcase_row("barebones-ai-village", done=4, total=4),  # 100% -> shipped
        ]
    )
    cards = {c["name"]: c for c in out["projects"]}
    assert cards["orion"]["status"] == "active"
    assert cards["orion"]["progress"] == {"done": 6, "total": 15, "pct": 40}
    assert cards["barebones-ai-village"]["status"] == "shipped"
    assert cards["barebones-ai-village"]["progress"]["pct"] == 100


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


def test_project_detail_assembles_stats_milestones_checklist_reports_comments():
    """The project shape carries stats, milestones (with slipping), checklist, reports, comments."""
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
    comments = [
        {"id": 1, "report_id": 26, "author": "Alex", "body": "nice", "created_at": "2026-06-25T00:00:00+00:00"}
    ]
    # A two-turn discussion thread: a supervisor message and the developer's reply. The
    # store dict carries author_id + project, which the wire shape drops (see assertion).
    discussions = [
        {"id": 1, "project": "orion", "author_id": 7, "author_name": "Dad",
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
        comments=comments,
        discussions=discussions,
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
        "source_tags": [],
    }
    # Comments: role is null in 4a (gap 7), report_id dropped.
    assert out["comments"] == [
        {"id": 1, "author": "Alex", "role": None, "body": "nice", "created_at": "2026-06-25T00:00:00+00:00"}
    ]
    # Discussions (E2 Inc 5): a REAL role per item (gap 7 closed for this surface), oldest
    # first. author_id and project are dropped from the wire (the badge needs name + role).
    assert out["discussions"] == [
        {"id": 1, "author_name": "Dad", "role": "supervisor", "body": "How's auth?",
         "created_at": "2026-06-26T09:00:00+00:00"},
        {"id": 2, "author_name": "orion-cli", "role": "developer", "body": "Landed.",
         "created_at": "2026-06-26T12:00:00+00:00"},
    ]


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
        comments=[],
        discussions=[],
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
        comments=[],
        discussions=[],
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
        comments=[],
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
        comments=[],
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
        report=_report(26), checklist=None, comments=[], history=history, today=_TODAY
    )
    # The middle report: older=25 (#1), newer=27 (#3). ids drive links, numbers the labels.
    assert out["nav"] == {
        "prev_id": 25, "prev_number": 1, "next_id": 27, "next_number": 3
    }

    # The newest report has a previous (older) but no next (newer).
    out_latest = api.serialize_report(
        report=_report(27), checklist=None, comments=[], history=history, today=_TODAY
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
            report=r, checklist=None, comments=[], history=history, today=_TODAY
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


# --- serialize_disciplines: Global vs per-project split (E2 Inc 4 4b) ------------


def _disc(title, why="why", scope="project", source="CLAUDE.md"):
    """Build one stored discipline dict (the shape get_disciplines returns)."""
    return {"title": title, "why": why, "scope": scope, "source": source}


def test_disciplines_card_shape_drops_scope():
    """Each emitted card is exactly {title, why, source} — scope is consumed by grouping.

    Why this matters: the Global vs project section already encodes scope, so the wire
    card carries only what the SPA renders (title, why, observed-source).
    """
    out = api.serialize_disciplines(
        [{"name": "orion", "disciplines": [_disc("Sectioned", why="distinct sections", source="CLAUDE.md")]}],
        allowed=None,
    )
    assert out["projects"][0]["principles"] == [
        {"title": "Sectioned", "why": "distinct sections", "source": "CLAUDE.md"}
    ]


def test_disciplines_split_global_from_project():
    """Global-scope cards go to `global`; project-scope cards group under their project.

    Why this matters: the design shows a Global section then per-project sections — the
    serializer must bucket by scope so the SPA renders two kinds of group correctly.
    """
    out = api.serialize_disciplines(
        [
            {
                "name": "orion",
                "disciplines": [
                    _disc("Local-first", scope="global", source="CLAUDE.md"),
                    _disc("Observe, not originate", scope="project", source="design/README.md"),
                ],
            }
        ],
        allowed=None,
    )
    assert [c["title"] for c in out["global"]] == ["Local-first"]
    assert out["projects"] == [
        {
            "name": "orion",
            "principles": [
                {"title": "Observe, not originate", "why": "why", "source": "design/README.md"}
            ],
        }
    ]


def test_disciplines_dedupes_globals_with_deterministic_source():
    """A global title stated in two projects dedupes to one card, source picked stably.

    Why this matters: a global convention can appear in several projects' docs. We dedupe
    by normalized title and pick the source from the lexicographically-first (project,
    source) so the footer never flickers with ingest order. Here 'alpha' wins over 'zeta'.
    """
    out = api.serialize_disciplines(
        [
            {"name": "zeta", "disciplines": [_disc("Untrusted text is inert", scope="global", source="zeta/sec.md")]},
            {"name": "alpha", "disciplines": [_disc("untrusted text is inert", scope="global", source="alpha/sec.md")]},
        ],
        allowed=None,
    )
    # One card despite the case-different titles; source comes from project 'alpha'.
    assert len(out["global"]) == 1
    assert out["global"][0]["source"] == "alpha/sec.md"


def test_disciplines_sorts_globals_by_title_and_projects_by_name():
    """Globals sort by title; project groups sort by name; cards within a group by title.

    Why this matters: a stable 2-column grid needs deterministic ordering, so a re-render
    (or a re-extraction in a different order) never reshuffles the cards.
    """
    out = api.serialize_disciplines(
        [
            {
                "name": "beta",
                "disciplines": [
                    _disc("Zed principle", scope="project"),
                    _disc("Able principle", scope="project"),
                ],
            },
            {
                "name": "alpha",
                "disciplines": [
                    _disc("Second global", scope="global"),
                    _disc("First global", scope="global"),
                    _disc("Alpha-only", scope="project"),  # so alpha forms a project group
                ],
            },
        ],
        allowed=None,
    )
    assert [c["title"] for c in out["global"]] == ["First global", "Second global"]
    assert [g["name"] for g in out["projects"]] == ["alpha", "beta"]
    beta = next(g for g in out["projects"] if g["name"] == "beta")
    assert [c["title"] for c in beta["principles"]] == ["Able principle", "Zed principle"]


def test_disciplines_omits_projects_with_no_project_scope_cards():
    """A project with only global cards (or none) produces no per-project group.

    Why this matters: an empty section would be visual noise. A project contributes a
    group only when it has project-scope cards; its global cards still merge into Global.
    """
    out = api.serialize_disciplines(
        [
            {"name": "orion", "disciplines": [_disc("Only global", scope="global")]},
            {"name": "other", "disciplines": None},  # never pushed
        ],
        allowed=None,
    )
    assert [c["title"] for c in out["global"]] == ["Only global"]
    assert out["projects"] == []  # neither project has project-scope cards


def test_disciplines_scope_block_reports_viewer_scope():
    """The scope block reflects unrestricted vs a scoped viewer, like serialize_portfolio.

    Why this matters: the SPA reads scope from the same response; an admin/open relay is
    unrestricted, a scoped viewer lists its granted projects.
    """
    unrestricted = api.serialize_disciplines([], allowed=None)
    assert unrestricted["scope"] == {"unrestricted": True, "projects": None}

    scoped = api.serialize_disciplines([], allowed={"orion", "applications"})
    assert scoped["scope"] == {"unrestricted": False, "projects": ["applications", "orion"]}


# --- serialize_skills: cross-project merge + depth (E2 Inc 4 4c, the comb) --------


def _skill(name, *, category="Backend", evidence="ev", weight=2, signals=("git",)):
    """Build one stored skill dict (the shape get_skills returns)."""
    return {
        "name": name,
        "category": category,
        "evidence": evidence,
        "weight": weight,
        "signals": list(signals),
    }


def test_skills_single_project_shape_and_depth():
    """One project's skill is emitted with its projects anchor and a derived depth.

    Why this matters: the baseline under the RE-TUNED depth scale (boundaries 2/4/6 for
    the global-dedup distribution) — a single-project skill spreads its weight onto the
    lower teeth: incidental (weight 1, score 1) is depth 1, central (weight 3, score 3) is
    depth 2. The taller teeth (3-4) are reserved for skills that recur ACROSS projects, so
    a single project alone cannot reach the top. The merged card names which project
    evidences it.
    """
    out = api.serialize_skills(
        [
            {
                "name": "orion",
                "skills": [
                    _skill("Incidental thing", weight=1),
                    _skill("Central thing", weight=3),
                ],
            }
        ],
        allowed=None,
    )
    by_name = {s["name"]: s for s in out["skills"]}
    assert by_name["Incidental thing"]["depth"] == 1  # score 1 <= T1 (2)
    assert by_name["Central thing"]["depth"] == 2  # score 3 <= T2 (4)
    assert by_name["Central thing"]["projects"] == ["orion"]


def test_skills_merge_across_projects_raises_depth_via_breadth():
    """The same skill in two projects merges to one card whose breadth raises its depth.

    Why this matters: this is the whole point of the cross-project comb — a competency
    shown across the portfolio out-ranks an equally-weighted one confined to one project.
    Under the re-tuned scale (boundaries 2/4/6), two projects at weight 2 (total 4, +1
    breadth = score 5) reach depth 3, above a lone weight-2 skill (score 2 = depth 1). The
    projects anchor unions both, sorted.
    """
    out = api.serialize_skills(
        [
            {"name": "orion", "skills": [_skill("Python backends", weight=2)]},
            {"name": "sar_hackathon", "skills": [_skill("python backends", weight=2)]},
        ],
        allowed=None,
    )
    assert len(out["skills"]) == 1  # casefold-merged despite the capitalization difference
    merged = out["skills"][0]
    assert merged["projects"] == ["orion", "sar_hackathon"]
    assert merged["depth"] == 3  # score 5 <= T3 (6)


def test_skills_merge_picks_canonical_text_and_unions_signals():
    """The merged card's name/category/evidence come from the lexicographically-first project.

    Why this matters: two projects may phrase the same skill differently; the card must not
    flicker with push order, so the canonical text is chosen deterministically ('alpha' <
    'zeta'). Signals union across projects in canonical order.
    """
    out = api.serialize_skills(
        [
            {"name": "zeta", "skills": [_skill("React", category="UI", evidence="zeta ev", signals=("docs",))]},
            {"name": "alpha", "skills": [_skill("react", category="Frontend", evidence="alpha ev", signals=("git",))]},
        ],
        allowed=None,
    )
    merged = out["skills"][0]
    assert merged["category"] == "Frontend"  # from project 'alpha'
    assert merged["evidence"] == "alpha ev"
    assert merged["signals"] == ["git", "docs"]  # unioned, canonical order


def test_skills_categories_ordered_by_total_depth():
    """`categories` lists groups strongest-first (by summed depth), tie-broken by name.

    Why this matters: the comb leads with the developer's deepest area, deterministically,
    so the SPA can render category sections without choosing an order itself.
    """
    out = api.serialize_skills(
        [
            {
                "name": "orion",
                "skills": [
                    _skill("A", category="Backend", weight=3),   # depth 3
                    _skill("B", category="ML / NLP", weight=1),  # depth 1
                ],
            }
        ],
        allowed=None,
    )
    assert out["categories"] == ["Backend", "ML / NLP"]


def test_skills_scope_filtering_hides_out_of_scope_evidence():
    """A skill evidenced only by an out-of-scope project must not appear (existence-hiding).

    Why this matters: the server scope-filters the entries BEFORE the merge, so a scoped
    viewer never learns a skill — or the project that evidences it — outside their grant.
    We simulate the server by passing only the in-scope entries, and assert the merged
    card's projects anchor stays within scope.
    """
    # The server would pass ONLY 'orion' for a viewer scoped to {orion}; 'secret_proj' is
    # filtered out upstream, so its skill never reaches the merge.
    out = api.serialize_skills(
        [{"name": "orion", "skills": [_skill("Shared skill", weight=2)]}],
        allowed={"orion"},
    )
    assert out["scope"] == {"unrestricted": False, "projects": ["orion"]}
    assert out["skills"][0]["projects"] == ["orion"]


def test_skills_empty_when_nothing_pushed():
    """No skills entries yields empty categories + skills, with the scope block intact.

    Why this matters: the empty state must be a clean, well-formed response (the SPA shows
    its own empty message), not a crash or a missing key.
    """
    out = api.serialize_skills([], allowed=None)
    assert out["categories"] == []
    assert out["skills"] == []
    assert out["scope"] == {"unrestricted": True, "projects": None}

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
    out = api.serialize_project(
        name="orion",
        kind="project",
        reports=reports,
        checklist=checklist,
        observations=observations,
        comments=comments,
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
    assert out["reports"][0] == {
        "id": 26,
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
    """nav.prev_id is the older neighbour, next_id the newer; both None at the ends."""
    # history is newest-first: 27, 26, 25.
    history = [_report(27), _report(26), _report(25)]
    out = api.serialize_report(
        report=_report(26), checklist=None, comments=[], history=history, today=_TODAY
    )
    assert out["nav"] == {"prev_id": 25, "next_id": 27}

    # The newest report has a previous (older) but no next (newer).
    out_latest = api.serialize_report(
        report=_report(27), checklist=None, comments=[], history=history, today=_TODAY
    )
    assert out_latest["nav"] == {"prev_id": 26, "next_id": None}


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

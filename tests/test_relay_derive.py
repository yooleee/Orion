# =============================================================================
# tests/test_relay_derive.py
# -----------------------------------------------------------------------------
# Responsible for: Pinning the pure forward-looking derivation (relay/derive.py) —
#                  the overdue / due_soon / at-risk truth-table and the timezone-aware
#                  "today" computation.
# Role in project: derive.py is the single source of "what counts as at risk", read by
#                  both the per-item render and the portfolio badge. If its truth-table
#                  is wrong, the dashboard mislabels deadlines — so we pin every edge
#                  (the today/+7 boundaries, done items, missing/garbage dates, and the
#                  zone-crossing date) against a FIXED today, with no real clock.
# =============================================================================

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from relay.derive import (
    DUE_SOON_DAYS,
    bucket_counts,
    classify_item,
    count_at_risk,
    is_slipping,
    milestones,
    next_open_due,
    slipping_item_keys,
    today_in_tz,
)

# Pacific, the relay's default display zone — the same zone the dashboard renders in.
_LA = ZoneInfo("America/Los_Angeles")
# A fixed reference date so every classification is deterministic (no real clock).
_TODAY = date(2026, 6, 26)


def _item(due_date=None, done=False, text="task"):
    """Build a checklist item dict, attaching due_date only when given.

    Why: mirrors the producer's optional-field shape (an item without a deadline has no
    due_date key at all), so tests exercise the real "key absent" path, not due_date=None.
    """
    item = {"text": text, "done": done}
    if due_date is not None:
        item["due_date"] = due_date
    return item


# --- today_in_tz(): the zone-aware reference date ------------------------------------


def test_today_in_tz_uses_the_zone_local_date_not_utc():
    """An instant that is a different calendar day in UTC vs Pacific resolves to Pacific.

    Why this matters: "overdue" is a per-day judgement, and the relay pins ONE zone so all
    viewers agree. 2026-06-27 05:00 UTC is still 2026-06-26 22:00 in Pacific (UTC-7 in
    summer), so "today" must be the 26th — taking the UTC date would be a day off near
    midnight, mislabeling a deadline as overdue a day early.
    """
    near_midnight_utc = datetime(2026, 6, 27, 5, 0, 0, tzinfo=timezone.utc)
    assert today_in_tz(_LA, now=near_midnight_utc) == date(2026, 6, 26)


# --- classify_item(): the overdue / due_soon / None truth-table ----------------------


@pytest.mark.parametrize(
    "due_date, done, expected",
    [
        # Open items, deadline relative to today (2026-06-26):
        ("2026-06-25", False, "overdue"),   # yesterday → overdue
        ("2026-06-26", False, "due_soon"),  # today → due_soon (date-only = end of day)
        ("2026-06-29", False, "due_soon"),  # +3 days → due_soon
        ("2026-07-03", False, "due_soon"),  # +7 days → boundary, inclusive
        ("2026-07-04", False, None),        # +8 days → beyond the horizon
        # A DONE item is never at risk, regardless of its (even past) deadline:
        ("2026-06-20", True, None),
        ("2026-06-26", True, None),
        # No / unusable deadline carries no forward signal (never raises):
        (None, False, None),                # no due_date key
        ("sometime", False, None),          # non-ISO free text
        ("2026-13-40", False, None),        # ISO-shaped but impossible calendar date
    ],
)
def test_classify_item_truth_table(due_date, done, expected):
    """classify_item flags only OPEN, dated, in-window items; everything else is None.

    Why this matters: this one table is the contract the whole forward layer rests on.
    The today and +7 boundaries decide the at-risk set; done/missing/garbage all collapse
    to None so they render exactly like a plain item and never crash a render.
    """
    assert classify_item(_item(due_date=due_date, done=done), _TODAY) == expected


def test_due_soon_default_horizon_is_seven_days():
    """The default due-soon window is 7 days (the documented rung-1 constant).

    Why this matters: the horizon is a stated default with a per-project knob deferred;
    pinning it guards against an accidental drift in the constant.
    """
    assert DUE_SOON_DAYS == 7


def test_due_soon_days_is_configurable_via_parameter():
    """A custom due_soon_days widens/narrows the window (the seam for per-project config).

    Why this matters: rung 1 ships only the default, but the parameter IS the seam a later
    per-project setting plugs into — so we prove a non-default horizon already works.
    """
    item = _item(due_date="2026-07-04")  # +8 days: out for N=7, in for N=10
    assert classify_item(item, _TODAY, due_soon_days=7) is None
    assert classify_item(item, _TODAY, due_soon_days=10) == "due_soon"


# --- count_at_risk(): the badge's aggregate -----------------------------------------


def test_count_at_risk_counts_open_overdue_and_due_soon_only():
    """count_at_risk tallies overdue ∪ due_soon, ignoring done / far-future / undated.

    Why this matters: the portfolio "N at risk" badge is this count. It must agree with
    the per-item classification (same function), so a mixed list yields exactly the number
    of items the per-item render would flag.
    """
    items = [
        _item("2026-06-20"),               # overdue
        _item("2026-06-28"),               # due soon
        _item("2026-12-01"),               # far future → not at risk
        _item("2026-06-01", done=True),    # done → not at risk
        _item(),                           # no deadline → not at risk
    ]
    assert count_at_risk(items, _TODAY) == 2


def test_count_at_risk_handles_none_and_empty():
    """A None or empty checklist counts as zero at-risk items, not an error.

    Why this matters: a report-only project has no checklist (None) and an enabled-but-empty
    one has []. Both must yield 0 so the badge is simply omitted, never a crash.
    """
    assert count_at_risk(None, _TODAY) == 0
    assert count_at_risk([], _TODAY) == 0


# --- is_slipping() / slipping_item_keys(): the history signal (E2 Inc 3 Unit 4) ------


def _o(due_date=None, done=False, at="2026-06-20T00:00:00+00:00", item_key="A"):
    """Build one observation row (observed_history's shape)."""
    return {"item_key": item_key, "due_date": due_date, "done": done, "observed_at": at}


def test_is_slipping_when_deadline_moved_later():
    """An open item whose deadline was postponed across observations is slipping.

    Why this matters: this is the primary, history-only signal — and exactly what the rung's
    eyes-on exercises ("push again with a moved deadline"). The later observation's due_date
    is after the earlier one, with the item still open.
    """
    obs = [_o("2026-07-01"), _o("2026-07-10")]
    assert is_slipping(obs, _TODAY) is True


def test_is_slipping_when_lingering_open_past_due():
    """An open item observed across ≥2 pushes and still past its deadline is slipping.

    Why this matters: the second arm — an item that has SAT open past its date across pushes,
    not one that only just became overdue (which is Unit 2's at-risk). Requires history (≥2
    observations), so a brand-new overdue item is not yet "slipping".
    """
    obs = [_o("2026-06-10"), _o("2026-06-10")]  # overdue at both, still open
    assert is_slipping(obs, _TODAY) is True


def test_not_slipping_when_deadline_stable_and_in_future():
    """A steady future deadline is not slipping, however many times it is observed.

    Why this matters: slipping must not fire on a healthy item — only a moved or lapsed
    deadline counts, so a stable upcoming date stays quiet.
    """
    obs = [_o("2026-07-10"), _o("2026-07-10")]
    assert is_slipping(obs, _TODAY) is False


def test_not_slipping_when_done_even_if_deadline_moved():
    """A DONE item never slips, even if its deadline had moved later along the way.

    Why this matters: the work landed; a bumpy path to completion is history, not a current
    risk. The latest observation being done short-circuits the signal.
    """
    obs = [_o("2026-07-01"), _o("2026-07-10", done=True)]
    assert is_slipping(obs, _TODAY) is False


def test_not_slipping_with_a_single_observation():
    """One observation carries no history, so nothing can be slipping yet.

    Why this matters: both arms require a prior observation — slipping means "going wrong
    over time". A just-created overdue item (one push) is at-risk (Unit 2), not slipping.
    """
    assert is_slipping([_o("2026-06-10")], _TODAY) is False  # overdue, but no history


def test_not_slipping_with_no_deadlines_or_empty():
    """No parseable deadlines (or no observations) → not slipping, never an error.

    Why this matters: an item that never carried a deadline has no slip signal, and an empty
    history must degrade cleanly to False rather than raising mid-render.
    """
    assert is_slipping([_o(None), _o(None)], _TODAY) is False
    assert is_slipping([], _TODAY) is False


def test_slipping_item_keys_groups_history_by_key():
    """slipping_item_keys folds a project's interleaved log per item_key, returns the slippers.

    Why this matters: the project's observed_history interleaves all items' rows. This groups
    them and runs is_slipping per key, so both the per-item marker and the portfolio count
    read ONE answer. Here A is postponed (slipping), B is a steady future date (not).
    """
    observations = [
        _o("2026-07-01", item_key="A"),
        _o("2026-07-10", item_key="B"),
        _o("2026-07-09", item_key="A"),  # A's deadline moved later → slipping
        _o("2026-07-10", item_key="B"),  # B steady, future → not
    ]
    assert slipping_item_keys(observations, _TODAY) == {"A"}


# --- milestones(): per-group roll-up (E2 Inc 3 Unit 5) -------------------------------


def _gi(group=None, done=False, due_date=None, text="task"):
    """Build a checklist item dict, attaching group/due_date only when given.

    Why: mirrors the producer's optional-field shape — an ungrouped or undated item has no
    such key at all — so the tests exercise the real "key absent" path, like _item above.
    """
    item = {"text": text, "done": done}
    if group is not None:
        item["group"] = group
    if due_date is not None:
        item["due_date"] = due_date
    return item


def test_milestones_groups_progress_and_order():
    """Items roll up per group, in first-seen order, with done/total progress.

    Why this matters: a milestone is a group of items; the view shows "M/N done" per group
    and must list groups in the document's own order (applications before to-dos), so the
    first appearance of each group fixes its position.
    """
    checklist = [
        _gi(group="Applications", done=True, text="App A"),
        _gi(group="Applications", done=False, text="App B"),
        _gi(group="Non-Application To-Do", done=False, text="Chore"),
        _gi(group="Applications", done=False, text="App C"),  # back to first group
    ]
    result = milestones(checklist, _TODAY)
    assert [(m["group"], m["done"], m["total"]) for m in result] == [
        ("Applications", 1, 3),
        ("Non-Application To-Do", 0, 1),
    ]


def test_milestones_nearest_due_is_soonest_open_deadline():
    """nearest_due is the earliest OPEN item's deadline; a done item never sets it.

    Why this matters: the milestone's headline date is "what's due next" for outstanding
    work, so a finished item's (earlier) deadline must not win — and the comparison must be
    calendar-correct, returning the original ISO string.
    """
    checklist = [
        # A done item with the earliest date — must be ignored for nearest_due.
        _gi(group="G", done=True, due_date="2026-06-01"),
        _gi(group="G", done=False, due_date="2026-08-15"),
        _gi(group="G", done=False, due_date="2026-07-04"),  # the soonest OPEN one
    ]
    assert milestones(checklist, _TODAY)[0]["nearest_due"] == "2026-07-04"


def test_milestones_nearest_due_none_when_no_open_deadline():
    """A group whose open items carry no parseable deadline has nearest_due None.

    Why this matters: the live to-do tables use year-less dates that parse to None, so a real
    milestone often has progress but no date — it must report None, not crash or guess.
    """
    checklist = [
        _gi(group="G", done=False),  # no due_date
        _gi(group="G", done=False, due_date="not-a-date"),  # unparseable → skipped
        _gi(group="G", done=True, due_date="2026-07-04"),  # done → ignored
    ]
    assert milestones(checklist, _TODAY)[0]["nearest_due"] is None


def test_milestones_at_risk_rollup_matches_count_at_risk():
    """A milestone's at_risk count is the group's overdue ∪ due-soon open items.

    Why this matters: the roll-up reuses count_at_risk so a milestone, the per-item render,
    and the card badge can never disagree on "at risk". Here one item is overdue and one is
    safely in the future, so exactly one is at risk.
    """
    checklist = [
        _gi(group="G", done=False, due_date="2026-06-01"),  # overdue (before _TODAY)
        _gi(group="G", done=False, due_date="2026-12-31"),  # far future → not at risk
    ]
    assert milestones(checklist, _TODAY)[0]["at_risk"] == 1


def test_milestones_excludes_ungrouped_items():
    """Items with no group contribute no milestone (and don't crash).

    Why this matters: a plain checkbox list (tasks collector) tags no groups, so it must
    yield no milestones — the feature degrades to nothing rather than inventing a bucket.
    """
    checklist = [
        _gi(done=False, text="ungrouped 1"),  # no group key
        _gi(group="", done=False, text="empty group"),  # falsy group → excluded too
        _gi(group="Real", done=False, text="grouped"),
    ]
    result = milestones(checklist, _TODAY)
    assert [m["group"] for m in result] == ["Real"]


def test_milestones_empty_when_nothing_grouped_or_none():
    """No grouped items, an empty checklist, or None all yield [].

    Why this matters: the renderer omits the Milestones section on []. A project without a
    structured tracker (or with no checklist at all) must take that path, never a half-built
    section.
    """
    assert milestones(None, _TODAY) == []
    assert milestones([], _TODAY) == []
    assert milestones([_gi(done=False, text="ungrouped")], _TODAY) == []


# --- bucket_counts(): the segmented-bar partition (E2 Inc 4) -------------------------


def test_bucket_counts_partitions_into_four_that_tile_the_total():
    """overdue / due_soon / remaining-open / done sum to the item total.

    Why this matters: the tracker card's segmented bar needs disjoint widths that fill the
    whole bar. The four buckets must tile every item exactly once — done items are never at
    risk, and `remaining` is the open-but-not-soon complement.
    """
    items = [
        _item(due_date="2026-06-20"),                 # overdue (past)
        _item(due_date="2026-06-26"),                 # due_soon (today, end-of-day)
        _item(due_date="2026-06-30"),                 # due_soon (+4 days)
        _item(due_date="2026-09-01"),                 # remaining (far future, open)
        _item(text="no-date"),                        # remaining (open, undated)
        _item(done=True, due_date="2026-06-20"),      # done (never at risk)
    ]
    counts = bucket_counts(items, _TODAY)
    assert counts == {"overdue": 1, "due_soon": 2, "remaining": 2, "done": 1}
    assert sum(counts.values()) == len(items)


def test_bucket_counts_empty_and_none_are_all_zero():
    """No items → every bucket 0 (so an empty tracker draws an empty bar, not a crash)."""
    zero = {"overdue": 0, "due_soon": 0, "remaining": 0, "done": 0}
    assert bucket_counts([], _TODAY) == zero
    assert bucket_counts(None, _TODAY) == zero


def test_bucket_counts_agrees_with_count_at_risk():
    """overdue + due_soon equals count_at_risk over the same items (one truth-table)."""
    items = [
        _item(due_date="2026-06-20"),
        _item(due_date="2026-06-29"),
        _item(done=True),
        _item(text="undated"),
    ]
    counts = bucket_counts(items, _TODAY)
    assert counts["overdue"] + counts["due_soon"] == count_at_risk(items, _TODAY)


# --- next_open_due(): the project-wide nearest open deadline (E2 Inc 4) --------------


def test_next_open_due_returns_soonest_open_deadline_across_all_items():
    """The earliest OPEN item's due_date wins, regardless of group or list order."""
    items = [
        _item(due_date="2026-07-10"),
        _item(due_date="2026-06-28"),  # soonest open
        _item(due_date="2026-06-25", done=True),  # done → ignored even though earlier
    ]
    assert next_open_due(items) == "2026-06-28"


def test_next_open_due_is_none_when_nothing_open_is_dated():
    """No open dated item → None (the header omits NEXT DUE), for empty/None too."""
    assert next_open_due([_item(done=True, due_date="2026-06-20"), _item(text="x")]) is None
    assert next_open_due([]) is None
    assert next_open_due(None) is None

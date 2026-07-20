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
    effective_checklist,
    fold_producer_checklists,
    is_slipping,
    item_key,
    merge_producer_checklists,
    milestones,
    next_open_due,
    slipping_item_keys,
    slipping_item_keys_by_author,
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


def _o(due_date=None, done=False, at="2026-06-20T00:00:00+00:00", item_key="A", author_id=None):
    """Build one observation row (observed_history's shape).

    author_id defaults to None so every existing single-stream test keeps exercising the
    legacy "one anonymous stream" path (which must stay byte-identical); the per-producer
    tests below pass it explicitly.
    """
    return {
        "item_key": item_key,
        "due_date": due_date,
        "done": done,
        "observed_at": at,
        "author_id": author_id,
    }


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


# --- slipping_item_keys_by_author(): per-producer stream partition (C3 Inc 2.5) ------
#
# The corruption this fixes: is_slipping reads an item's deadline history as ONE ordered
# stream, so two machines pushing the same item interleave into a stream neither produced.
# Partitioning by author before running is_slipping is the fix.


def test_by_author_fixes_a_false_postponement_from_interleaved_streams():
    """Two producers, each with a STABLE (but different) deadline, must not look postponed.

    Scenario: machine A always reports item X due 2026-07-01; machine B always reports the
    SAME item due 2026-07-10. Their pushes interleave in the log as [07-01, 07-10, 07-01,
    07-10]. Collapsed into one stream that reads as "deadline moved later" (07-01 → 07-10) — a
    postponement NEITHER machine made. Partitioned by author, each stream is a flat line, so X
    is slipping for no one. We assert both the per-author result AND that the OLD collapsed
    view (dropping author_id) would have wrongly flagged it — pinning the bug being fixed.
    """
    observations = [
        _o("2026-07-01", at="2026-06-20T00:00:00+00:00", item_key="X", author_id=1),
        _o("2026-07-10", at="2026-06-21T00:00:00+00:00", item_key="X", author_id=2),
        _o("2026-07-01", at="2026-06-22T00:00:00+00:00", item_key="X", author_id=1),
        _o("2026-07-10", at="2026-06-23T00:00:00+00:00", item_key="X", author_id=2),
    ]
    # Partitioned: neither producer's own stream moved → X slips for no one.
    assert slipping_item_keys_by_author(observations, _TODAY) == {}
    assert slipping_item_keys(observations, _TODAY) == set()
    # Sanity: the OLD collapsed behavior (ignore author_id → one stream) DID false-flag X.
    collapsed = [{**o, "author_id": None} for o in observations]
    assert slipping_item_keys(collapsed, _TODAY) == {"X"}


def test_by_author_keeps_a_real_per_stream_postponement():
    """A genuine postponement in one producer's stream is attributed to that producer only.

    Scenario: machine A postpones item X (07-01 → 07-10); machine B keeps X steady. X is
    slipping in A's stream, not B's — and appears in the global union. This is the signal the
    partition must NOT lose while it drops the false positives above.
    """
    observations = [
        _o("2026-07-01", at="2026-06-20T00:00:00+00:00", item_key="X", author_id=1),
        _o("2026-07-10", at="2026-06-21T00:00:00+00:00", item_key="X", author_id=2),
        _o("2026-07-10", at="2026-06-22T00:00:00+00:00", item_key="X", author_id=1),  # A moved it later
        _o("2026-07-10", at="2026-06-23T00:00:00+00:00", item_key="X", author_id=2),  # B steady
    ]
    by_author = slipping_item_keys_by_author(observations, _TODAY)
    assert by_author == {1: {"X"}}  # only producer 1's stream slipped
    assert slipping_item_keys(observations, _TODAY) == {"X"}  # union still flags X


def test_slipping_item_keys_is_the_union_of_the_per_author_sets():
    """The global function equals the union over all producers' streams.

    Scenario: producer 1 slips item A (postponed), producer 2 slips item B (postponed); nobody
    slips C. The union is {A, B}, and each is attributed to its own producer's stream.
    """
    observations = [
        _o("2026-07-01", at="2026-06-20T00:00:00+00:00", item_key="A", author_id=1),
        _o("2026-07-10", at="2026-06-21T00:00:00+00:00", item_key="A", author_id=1),  # 1 postpones A
        _o("2026-07-01", at="2026-06-20T00:00:00+00:00", item_key="B", author_id=2),
        _o("2026-07-10", at="2026-06-21T00:00:00+00:00", item_key="B", author_id=2),  # 2 postpones B
        _o("2026-07-10", at="2026-06-20T00:00:00+00:00", item_key="C", author_id=1),
        _o("2026-07-10", at="2026-06-21T00:00:00+00:00", item_key="C", author_id=1),  # C steady
    ]
    by_author = slipping_item_keys_by_author(observations, _TODAY)
    assert by_author == {1: {"A"}, 2: {"B"}}
    assert slipping_item_keys(observations, _TODAY) == {"A", "B"}


def test_null_author_rows_form_one_anonymous_stream():
    """Legacy all-NULL-author data collapses to a single stream — byte-identical to before.

    Why this matters: every project that existed before this slice has author_id NULL on every
    row, so the partition must reproduce the old per-item grouping exactly (one None stream).
    Here A is postponed, B steady → {None: {"A"}}, union {"A"} — the same answer the pre-2.5
    slipping_item_keys returned.
    """
    observations = [
        _o("2026-07-01", item_key="A"),  # author_id defaults to None
        _o("2026-07-09", at="2026-06-21T00:00:00+00:00", item_key="A"),
        _o("2026-07-10", item_key="B"),
        _o("2026-07-10", at="2026-06-21T00:00:00+00:00", item_key="B"),
    ]
    assert slipping_item_keys_by_author(observations, _TODAY) == {None: {"A"}}
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


# --- item_key(): the shared identity rule (C3 Inc 2.5) -------------------------------


def test_item_key_prefers_key_then_falls_back_to_text():
    """`key` wins when present/truthy; otherwise `text` — the one identity rule the merge,
    the observation stream, and the serializers all share (they must never diverge)."""
    assert item_key({"key": "app-1", "text": "Apply to Foo", "done": False}) == "app-1"
    assert item_key({"text": "Write tests", "done": False}) == "Write tests"
    # An empty-string key is falsy → falls through to text (matches `x.get("key") or x[...]`).
    assert item_key({"key": "", "text": "Fallback", "done": False}) == "Fallback"


# --- merge_producer_checklists() / effective_checklist(): the KI-30 fold --------------
#
# The merge is the correctness core of Unit 1.1: two machines each push their own copy of
# the SAME base checklist, and the badge must show the union with done OR-ed so a stale
# copy can never regress a done item. Each test below pins one settled rule.


def _producer(items, updated_at):
    """Build one producer's checklist copy as producer_checklists_for returns it.

    Why: the merge only reads `items` and `updated_at`; author fields are irrelevant to the
    fold, so the fixture omits them to keep each case focused on the merge behavior.
    """
    return {"items": items, "updated_at": updated_at}


def test_merge_unions_distinct_items_in_first_seen_order():
    """Items only one producer has still appear; order is first-seen across the fold.

    Scenario: producer A (earlier push) tracks task-1; producer B (later push) tracks
    task-2. The effective list is their union, A's item first (A folded first). This is why
    a teammate's project-specific item is not dropped just because the other machine lacks it.
    """
    a = _producer([_item(text="task-1")], "2026-06-25T10:00:00Z")
    b = _producer([_item(text="task-2")], "2026-06-26T10:00:00Z")
    merged = merge_producer_checklists([a, b])
    assert [item_key(i) for i in merged] == ["task-1", "task-2"]


def test_merge_ors_done_across_producer_copies():
    """One producer done + the other not → the merged item is done (the KI-30 fix).

    Scenario: A marked "Ship" done; B's copy still shows it open. done = OR means the badge
    counts it done — the whole point of the merge (an aggregate would show whoever pushed last).
    """
    a = _producer([_item(text="Ship", done=True)], "2026-06-25T10:00:00Z")
    b = _producer([_item(text="Ship", done=False)], "2026-06-26T10:00:00Z")
    (merged_item,) = merge_producer_checklists([a, b])
    assert merged_item["done"] is True


def test_merge_stale_not_done_copy_never_regresses_a_done_item():
    """A LATER push that still shows the item open cannot flip a done item back open.

    Scenario: A's cron pushed "done" at 10:00; B's stale cron pushes the same item "not done"
    at 11:00 (a later updated_at). Latest-writer-wins would regress it — exactly KI-30's
    flicker. done = OR keeps it done regardless of which copy pushed last.
    """
    a = _producer([_item(text="Ship", done=True)], "2026-06-26T10:00:00Z")
    b_stale = _producer([_item(text="Ship", done=False)], "2026-06-26T11:00:00Z")
    (merged_item,) = merge_producer_checklists([a, b_stale])
    assert merged_item["done"] is True


def test_merge_metadata_comes_from_the_most_recently_updated_copy():
    """Non-done fields (text/due_date) come from the producer with the later updated_at.

    Scenario: both track the same item (same key), but B re-dated it later. B's push is more
    recent, so the merged item carries B's due_date — a genuine re-dating is honored, while
    done stays OR-ed. The fold walks ascending by updated_at, so the last writer wins metadata.
    """
    a = _producer(
        [{"key": "m1", "text": "Milestone", "done": False, "due_date": "2026-07-01"}],
        "2026-06-25T10:00:00Z",
    )
    b = _producer(
        [{"key": "m1", "text": "Milestone", "done": False, "due_date": "2026-07-15"}],
        "2026-06-26T10:00:00Z",
    )
    (merged_item,) = merge_producer_checklists([a, b])
    assert merged_item["due_date"] == "2026-07-15"  # B's later value


def test_merge_metadata_precedence_is_by_timestamp_not_argument_order():
    """Even if the later-updated producer is passed FIRST, its metadata still wins.

    Why: the fold sorts by updated_at, so precedence is timestamp-driven and independent of
    the order producer_checklists_for happened to return the rows (which is by name).
    """
    later = _producer(
        [{"key": "m1", "text": "Milestone", "done": False, "due_date": "2026-07-15"}],
        "2026-06-26T10:00:00Z",
    )
    earlier = _producer(
        [{"key": "m1", "text": "Milestone", "done": False, "due_date": "2026-07-01"}],
        "2026-06-25T10:00:00Z",
    )
    (merged_item,) = merge_producer_checklists([later, earlier])  # later passed first
    assert merged_item["due_date"] == "2026-07-15"


def test_effective_checklist_merges_only_at_two_or_more_producers():
    """≥2 producers → merged; 0 or 1 → the aggregate list UNCHANGED (byte-identical fallback).

    This gate is what keeps every current single-producer / anonymous deployment untouched:
    with fewer than two identified producers there is nothing to merge, so the aggregate
    (which those deployments still write) is returned exactly as-is.
    """
    aggregate = [_item(text="Ship", done=False)]
    one = _producer([_item(text="Ship", done=True)], "2026-06-26T10:00:00Z")
    # 1 producer: fallback returns the aggregate object unchanged (still shows not-done).
    assert effective_checklist(aggregate, [one]) is aggregate
    # 0 producers: same fallback.
    assert effective_checklist(aggregate, []) is aggregate
    # 2 producers: now the merge runs and OR-s done → the item reads done.
    two = _producer([_item(text="Ship", done=False)], "2026-06-27T10:00:00Z")
    assert effective_checklist(aggregate, [one, two])[0]["done"] is True


# --- Unit 4b: operator-folding (agents fold into the human they act for) --------------
#
# An agent is a machine acting on a person's behalf, so the project page shows ONE card per
# PERSON: a human plus their two agents is one contributor, not three. These tests pin the
# fold itself; the crucial "slippage still derives on RAW streams" half is pinned in
# test_relay_api.py, where the observation streams actually exist.


def _identified_producer(author_id, author_name, items, updated_at, operator=None):
    """Build a producer row as producer_checklists_for returns it (Unit 4b shape).

    Args:
        operator: (id, name) of the operating human when this row is an AGENT, else None.

    Why: the fold reads `effective_producer_id`/`effective_producer_name`, which the store
    resolves as `operated_by ?? author_id`. Passing a human's own identity for a non-agent
    is what makes "no agents behaves exactly as before" testable in one fixture.
    """
    operator_id, operator_name = operator if operator else (author_id, author_name)
    return {
        "author_id": author_id,
        "author_name": author_name,
        "items": items,
        "updated_at": updated_at,
        "effective_producer_id": operator_id,
        "effective_producer_name": operator_name,
    }


def test_fold_groups_an_agent_into_its_operator_as_one_card():
    """A human + their agent produce ONE card, named for the human, carrying both raw ids.

    This is the core of the unit: agents act on their operator's behalf, so work-tracking
    groups by person. Both raw author_ids must survive on the row — the caller needs them to
    look up each stream's slippage, which is never merged.
    """
    human = _identified_producer(1, "yoo", [_item(text="Ship", done=False)], "2026-06-26T10:00:00Z")
    agent = _identified_producer(
        2, "claude-mac", [_item(text="Ship", done=True)], "2026-06-27T10:00:00Z",
        operator=(1, "yoo"),
    )

    (card,) = fold_producer_checklists([human, agent])
    assert card["author_name"] == "yoo"  # the person, not the machine
    assert sorted(card["author_ids"]) == [1, 2]  # provenance carried forward
    # Within the group the existing done-OR merge applies: the agent's done wins.
    assert card["items"][0]["done"] is True
    # The group's freshest push, for the cross-producer fold that may follow.
    assert card["updated_at"] == "2026-06-27T10:00:00Z"


def test_fold_groups_two_agents_of_one_operator_into_a_single_card():
    """Two agents under one human still yield ONE card — the count is people, not machines."""
    human = _identified_producer(1, "yoo", [_item(text="A", done=False)], "2026-06-26T10:00:00Z")
    agent_a = _identified_producer(
        2, "claude-mac", [_item(text="B", done=True)], "2026-06-27T10:00:00Z", operator=(1, "yoo")
    )
    agent_b = _identified_producer(
        3, "codex", [_item(text="C", done=True)], "2026-06-28T10:00:00Z", operator=(1, "yoo")
    )

    cards = fold_producer_checklists([human, agent_a, agent_b])
    assert len(cards) == 1
    assert sorted(cards[0]["author_ids"]) == [1, 2, 3]
    assert {i["text"] for i in cards[0]["items"]} == {"A", "B", "C"}


def test_fold_keeps_unrelated_producers_apart():
    """Two humans stay two cards — folding must group by operator, not collapse everything."""
    a = _identified_producer(1, "yoo", [_item(text="A")], "2026-06-26T10:00:00Z")
    b = _identified_producer(2, "teammate", [_item(text="B")], "2026-06-27T10:00:00Z")

    cards = fold_producer_checklists([a, b])
    assert [c["author_name"] for c in cards] == ["yoo", "teammate"]  # input (name) order kept


def test_fold_leaves_a_lone_producers_items_untouched():
    """A group of one passes its items through by identity — nothing to reconcile.

    Why this matters: the overwhelmingly common no-agents case must not acquire merge
    semantics (or the merge's `updated_at` requirement) just because folding now runs.
    """
    items = [_item(text="Ship", done=False)]
    lone = _identified_producer(1, "yoo", items, "2026-06-26T10:00:00Z")
    (card,) = fold_producer_checklists([lone])
    assert card["items"] is items


def test_the_merge_gate_still_counts_copies_not_people_when_an_agent_folds():
    """A human + their agent still MERGE, even though they render as one card.

    This pins the deliberate deviation from amendment 4 (decided on measured evidence).
    Amendment 4 wanted this gate to count distinct effective producers, so a human+agent
    pair would skip the merge. Doing that fell back to the last-writer-wins aggregate, which
    DROPS whatever items the other producer holds and flips `done` by push order — KI-30's
    flicker, reintroduced. The merge reconciles COPIES; a human and their agent genuinely
    hold two, either of which may be stale.

    Folding and this gate answer different questions, and both must hold at once: ONE card
    (people) AND a merged badge (copies).
    """
    aggregate = [_item(text="Ship", done=False), _item(text="Human-only", done=False)]
    human = _identified_producer(
        1, "yoo",
        [_item(text="Ship", done=False), _item(text="Human-only", done=False)],
        "2026-06-26T10:00:00Z",
    )
    agent = _identified_producer(
        2, "claude-mac",
        [_item(text="Ship", done=True), _item(text="Agent-only", done=True)],
        "2026-06-27T10:00:00Z", operator=(1, "yoo"),
    )

    merged = effective_checklist(aggregate, [human, agent])
    by_text = {i["text"]: i for i in merged}
    # Nothing is lost: both producers' items survive...
    assert set(by_text) == {"Ship", "Human-only", "Agent-only"}
    # ...and the agent's completion OR-s over the human's stale not-done copy.
    assert by_text["Ship"]["done"] is True

    # The two questions stay separate: still ONE card for the person.
    assert len(fold_producer_checklists([human, agent])) == 1


def test_effective_checklist_gate_is_unchanged_for_identity_less_rows():
    """Rows carrying no identity behave exactly as before 4b — the gate is untouched."""
    aggregate = [_item(text="Ship", done=False)]
    one = _producer([_item(text="Ship", done=True)], "2026-06-26T10:00:00Z")
    two = _producer([_item(text="Ship", done=False)], "2026-06-27T10:00:00Z")
    assert effective_checklist(aggregate, [one, two])[0]["done"] is True
    assert effective_checklist(aggregate, [one]) is aggregate


def test_effective_checklist_preserves_none_aggregate_in_the_fallback():
    """A checklist-less project (aggregate None) stays None under the <2 fallback.

    Why this matters: the project route's existence-hiding 404 tests `checklist is None`, so
    the fallback must not turn a missing checklist into [] (which would read as "exists").
    """
    assert effective_checklist(None, []) is None


def test_merge_handles_empty_producer_items():
    """A producer whose checklist is empty contributes nothing; the union is the other's.

    Scenario: B enabled the checklist but has no items yet (a legitimate empty push). The
    merge must not crash and must yield A's items unchanged.
    """
    a = _producer([_item(text="task-1")], "2026-06-25T10:00:00Z")
    b_empty = _producer([], "2026-06-26T10:00:00Z")
    merged = merge_producer_checklists([a, b_empty])
    assert [item_key(i) for i in merged] == ["task-1"]

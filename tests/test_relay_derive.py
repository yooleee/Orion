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

from relay.derive import DUE_SOON_DAYS, classify_item, count_at_risk, today_in_tz

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

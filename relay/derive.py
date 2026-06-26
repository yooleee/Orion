# =============================================================================
# relay/derive.py
# -----------------------------------------------------------------------------
# Responsible for: PURE forward-looking derivation over the live checklist — turning
#                  each item's `due_date` into a status (overdue / due_soon) relative
#                  to "today" in the relay's display timezone. No I/O, no HTTP, no
#                  database: just deterministic functions of (items, today).
# Role in project: The "derive" layer of E2 Inc 3 (forward-looking layer). store.py
#                  reads the items, render.py presents them, and this module is the
#                  thin, testable middle that decides what is at risk. Keeping it pure
#                  and separate (vs. inlining the comparison in render) makes the
#                  truth-table directly unit-testable with a fixed `today`, and keeps
#                  the store/render layers free of date logic.
# Observe, don't originate: this is a DOWNSTREAM PROJECTION of deadlines the source
#                  docs already stated (parsed by the local tracker collector). It
#                  authors no forward facts — it only reframes what was observed.
# Assumptions: a checklist item is a {"text", "done"[, "due_date"]} dict; due_date,
#                  when present, is an ISO "YYYY-MM-DD" string. A date-only deadline
#                  is treated as END-OF-DAY in the display zone, so a deadline of
#                  "today" is due_soon (not yet overdue) until today ends.
# =============================================================================

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Default "due soon" horizon: an open deadline within this many days of today
# (inclusive) is at risk. A module constant for this rung; a per-project
# `due_soon_days` knob is a later add — the function parameter below IS the seam, so
# adding config later is additive and touches no call site's shape.
DUE_SOON_DAYS = 7

# The status strings this module emits. Kept as named constants so render.py can map
# them to CSS classes without restating the literals (one source of truth for the set).
OVERDUE = "overdue"
DUE_SOON = "due_soon"


def today_in_tz(tz: ZoneInfo, now: datetime | None = None) -> date:
    """Return the current calendar date as seen in the given timezone.

    Args:
        tz: The display timezone the relay renders in (e.g. America/Los_Angeles).
        now: An explicit "current instant" (timezone-aware). Defaults to the real UTC
            now. Injected by tests to pin a deterministic "today".

    Returns:
        The local calendar date in `tz` (a datetime.date).

    Why:
        "Overdue" only means anything relative to a specific day, and the relay shows
        ONE fixed zone (KI-20) so every viewer sees the same "today" regardless of where
        they or the server sit. We derive the date from an absolute UTC instant converted
        into `tz` — never an argless `datetime.now()`/naive local time — so the result is
        deterministic and the `now` seam keeps it testable. Near midnight the same instant
        is a different date in different zones, which is exactly why we convert before
        taking .date().
    """
    if now is None:
        # Timezone-AWARE UTC now (never the argless naive now): the only non-deterministic
        # input, isolated here behind the `now` seam so every caller below stays pure.
        now = datetime.now(timezone.utc)
    return now.astimezone(tz).date()


def classify_item(item: dict, today: date, due_soon_days: int = DUE_SOON_DAYS) -> str | None:
    """Classify one checklist item's deadline as overdue / due_soon / neither.

    Args:
        item: A checklist item dict ({"text", "done"[, "due_date"]}).
        today: The reference date (already computed in the display zone).
        due_soon_days: How many days ahead still counts as "due soon" (inclusive).

    Returns:
        OVERDUE when the item is OPEN and its deadline is strictly before today;
        DUE_SOON when OPEN and the deadline is between today and today+due_soon_days
        (inclusive); None otherwise — including a done item, a missing due_date, or an
        unparseable one.

    Why:
        This is the whole truth-table in one place. A DONE item is never at risk no
        matter its date (the work is finished), and an item with no parseable deadline
        carries no forward signal — both return None so they render exactly as before.
        We re-parse defensively (rather than trust the producer) so a malformed value
        from any source degrades to "no signal" instead of raising mid-render. Date-only
        deadlines are end-of-day in the zone, so `due == today` is DUE_SOON, not overdue:
        the day is not yet over.
    """
    if item.get("done"):
        return None
    raw = item.get("due_date")
    if not raw:
        return None
    try:
        due = date.fromisoformat(raw)
    except (ValueError, TypeError):
        # A non-ISO / non-string value carries no usable deadline; treat it as "no
        # signal" rather than an error (mirrors the local parser's never-fail stance).
        return None
    if due < today:
        return OVERDUE
    if due <= today + timedelta(days=due_soon_days):
        return DUE_SOON
    return None


def count_at_risk(items: list[dict] | None, today: date, due_soon_days: int = DUE_SOON_DAYS) -> int:
    """Count the checklist items that are overdue or due soon.

    Args:
        items: The checklist items (or None).
        today: The reference date (display zone).
        due_soon_days: The "due soon" horizon (inclusive).

    Returns:
        The number of items classify_item() flags (overdue ∪ due_soon). 0 for None/empty.

    Why:
        The portfolio card shows a single "N at risk" badge, so it needs the count, not
        the per-item detail. Built on classify_item so the badge and the per-item render
        can never disagree about what "at risk" means.
    """
    return sum(1 for item in (items or []) if classify_item(item, today, due_soon_days) is not None)

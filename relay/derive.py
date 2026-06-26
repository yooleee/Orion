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


def milestones(checklist: list[dict] | None, today: date) -> list[dict]:
    """Roll the live checklist up into per-group milestones.

    Args:
        checklist: The live checklist items ({"text", "done"[, "due_date"][, "group"]}),
            or None.
        today: The reference date (display zone), forwarded to count_at_risk.

    Returns:
        One dict per milestone group, in FIRST-SEEN order:
        {"group": str, "done": int, "total": int, "at_risk": int, "nearest_due": str|None}.
        `nearest_due` is the soonest OPEN item's due_date (ISO string) in the group, or None
        when no open item carries a parseable deadline. Items with no `group` are excluded;
        the result is [] when nothing is grouped (or the checklist is None/empty).

    Why:
        A milestone is just a GROUP of checklist items, and the grouping is supplied by the
        producer (the tracker tags items with a section `group`; a plain checkbox list tags
        none). So this layer only has to bucket by that field and summarize each bucket —
        progress (done/total), the nearest thing still due, and how many are at risk — which
        is what the dashboard's "Milestones" view and the portfolio "next milestone" hint
        read. First-seen order mirrors the checklist's own order (applications before the
        to-do tables), so the view matches the document. nearest_due looks only at OPEN items
        because a finished item's deadline is moot — the same reason count_at_risk and the
        per-item render ignore done items, keeping the three consistent. This OBSERVES the
        structure the source doc already has; it authors no grouping of its own.
    """
    if not checklist:
        return []

    # Bucket items by group, preserving first-seen group order. dict preserves insertion
    # order (Python 3.7+), so the first time a group appears fixes its position.
    groups: dict[str, list[dict]] = {}
    for item in checklist:
        group = item.get("group")
        if not group:  # None or "" → ungrouped, contributes no milestone
            continue
        groups.setdefault(group, []).append(item)

    result: list[dict] = []
    for group, items in groups.items():
        total = len(items)
        done = sum(1 for item in items if item.get("done"))
        # Reuse the at-risk rule (overdue ∪ due-soon, open items only) so a milestone's
        # roll-up can never disagree with the per-item treatment or the card's at-risk badge.
        at_risk = count_at_risk(items, today)
        result.append(
            {
                "group": group,
                "done": done,
                "total": total,
                "at_risk": at_risk,
                "nearest_due": _nearest_open_due(items),
            }
        )
    return result


def _nearest_open_due(items: list[dict]) -> str | None:
    """Return the soonest OPEN item's due_date (ISO string) in a group, or None.

    Args:
        items: The checklist items in one milestone group.

    Returns:
        The earliest parseable due_date among the group's OPEN items, as its original ISO
        "YYYY-MM-DD" string, or None when no open item carries a parseable deadline.

    Why:
        The milestone's headline date is "what is due next" for work still outstanding, so a
        done item never sets it (its deadline is moot). We compare as real dates (so ordering
        is calendar-correct, not lexical-by-accident) but return the ORIGINAL string, since
        the producer already normalized it to ISO and the renderer formats from that. Re-parse
        defensively: a malformed stored value is skipped rather than raising mid-derive,
        mirroring classify_item's never-fail stance.
    """
    best: date | None = None
    best_iso: str | None = None
    for item in items:
        if item.get("done"):
            continue  # an open milestone's next date comes from open work only
        raw = item.get("due_date")
        if not raw:
            continue
        try:
            due = date.fromisoformat(raw)
        except (ValueError, TypeError):
            continue  # a malformed date carries no usable deadline
        if best is None or due < best:
            best, best_iso = due, raw
    return best_iso


def is_slipping(observations: list[dict], today: date) -> bool:
    """Decide whether one item is SLIPPING from its observation history.

    Args:
        observations: One item_key's observation rows (observed_history's shape:
            {"due_date", "done", "observed_at"} dicts), OLDEST first.
        today: The reference date (display zone).

    Returns:
        True when the item is open (its latest observation is not done) AND either
        (a) its deadline moved LATER over time (postponed: the latest non-null due_date is
        after the earliest), or (b) it is "lingering past-due" — observed across at least
        two pushes and currently overdue (latest due_date before today). False otherwise,
        including a done item, a single-observation item, or one with no deadline history.

    Why:
        This is the signal Unit 3's history exists to support, and it is deliberately
        DISTINCT from Unit 2's at-risk: both arms require HISTORY (a prior observation),
        so slipping means "this has been going wrong over time," not just "it is due soon
        right now." A DONE item never slips (the work landed, however bumpy the path). The
        postponement arm is exactly what the rung's eyes-on exercises ("push again with a
        moved deadline"); the lingering arm catches an item that has sat open past its date
        across pushes rather than one that only just became overdue. Re-parse defensively so
        a malformed stored date degrades to "no signal" instead of raising mid-render.
    """
    if not observations:
        return False
    latest = observations[-1]
    if latest.get("done"):
        return False
    dues = []
    for obs in observations:
        raw = obs.get("due_date")
        if not raw:
            continue
        try:
            dues.append(date.fromisoformat(raw))
        except (ValueError, TypeError):
            continue  # a malformed stored date carries no usable signal
    # (a) Postponed: the deadline moved later across the item's life.
    if len(dues) >= 2 and dues[-1] > dues[0]:
        return True
    # (b) Lingering past-due: seen across >1 push and still open past its (latest) deadline.
    if len(observations) >= 2 and dues and dues[-1] < today:
        return True
    return False


def slipping_item_keys(observations: list[dict], today: date) -> set[str]:
    """Return the set of item_keys that are slipping, from a project's FULL history.

    Args:
        observations: A project's observed_history (all items' rows interleaved, oldest
            first), each carrying an "item_key".
        today: The reference date (display zone).

    Returns:
        The set of item_keys whose per-item history is_slipping() flags.

    Why:
        Both surfaces need the same answer, so this is the single place that groups a
        project's interleaved observation log by item_key and runs is_slipping over each
        group. The per-item render checks membership; the portfolio badge counts the set —
        so the indicator and the count can never disagree. Grouping preserves the oldest-
        first order (observed_history already sorts), which is what the postponement check
        relies on.
    """
    by_key: dict[str, list[dict]] = {}
    for obs in observations:
        by_key.setdefault(obs["item_key"], []).append(obs)
    return {key for key, history in by_key.items() if is_slipping(history, today)}

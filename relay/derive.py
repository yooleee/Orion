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


def bucket_counts(items: list[dict] | None, today: date, due_soon_days: int = DUE_SOON_DAYS) -> dict:
    """Partition a checklist into the four states the segmented progress bar shows.

    Args:
        items: The checklist items (or None).
        today: The reference date (display zone).
        due_soon_days: The "due soon" horizon (inclusive), forwarded to classify_item.

    Returns:
        A dict {"overdue", "due_soon", "remaining", "done"} of counts that SUM TO the
        item total: `done` counts finished items; `overdue`/`due_soon` are the open at-risk
        split from classify_item; `remaining` is every other open item (open, no near
        deadline). {0,0,0,0} for None/empty.

    Why:
        The home's tracker card draws a SEGMENTED bar — distinct widths for overdue vs.
        due-soon vs. the rest — which count_at_risk's single "N at risk" number cannot feed.
        This is the same truth-table as classify_item (so the segments and the per-item
        treatment can never disagree), just bucketed rather than summed. `remaining` is the
        complement (total - done - overdue - due_soon) so the four always tile the whole
        checklist, which is exactly what a stacked bar needs to fill its width.
    """
    items = items or []
    done = sum(1 for item in items if item.get("done"))
    overdue = due_soon = 0
    for item in items:
        state = classify_item(item, today, due_soon_days)
        if state == OVERDUE:
            overdue += 1
        elif state == DUE_SOON:
            due_soon += 1
    # The complement: open items carrying no near deadline. Derived (not counted) so the
    # four buckets provably tile the total — done items are never at risk, so they and the
    # at-risk split are disjoint and `remaining` is whatever is left.
    remaining = len(items) - done - overdue - due_soon
    return {"overdue": overdue, "due_soon": due_soon, "remaining": remaining, "done": done}


def next_open_due(checklist: list[dict] | None) -> str | None:
    """Return the soonest OPEN item's due_date (ISO string) across the whole checklist.

    Args:
        checklist: A project's live checklist items (or None).

    Returns:
        The earliest parseable due_date among ALL open items, as its original ISO
        "YYYY-MM-DD" string, or None when no open item carries a parseable deadline.

    Why:
        The project header's "NEXT DUE" is the single nearest outstanding deadline for the
        project as a whole — across every item, not bucketed by milestone group. It is the
        same "soonest open deadline" rule milestones use per group (_nearest_open_due), so
        this delegates to that one helper applied to the full list, keeping the project-wide
        figure and the per-group figures consistent. None when nothing open is dated, which
        the serializer reads as "omit NEXT DUE".
    """
    return _nearest_open_due(checklist or [])


def milestones(
    checklist: list[dict] | None, today: date, due_soon_days: int = DUE_SOON_DAYS
) -> list[dict]:
    """Roll the live checklist up into per-group milestones.

    Args:
        checklist: The live checklist items ({"text", "done"[, "due_date"][, "group"]}),
            or None.
        today: The reference date (display zone), forwarded to count_at_risk.
        due_soon_days: The "due soon" horizon (inclusive), forwarded to count_at_risk so a
            group's at-risk roll-up uses the SAME per-project horizon as the per-item render.

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
        at_risk = count_at_risk(items, today, due_soon_days)
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


def slipping_item_keys_by_author(
    observations: list[dict], today: date
) -> dict[str | None, set[str]]:
    """Group a project's observation log into PER-PRODUCER slipping sets (C3 Inc 2.5).

    Args:
        observations: A project's observed_history (all items' rows interleaved, oldest
            first), each carrying an "item_key" and an "author_id" (the producer whose push
            recorded it, or None for a legacy/anonymous push).
        today: The reference date (display zone).

    Returns:
        A dict mapping author_id -> the set of that producer's slipping item_keys. Only
        authors with at least one slipping item appear (consumers use `.get(author, set())`);
        legacy/anonymous rows accumulate under the None key.

    Why:
        is_slipping reads an item's deadline history as a single time-ordered stream, so two
        machines pushing the SAME item interleave their observations into one corrupted stream
        — machine A's earlier date followed by machine B's later date reads as a postponement
        that neither producer actually made, and one-push-each rows inflate the "lingering"
        arm. Partitioning by (author_id, item_key) BEFORE running the untouched is_slipping
        keeps each producer's stream intact, so a streak reflects one machine's real history.
        Every existing project has all-None author_id (the column predates any read), so it
        collapses to a single None stream — identical to the pre-partition behavior.
    """
    by_stream: dict[tuple[str | None, str], list[dict]] = {}
    for obs in observations:
        # A NULL author forms its own "anonymous" stream (the None key), so legacy data
        # (every row NULL) is one stream and reproduces the old collapsed result exactly.
        stream_key = (obs.get("author_id"), obs["item_key"])
        by_stream.setdefault(stream_key, []).append(obs)
    result: dict[str | None, set[str]] = {}
    for (author_id, item_key), history in by_stream.items():
        if is_slipping(history, today):
            result.setdefault(author_id, set()).add(item_key)
    return result


def slipping_item_keys(observations: list[dict], today: date) -> set[str]:
    """Return the set of item_keys that are slipping, from a project's FULL history.

    Args:
        observations: A project's observed_history (all items' rows interleaved, oldest
            first), each carrying an "item_key" (and an "author_id").
        today: The reference date (display zone).

    Returns:
        The union across all producers of each stream's slipping item_keys — the global
        "is this item slipping for anyone" answer.

    Why:
        The project-wide surfaces (aggregate checklist rows, milestone roll-ups, scheduling,
        the portfolio count) want one answer per item regardless of which producer's stream it
        slipped in, so this is the UNION of the per-author partition (C3 Inc 2.5). It stays the
        single source those surfaces share, so the per-item indicator and the count can never
        disagree. Deriving it from slipping_item_keys_by_author (rather than a second grouping)
        keeps the two definitions in lockstep.
    """
    return {
        key
        for keys in slipping_item_keys_by_author(observations, today).values()
        for key in keys
    }


# =============================================================================
# Effective checklist (C3 Inc 2.5, Unit 1.1) — merge per-producer checklist copies
# into the single displayed badge/progress. See docs/per-producer-consolidation-kickoff.md.
# Pure, like the rest of this module: a deterministic fold over already-decoded item
# dicts, no I/O. store.py fetches the aggregate + per-producer rows and calls in.
# =============================================================================


def item_key(item: dict) -> str:
    """Return a checklist item's stable identity: its `key`, else its `text`.

    Args:
        item: A decoded checklist item dict — {"text", "done"[, "due_date"][, "key"]
            [, "group"]}. `key` is optional; `text` is required.

    Returns:
        The item's `key` when present and truthy, else its `text` (str).

    Why:
        This is the ONE definition of item identity the whole codebase shares:
        record_observations stamps it, the slipping lookup matches on it, and the
        producer-checklist merge below unions on it. Hoisting it here (api.py's
        `_item_key` now delegates) means a single source of truth — if the rule ever
        changes, the observation stream and the merged badge can never drift apart.
    """
    return item.get("key") or item["text"]


def merge_producer_checklists(producer_lists: list[dict]) -> list[dict]:
    """Merge several producers' checklist copies into one "effective" item list.

    Args:
        producer_lists: One dict per active identified producer, each carrying that
            producer's current checklist copy — {"items": list[dict], "updated_at": str,
            ...}. `updated_at` is the row's ISO-8601 UTC push time; author fields (if
            present) are ignored here. Items are decoded wire dicts.

    Returns:
        A single list of merged items. For each distinct item identity (item_key):
        `done` is the OR across every producer's copy; all other fields (text, due_date,
        group, status) come from the most-recently-updated producer that carries the
        item (last-writer-per-item); items appear in first-seen order.

    Why:
        Both producers track the SAME base checklist, so any one producer's copy can
        only be STALE, never authoritative. OR-ing `done` means a stale "not done" copy
        can never regress a genuinely-done item (that is exactly KI-30's badge flicker),
        while a reopened item self-heals as each producer re-pushes. Metadata is
        last-writer-per-item because a rename/re-dating is a real edit, and the freshest
        push reflects it. The fold walks producers ASCENDING by `updated_at` so the last
        write to touch an item wins its metadata; `sorted` is stable, so equal
        timestamps fall back to input order (producer_checklists_for's name order) —
        fully deterministic.
    """
    # Ascending updated_at: the later a producer pushed, the more its metadata should
    # win. Stable sort → equal timestamps keep the caller's (name-ordered) sequence.
    ordered = sorted(producer_lists, key=lambda p: p["updated_at"])
    # dict preserves first-insertion order; reassigning an existing key does NOT move it,
    # so items keep the order in which they were first seen across the fold.
    merged: dict[str, dict] = {}
    for producer in ordered:
        for item in producer["items"]:
            key = item_key(item)
            prior = merged.get(key)
            # Take the (later) producer's full copy so its metadata wins wholesale...
            new_item = dict(item)
            if prior is not None:
                # ...but `done` is the OR across all copies seen so far, so a stale
                # "not done" can never flip a done item back open.
                new_item["done"] = bool(prior.get("done")) or bool(item.get("done"))
            merged[key] = new_item
    return list(merged.values())


def _effective_producer_key(producer: dict, index: int):
    """Return the grouping key for one producer row (auth revamp, Unit 4b).

    Args:
        producer: A producer checklist row. Carries `effective_producer_id` when it came
            from producer_checklists_for; may carry neither that nor `author_id`.
        index: The row's position, used only as the identity-less fallback.

    Returns:
        The effective producer id, or a per-row unique sentinel when the row carries no
        identity at all.

    Why:
        `effective_producer_id` is what folds an agent into its operator. The fallback is
        the load-bearing part: a caller that supplies NO identity (any caller working from
        bare {items, updated_at} rows) must keep the pre-4b behavior where every row is its
        own producer, so it gets one card each. Defaulting those rows to a SHARED key
        instead would silently collapse unrelated producers into a single card.
    """
    key = producer.get("effective_producer_id")
    if key is None:
        key = producer.get("author_id")
    return key if key is not None else ("__unidentified__", index)


def fold_producer_checklists(producer_lists: list[dict]) -> list[dict]:
    """Group producer rows by effective producer, consolidating each group into one card.

    Args:
        producer_lists: Producer checklist rows from producer_checklists_for, each
            {"author_id", "author_name", "items", "updated_at", "effective_producer_id",
            "effective_producer_name"}.

    Returns:
        One dict per EFFECTIVE producer, in the input's (name-ordered) first-seen order:
        {"author_name": the effective producer's display name, "items": the group's
        consolidated items, "updated_at": the group's latest push time, "author_ids": every
        RAW author_id in the group}. A project with no agents returns one row per input row,
        with `author_ids` a single-element list.

    Why:
        Agents act on their operator's behalf, so the project page shows ONE card per
        person — a human plus their two agents is one contributor, not three. Provenance is
        not lost: the stored rows keep each agent's real author_id, and `author_ids` carries
        them forward so the caller can still resolve per-stream facts (slippage) against the
        raw streams they were derived on.

        Within a group the consolidation is exactly `merge_producer_checklists` — the same
        done-OR, last-writer-per-item rule already used across producers, because the
        problem is identical (several copies of one base checklist, any of which may be
        stale). Crucially the group's rows are passed in WHOLE rather than pre-collapsed, so
        each keeps its own `updated_at` and the last-writer ordering still decides metadata.
        Folding them first would destroy exactly the timestamps that decision depends on.

        Known accepted edge (recorded, not fixed): because `done` is OR-ed, re-opening an
        item needs every stale copy in the group to re-push before the card shows it open.
        That is the same trade the cross-producer merge already makes.
    """
    groups: dict = {}
    for index, producer in enumerate(producer_lists):
        key = _effective_producer_key(producer, index)
        groups.setdefault(key, []).append(producer)
    folded = []
    for group in groups.values():
        # The display name comes from the effective producer (the operator for an agent);
        # a row with no operator falls back to its own denormalized author_name.
        first = group[0]
        name = first.get("effective_producer_name") or first.get("author_name")
        folded.append(
            {
                "author_name": name,
                # A group of ONE has nothing to reconcile, so its items pass through
                # untouched. That is not just an optimization: it keeps this function
                # usable by callers whose rows carry no `updated_at` (which the merge
                # requires but a lone row never needs), so the overwhelmingly common
                # no-agents case behaves exactly as it did before 4b.
                "items": (
                    first["items"] if len(group) == 1 else merge_producer_checklists(group)
                ),
                # The freshest push in the group — this row may itself be merged again by
                # effective_checklist, where updated_at orders the cross-producer fold.
                "updated_at": max(
                    (p["updated_at"] for p in group if p.get("updated_at") is not None),
                    default=None,
                ),
                # Every RAW author id that folded in. The caller unions their per-stream
                # slipping sets; the streams themselves are never merged.
                "author_ids": [
                    p["author_id"] for p in group if p.get("author_id") is not None
                ],
            }
        )
    return folded


def effective_checklist(
    aggregate_items: list[dict] | None, producer_lists: list[dict]
) -> list[dict] | None:
    """Choose the checklist that drives the displayed badge/progress.

    Args:
        aggregate_items: The single-row aggregate checklist (get_checklist's return):
            a list of items, or None when the project has no checklist row at all.
        producer_lists: The active identified producers' checklist copies (already
            revoked-filtered by producer_checklists_for), each {"items", "updated_at"}.

    Returns:
        The merged effective item list when ≥2 producer ROWS exist; otherwise the
        aggregate list UNCHANGED (including None passthrough).

    Why:
        With 0–1 producers there is nothing to merge, so the aggregate (which anonymous
        pushes still update) is returned as-is — every single-producer / anonymous
        deployment stays BYTE-IDENTICAL. Preserving None matters — the project route's
        existence-hiding 404 tests `checklist is None`, so a genuinely checklist-less
        project must stay None, not become [].

        DELIBERATE DEVIATION from the arc's amendment 4 (decided 2026-07-20, with the
        user, on measured evidence): this gate counts RAW ROWS, *not* distinct effective
        producers. Amendment 4 asked for the latter, reasoning that a person plus their
        agent should not "spuriously" trip the merge. But the merge is not about people —
        it reconciles COPIES, and a human and their agent genuinely hold two copies, either
        of which may be stale. Counting effective producers there made a human+agent pair
        fall back to the aggregate, which is last-writer-wins: the other producer's items
        VANISH from the badge and its `done` state flips with push order. That is exactly
        the KI-30 flicker the merge exists to prevent, reintroduced.

        The ≥2 card threshold in the SPA and this gate therefore answer DIFFERENT questions
        — "how many people?" (folded, one card per operator) versus "are there copies to
        reconcile?" (raw rows). They previously coincided; agents are what separate them.
        Folding is unaffected: fold_producer_checklists never consults this gate, so a
        human+agent pair still renders as one card while the badge stays correct.
    """
    if len(producer_lists) >= 2:
        return merge_producer_checklists(producer_lists)
    return aggregate_items

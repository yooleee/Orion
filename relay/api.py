# =============================================================================
# relay/api.py
# -----------------------------------------------------------------------------
# Responsible for: PURE serializers that turn the relay's stored data into the
#                  read-only JSON the dashboard SPA consumes. One function per
#                  screen (me / portfolio / project / report), each taking
#                  already-fetched store rows + the reference date and returning a
#                  JSON-able dict. No I/O, no HTTP, no database.
# Role in project: The "api" layer of the E2 Inc 4 SPA rebuild — the serializer
#                  half of the SPA<->relay seam. server.py fetches (store/derive),
#                  calls these, and _send_json's the result. It MIRRORS render.py's
#                  pattern (pure presentation functions over fetched data), but emits
#                  JSON instead of HTML. The contract these shapes implement lives in
#                  docs/dashboard-api-contract.md.
# Observe, don't originate: every field here is a reframing of data observed from
#                  external sources (git, the tracker doc, sessions) — this layer
#                  authors no domain facts, it only reshapes them for the wire.
# Assumptions: a checklist item is a {"text","done"[, "due_date"][, "key"][, "group"]}
#              dict (the wire shape get_checklist decodes); a report is _row_to_report's
#              shape; `today` is already computed in the relay's display zone.
# =============================================================================

from __future__ import annotations

import re
from datetime import date
from zoneinfo import ZoneInfo

from .derive import (
    DUE_SOON_DAYS,
    OVERDUE,
    bucket_counts,
    classify_item,
    fold_producer_checklists,
    item_key,
    milestones,
    next_open_due,
    slipping_item_keys,
    slipping_item_keys_by_author,
)

# The lifecycle vocabulary (S2.2). Imported rather than re-declared so the wire value and the
# stored value can never drift apart — these are plain string constants, so this module stays
# free of I/O and of any store behaviour.
from .store import LIFECYCLE_ACTIVE, LIFECYCLE_PAST


def _resolve_due_soon_days(value: int | None) -> int:
    """Map a project's stored due-soon horizon (int, or None when unset) to a concrete int.

    Args:
        value: A project's persisted due_soon_days (store.get_due_soon_days), or None when the
            project never set the knob (omit-when-unset).

    Returns:
        The stored horizon, or derive.DUE_SOON_DAYS (7) when it is None.

    Why:
        The pure derive functions take a concrete int horizon, so the None → default mapping
        must happen at the serializer boundary, ONCE per project. Factoring it here (rather than
        inlining the same ternary at every entry point) keeps the "unset means 7 days" rule in
        one place, so the portfolio, project, report, and scheduling surfaces can never disagree
        about what an un-configured project's window is.
    """
    return value if value is not None else DUE_SOON_DAYS

# The display title / portfolio headline is the report body's first line. This extraction
# moved here when render.py retired (E2 Inc 4, KI-23) — the SPA is now the only front-end,
# so api.py owns the rule outright (it formerly lived in render.py and was imported here).
_HEADLINE_MAX_CHARS = 100

# --- Report-title Markdown flattening (KI-42) -------------------------------------------
# The title is the body's first line, and the body is MARKDOWN — so without flattening a
# title like "**Shipped** the parser" or "## Week 3" surfaces its raw `**`/`##` markers on
# the timeline and report page. We strip the notation a plain-text title cannot render,
# mirroring the About collector's CONSERVATIVE rules (src/orion/collectors/about.py): single
# `_`/`*` are LEFT ALONE so `snake_case`, file globs and math survive; only unambiguous
# constructs are touched. Unlike About (which skips whole heading LINES upstream), a title is
# one already-chosen line, so a leading ATX marker is stripped inline here too.
#
# Kept relay-local (a handful of regexes) rather than importing the collector's helper: the
# relay is a self-contained, separately deployed package that imports nothing from the
# producer's src/orion, and a small duplication buys keeping that package boundary clean.
_TITLE_HEADING_RE = re.compile(r"^#{1,6}\s+")  # leading ATX heading "## " -> dropped
_TITLE_INLINE_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")  # ![alt](url) -> dropped
_TITLE_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")  # [text](url) -> text
_TITLE_BOLD_ITALIC_RE = re.compile(r"\*\*\*(.+?)\*\*\*")  # ***text*** -> text
_TITLE_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")  # **text** -> text
_TITLE_INLINE_CODE_RE = re.compile(r"`([^`]+)`")  # `code` -> code


def _flatten_title(line: str) -> str:
    """Strip the inline Markdown notation a plain-text report title cannot render.

    Args:
        line: One already-stripped line — the report body's first non-empty line.

    Returns:
        The same text with a leading heading marker and inline emphasis / link / code /
        image notation removed, and whitespace re-collapsed. Word content is never altered,
        and single `_`/`*` are left intact (so `snake_case` and globs survive). Can return
        "" when the line was pure markup (e.g. a lone image).

    Why:
        The title is observed from a Markdown body and shown as plain text, so leaving `**`
        or `##` in would surface a rendering artifact, not the author's words. Flattening
        keeps the observed wording exact while making it legible in the medium it lands in —
        the same medium-translation the About collector and the Slack mrkdwn path already do.
    """
    line = _TITLE_HEADING_RE.sub("", line)
    line = _TITLE_INLINE_IMAGE_RE.sub("", line)
    line = _TITLE_LINK_RE.sub(r"\1", line)
    line = _TITLE_BOLD_ITALIC_RE.sub(r"\1", line)
    line = _TITLE_BOLD_RE.sub(r"\1", line)
    line = _TITLE_INLINE_CODE_RE.sub(r"\1", line)
    # Dropping an inline image can leave a double space behind; re-collapse runs.
    return " ".join(line.split())


def _headline(body: str, limit: int = _HEADLINE_MAX_CHARS) -> str:
    """Extract a one-line headline from a report body for a portfolio card / title.

    Args:
        body: The report's full body text (may be multi-line, or empty).
        limit: Max characters before truncation. Defaults to _HEADLINE_MAX_CHARS.

    Returns:
        The first non-empty line, stripped and truncated to `limit` characters with a
        trailing "…" when it was longer. Returns "" when the body has no non-empty line,
        so the caller can OMIT the headline rather than render a blank one.

    Why:
        The portfolio home + report timeline show each project's latest update at a glance,
        and the report's own first line is the most honest one-liner available (no invented
        text). Keeping it to one line keeps a card scannable. Truncation is a presentation
        choice, so it lives in this presentation/serializer layer (the store query stays
        content-agnostic). The empty-string fallback means a report with no usable body
        simply drops the headline line — honest over decorative.
    """
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Flatten Markdown BEFORE measuring/truncating, so the length and the "…" cut reflect
        # the visible title (KI-42). A line that was pure markup (e.g. a lone image) flattens
        # to "" — skip it and take the next real line rather than showing a blank title.
        flat = _flatten_title(stripped)
        if not flat:
            continue
        # Add the ellipsis only when we actually cut text (a first line exactly `limit`
        # long is shown whole). One "…" char keeps the rendered length predictable.
        if len(flat) > limit:
            return flat[:limit].rstrip() + "…"
        return flat
    return ""


# An open deadline that is neither overdue nor due-soon: dated, but beyond the at-risk
# horizon. Not a "flagged" state (no glyph/colour in the design vocabulary) — the SPA
# renders it as a neutral relative time — but the relay still owns the classification, so
# it is named here rather than re-derived client-side.
_UPCOMING = "upcoming"


def _item_key(item: dict) -> str:
    """Return a checklist item's stable identity (its `key`, else its text).

    Args:
        item: A checklist wire dict.

    Returns:
        The producer's `key` when present, else the item `text` — the SAME identity
        record_observations/slipping use, so a per-item slipping lookup matches.

    Why:
        Slippage is keyed by item_key (the tracker's bare title survives a status change in
        the text); to mark a checklist item slipping we must resolve the same key the
        observation history stored. Delegates to derive.item_key — the single source of the
        rule, shared with record_observations, the slipping lookup, and the producer-
        checklist merge — so identity can never drift between these paths.
    """
    return item_key(item)


def _deadline_state(
    due_iso: str | None, today: date, due_soon_days: int = DUE_SOON_DAYS
) -> str | None:
    """Classify a deadline date as overdue / due_soon / upcoming, or None when absent.

    Args:
        due_iso: An ISO "YYYY-MM-DD" deadline string, or None.
        today: The reference date (display zone).
        due_soon_days: The project's due-soon horizon (inclusive), forwarded to classify_item
            so "due_soon" vs. "upcoming" splits at the project's window, not always 7 days.

    Returns:
        "overdue" / "due_soon" from the shared classify_item rule, "upcoming" for a dated
        open deadline beyond the due-soon horizon, or None when there is no deadline.

    Why:
        The project header's "NEXT DUE" and a card's deadline chip need a state for ANY
        next deadline, not only the at-risk ones — so this wraps classify_item (an OPEN,
        undone synthetic item) and fills the one gap it leaves: a far-future open deadline,
        which classify_item returns None for, is reported as "upcoming" so the SPA can show
        a neutral relative time. None only when there is genuinely no date.
    """
    if not due_iso:
        return None
    # A synthetic OPEN item so classify_item applies its overdue/due_soon rule to the date.
    state = classify_item({"due_date": due_iso, "done": False}, today, due_soon_days)
    return state if state is not None else _UPCOMING


def _next_due(
    checklist: list | None, today: date, due_soon_days: int = DUE_SOON_DAYS
) -> dict | None:
    """Build the {"due_date","state"} for a checklist's nearest open deadline, or None.

    Args:
        checklist: A project's live checklist items (or None).
        today: The reference date (display zone).
        due_soon_days: The project's due-soon horizon, forwarded to the state classification.

    Returns:
        {"due_date": <iso>, "state": <overdue|due_soon|upcoming>} for the soonest open
        deadline across the checklist, or None when nothing open is dated.

    Why:
        The home row and the project header both show "the next thing due"; this pairs the
        date (next_open_due) with its derived state so the SPA renders the urgency without
        re-deriving. None ⇒ omit the field.
    """
    due = next_open_due(checklist)
    if due is None:
        return None
    return {"due_date": due, "state": _deadline_state(due, today, due_soon_days)}


def _item_state(item: dict, today: date, due_soon_days: int = DUE_SOON_DAYS) -> str:
    """Resolve one checklist item's per-row state (4a vocabulary).

    Args:
        item: A checklist wire dict.
        today: The reference date (display zone).
        due_soon_days: The project's due-soon horizon, forwarded to classify_item so a row's
            "due_soon" state uses the same window as the at-risk count and the deadline chip.

    Returns:
        "done" when finished; else "overdue" / "due_soon" from classify_item; else
        "in_progress" when the producer marked it so (the tracker's structured status,
        E2 Inc 4 closing gap 8); else "not_started" (open, undated, untouched).

    Why:
        The project page's LIVE CHECKLIST and the tracker page colour each row by state.
        Built on classify_item so a row's treatment can never disagree with the at-risk
        count. Deadline urgency (overdue/due_soon) leads the single state because it is the
        more actionable signal; "in_progress" fills the open-and-undated gap that used to
        collapse to "not_started". The raw `status` is ALSO shipped on the row (see
        serialize_project) so the tracker's circular indicator can show the in-progress arc
        independently of this single derived state. Absent status ⇒ the old behaviour.
    """
    if item.get("done"):
        return "done"
    state = classify_item(item, today, due_soon_days)
    if state is not None:
        return state
    if item.get("status") == "in_progress":
        return "in_progress"
    return "not_started"


def _progress(done: int, total: int) -> dict:
    """Build a {"done","total","pct"} progress block (pct None when total is 0).

    Args:
        done: Count of finished items.
        total: Count of all items.

    Returns:
        {"done","total","pct"} where pct = round(done/total*100), or None when total is 0
        (so the SPA hides the bar rather than dividing by zero).

    Why:
        Every card and the project header show the same done/total/percent shape; computing
        it once keeps the percentage rule identical everywhere.
    """
    pct = round(done / total * 100) if total else None
    return {"done": done, "total": total, "pct": pct}


def serialize_me(
    *,
    gated: bool,
    principal: dict | None,
    allowed: set | None,
    display_tz: ZoneInfo,
    showcase_enabled: bool = False,
) -> dict:
    """Serialize the current viewer's identity, scope, and server context (/api/me).

    Args:
        gated: Whether dashboard access is access-gated (server._auth_required).
        principal: The authenticated principal {"user_id","role","name"} or None.
        allowed: The viewer's read scope — None for unrestricted (admin / open relay), else
            the set of granted project names (server._allowed_projects).
        display_tz: The relay's display timezone.
        showcase_enabled: Whether this relay exposes a public, no-login Showcase surface
            (server.showcase_enabled). The SPA shows the "Public showcase" sidebar link
            only when this is true.

    Returns:
        The /api/me shape: gated / authenticated / identity / scope / display_tz /
        showcase_enabled.

    Why:
        The SPA reads this once on boot to pick the full / scoped / empty shell and to know
        whether to redirect to login. We surface display_tz so the SPA formats relative time
        in the SAME zone the relay derives "today" in (one zone, KI-20). scope mirrors
        _allowed_projects' None-vs-set distinction explicitly as unrestricted + a sorted
        list, so the membership question reads the same on both sides of the wire.
    """
    return {
        "gated": gated,
        "authenticated": principal is not None,
        "identity": (
            {"name": principal["name"], "role": principal["role"]}
            if principal is not None
            else None
        ),
        "scope": {
            "unrestricted": allowed is None,
            "projects": None if allowed is None else sorted(allowed),
        },
        "display_tz": display_tz.key,
        "showcase_enabled": showcase_enabled,
    }


def _at_risk_items(
    checklist: list | None, today: date, due_soon_days: int = DUE_SOON_DAYS
) -> list[dict]:
    """List a checklist's at-risk items as forward-signal chips, most urgent first.

    Args:
        checklist: A project's live checklist items (or None).
        today: The reference date (display zone).
        due_soon_days: The project's due-soon horizon, forwarded to classify_item so the chip
            set matches the project's window (and the at-risk count) rather than a fixed 7 days.

    Returns:
        One {"state","label","due_date"} dict per OPEN at-risk item (overdue or due_soon),
        ordered overdue-before-due_soon then by due_date ascending. [] when none.

    Why:
        The tracker card shows a few forward-signal chips ("▲ Hack Your Summer 2d overdue",
        "◷ Claude Corps Fellow in 6d", "+N more"). Shipping every at-risk item (the SPA
        truncates) keeps the "+N more" count honest. Ordering overdue-first then by date
        puts the most pressing chip first, matching the design's read. `label` uses the
        stable key (the tracker's bare title) so a chip reads "Hack Your Summer", not the
        status-suffixed text.
    """
    chips = []
    for item in checklist or []:
        state = classify_item(item, today, due_soon_days)
        if state is None:
            continue
        chips.append(
            {"state": state, "label": _item_key(item), "due_date": item.get("due_date")}
        )
    # overdue (0) before due_soon (1), then by due_date ascending. due_date is present on
    # every at-risk item (classify needs it), so the date key is always a real string.
    chips.sort(key=lambda c: (0 if c["state"] == OVERDUE else 1, c["due_date"]))
    return chips


def _portfolio_entry(row: dict, items: list | None, today: date) -> dict:
    """Serialize one portfolio row (project or tracker) from its store row + checklist.

    Args:
        row: One latest_report_per_project row (carries kind, counts, latest_body, times).
        items: That project's live checklist items (get_checklist), or None.
        today: The reference date (display zone).

    Returns:
        The project-row or tracker-card JSON shape (tracker adds segments + at_risk_items).

    Why:
        Projects and trackers share most fields (name, kind, progress, at-risk, slipping,
        next_due, time); a tracker only ADDS the segmented-bar buckets and the chip list.
        Building the common part once and extending it for trackers keeps the two in sync
        and the projects-vs-todos split a single `kind` branch.

        S2.2: a PAST project carries its record (progress, About, headline, last activity)
        but no forward-looking urgency — at_risk / slipping / next_due are zeroed and a
        tracker's chip list emptied. Suppressing at the SERIALIZER, not in the SPA, is what
        makes the guarantee real: no consumer of this payload can reconstruct an "overdue"
        read of a finished project, because the facts that would say so never leave here.
    """
    done = row["checklist_done"] or 0
    total = row["checklist_total"] or 0
    # E1.2: this project's due-soon horizon (None ⇒ 7-day default). The store already applied
    # it to row["checklist_at_risk"]; we re-apply the SAME value to the item-level derivations
    # below (next_due, segments, chips) so the badge and the detail agree on the window.
    due_soon_days = _resolve_due_soon_days(row.get("due_soon_days"))
    # S2.2: absence reads as active, matching store.get_project_lifecycle — so a row from a
    # pre-column DB (or any caller that did not join the meta table) behaves exactly as before.
    past = row.get("lifecycle") == LIFECYCLE_PAST
    entry = {
        "name": row["project"],
        "kind": row["kind"],
        # S2.2: 'active' | 'past'. The SPA groups on this; it is a declared fact, not derived.
        "lifecycle": row.get("lifecycle") or LIFECYCLE_ACTIVE,
        "progress": _progress(done, total),
        "at_risk": 0 if past else (row["checklist_at_risk"] or 0),
        "slipping": 0 if past else (row["checklist_slipping"] or 0),
        "next_due": None if past else _next_due(items, today, due_soon_days),
        # Last activity: a report's time when there is one, else the checklist's receive
        # clock — the same fallback the old portfolio card used.
        "updated_at": row["latest_generated_at"] or row["checklist_updated_at"],
        # KB surface (Unit 2): the project's About line (None when unset → the SPA renders
        # no sub-line). Carried on BOTH kinds — a tracker may set about_file too — but v1
        # surfaces it on project rows only (the SPA decides presentation).
        "about": row.get("about"),
    }
    if row["kind"] == "tracker":
        # A general checklist: the segmented bar + the chip list, plus an item count.
        entry["item_count"] = total
        segments = bucket_counts(items, today, due_soon_days)
        if past:
            # The segmented bar colours its overdue/due-soon slices red and amber, so leaving
            # them populated would let a finished tracker read as overdue through the bar even
            # with at_risk zeroed. Fold both into `remaining` (open, not urgent): the four
            # buckets still tile the total and `done` is untouched, so the record survives —
            # only the urgency CLAIM is dropped, which is the same edit at_risk gets above.
            segments = {
                "overdue": 0,
                "due_soon": 0,
                "remaining": segments["remaining"] + segments["overdue"] + segments["due_soon"],
                "done": segments["done"],
            }
        entry["segments"] = segments
        entry["at_risk_items"] = [] if past else _at_risk_items(items, today, due_soon_days)
    else:
        # A software project: the one-line headline from the latest report + its id.
        entry["headline"] = _headline(row["latest_body"]) if row["latest_body"] else ""
        entry["report_id"] = row["latest_report_id"]
    return entry


def serialize_portfolio(entries: list[dict], allowed: set | None, today: date) -> dict:
    """Serialize the home dataset, split into projects and trackers (/api/portfolio).

    Args:
        entries: Scope-FILTERED rows, each a latest_report_per_project row plus an "items"
            key holding that project's checklist (get_checklist result, or None). The server
            applies scope before calling, mirroring the old "/" route.
        allowed: The viewer's read scope (None unrestricted, else granted names) — used only
            to report the scope back; filtering already happened.
        today: The reference date (display zone).

    Returns:
        {"scope", "projects", "trackers"} — the home split by kind.

    Why:
        The core IA decision is distinct sections, not one flat list, so the split happens
        HERE (server-side) and the SPA renders two sections without re-deriving kind. scope
        rides along so the SPA can show the viewer's "N projects granted" banner from the
        same response.
    """
    projects = []
    trackers = []
    for row in entries:
        entry = _portfolio_entry(row, row.get("items"), today)
        (trackers if entry["kind"] == "tracker" else projects).append(entry)
    return {
        "scope": {
            "unrestricted": allowed is None,
            "projects": None if allowed is None else sorted(allowed),
        },
        "projects": projects,
        "trackers": trackers,
    }


def _discipline_card(card: dict) -> dict:
    """Reduce a stored discipline to the wire card the SPA renders.

    Args:
        card: One stored discipline ({title, why, scope, source}); `scope` was used to
            bucket it (Global vs per-project) and is dropped from the card itself.

    Returns:
        {"title", "why", "source"} — the bold title, the "why" paragraph, and the
        repo-relative doc the "observed · <source>" footer shows.

    Why:
        The grouping (Global section vs a project's section) already encodes the scope,
        so the card itself need not carry it — one minimal shape for both groups keeps
        the SPA's Discipline type single and the wire honest.
    """
    return {
        "title": card.get("title", ""),
        "why": card.get("why", ""),
        "source": card.get("source", ""),
    }


def _showcase_card(row: dict) -> dict:
    """Serialize one curated project into a public Showcase card (summary facts only).

    Args:
        row: A latest_report_per_project row for an allowlisted project, with an extra
            "blurb" key holding the curated public description ("" when none was set).

    Returns:
        A {"name", "description", "status", "progress", "report_count"} card — and
        DELIBERATELY nothing else. No checklist, reports, comments, or deadlines.

    Why:
        The Showcase is a public, no-login surface, so the privacy boundary is enforced by
        the SHAPE here: this dict is the entire wire contract, so there is no path by which
        an item label, a comment, or a deadline can leak to an anonymous viewer (a guard
        pinned by test). `description` prefers the curated blurb and falls back to the same
        observed headline the portfolio uses, so an allowlisted project always reads
        sensibly even before a blurb is written. `status` is derived from completion (a
        fully-done project reads "shipped", otherwise "active") rather than authored, in
        keeping with observe-and-reframe.
    """
    progress = _progress(row["checklist_done"] or 0, row["checklist_total"] or 0)
    # A curated blurb wins; else the latest report's headline; else empty. _headline() needs
    # a non-empty body, so guard the checklist-only case (no report → latest_body is None).
    description = row.get("blurb") or (
        _headline(row["latest_body"]) if row.get("latest_body") else ""
    )
    return {
        "name": row["project"],
        "description": description,
        "status": "shipped" if progress["pct"] == 100 else "active",
        "progress": progress,
        "report_count": row["report_count"],
    }


def serialize_showcase(entries: list[dict]) -> dict:
    """Serialize the curated public Showcase dataset (/api/showcase).

    Args:
        entries: The allowlisted projects, in allowlist (display) order. Each is a
            latest_report_per_project row plus a "blurb" key (the curated description, or
            "" to fall back to the headline). The server applies the allowlist before
            calling — this serializer trusts the caller's curation, exactly as
            serialize_portfolio trusts the caller's scope filter.

    Returns:
        {"projects": [...]} — one summary card per curated project, allowlist-ordered.

    Why:
        Pulled out as its own serializer (not folded into the portfolio) because the public
        surface is a STRICT SUBSET of the fields a logged-in viewer sees — a separate
        function makes that narrowing explicit and testable rather than relying on a caller
        to strip fields. No `scope` block: the guest has no session, and the allowlist is
        the only access control here.
    """
    return {"projects": [_showcase_card(row) for row in entries]}


def serialize_scheduling(projects: list[dict], today: date) -> dict:
    """Aggregate every scoped project's open, dated deadlines into time buckets.

    Args:
        projects: Scope-FILTERED per-project data, each a dict with "name", "kind",
            "lifecycle" ("active" | "past"; absent reads as active), "items" (that project's
            checklist, or None), and "observations" (its observed_history, for slippage).
            The server fetches + filters before calling, mirroring serialize_portfolio.
        today: The reference date (display zone).

    Returns:
        {"summary": {"overdue","due_this_week","slipping"},
         "buckets": {"overdue":[...], "this_week":[...], "later":[...]}} where each bucket
        is a list of {state, label, due_date, slipping, source:{name,kind}} sorted by
        due_date ascending (soonest / most-overdue first).

    Why:
        The Scheduling view is the cross-project "by when" lens: the SAME deadlines the
        portfolio/project pages show, re-grouped by time. Only OPEN, DATED items appear —
        a timeline has no place for undated or finished items. Bucketing reuses the relay's
        existing per-deadline classifier (_deadline_state → overdue/due_soon/upcoming) so a
        row's bucket can never disagree with the urgency the rest of the dashboard shows;
        slippage reuses slipping_item_keys so the count matches the project page. The label
        is `key ?? text` (the clean title with any embedded status stripped), the same rule
        the tracker page uses. No new derivation — this is pure re-aggregation.

        S2.2: PAST projects are skipped entirely. A finished project's leftover deadlines are
        history, and a timeline is the one surface where they would be loudest ("14 overdue"
        in the summary, months of red rows). The skip lives HERE rather than in the route so
        the rule is unit-testable against fixed inputs, and so every future caller of this
        serializer inherits it instead of having to remember to filter first.
    """
    buckets: dict[str, list] = {"overdue": [], "this_week": [], "later": []}
    summary = {"overdue": 0, "due_this_week": 0, "slipping": 0}
    # _deadline_state's three open-deadline states → the design's three time buckets.
    bucket_of = {OVERDUE: "overdue", "due_soon": "this_week", _UPCOMING: "later"}
    for proj in projects:
        # Absence reads as active (store.get_project_lifecycle's rule), so a caller that does
        # not supply the key gets today's behaviour unchanged.
        if proj.get("lifecycle") == LIFECYCLE_PAST:
            continue
        items = proj.get("items") or []
        slipping = slipping_item_keys(proj.get("observations") or [], today)
        # E1.2: the Scheduling timeline uses the FIXED default week (_deadline_state's default
        # DUE_SOON_DAYS), NOT a project's custom due_soon_days. "this_week" is a calendar
        # concept — a shared cross-project timeline — so a project raising its at-risk horizon
        # to 30 days must not drag month-out items into a bucket labelled "this week". The
        # per-project horizon still drives the project/portfolio at-risk classification; it
        # just does not redefine this timeline's buckets.
        for item in items:
            if item.get("done"):
                continue  # finished — off the timeline
            state = _deadline_state(item.get("due_date"), today)  # fixed default week
            if state is None:
                continue  # open but undated — no place on a timeline
            is_slipping = _item_key(item) in slipping
            buckets[bucket_of[state]].append(
                {
                    "state": state,
                    "label": item.get("key") or item["text"],
                    "due_date": item.get("due_date"),
                    "slipping": is_slipping,
                    "source": {"name": proj["name"], "kind": proj["kind"]},
                }
            )
            if is_slipping:
                summary["slipping"] += 1
    for rows in buckets.values():
        rows.sort(key=lambda r: r["due_date"])
    summary["overdue"] = len(buckets["overdue"])
    summary["due_this_week"] = len(buckets["this_week"])
    return {"summary": summary, "buckets": buckets}


def _checklist_rows(
    items: list, today: date, slipping: set, due_soon_days: int = DUE_SOON_DAYS
) -> list[dict]:
    """Serialize checklist items into the dashboard's per-item row shape.

    Args:
        items: The checklist items (validated {"text", "done"[, ...]} dicts), in file order.
        today: The reference date (display zone), for the per-item state derivation.
        slipping: The set of slipping item keys the CALLER supplies — the project-wide union
            for the aggregate rows, or one producer's own stream for a producer card.
        due_soon_days: The project's due-soon horizon, forwarded to each row's state derivation
            so a row's "due_soon" matches the project window (aggregate and per-producer cards
            share the project's single horizon).

    Returns:
        A list of row dicts {text, done, due_date, key, group, state, status, slipping}.

    Why:
        The aggregate checklist AND each per-producer checklist (C3 Inc 2) render the SAME row
        shape, so building it lives in one place (DRY). `state` is self-contained per item;
        `slipping` is passed in, so this builder is agnostic to WHOSE slippage it marks. As of
        C3 Inc 2.5 the caller hands the aggregate the project-wide union and each producer card
        its own partitioned stream (slipping_item_keys_by_author), so a card marks only the
        slippage its own machine's pushes recorded.
    """
    return [
        {
            "text": item["text"],
            "done": bool(item.get("done")),
            "due_date": item.get("due_date"),
            "key": item.get("key"),
            "group": item.get("group"),
            "state": _item_state(item, today, due_soon_days),
            # The raw observed status (E2 Inc 4, gap 8): None for items without one. Shipped
            # alongside the derived `state` so the tracker's circular indicator renders the
            # in_progress/submitted treatment directly, and so future consumers get status as
            # a first-class fact rather than re-deriving it from `state`.
            "status": item.get("status"),
            "slipping": _item_key(item) in slipping,
        }
        for item in items
    ]


def _attribution_fields(report: dict, attributions: dict | None) -> dict:
    """Build the {author_kind, operated_by_name} pair for one report row (Unit 4a).

    Args:
        report: A report row carrying `author_id` (None on a legacy anonymous push).
        attributions: {author_id: (account_kind, operated_by_name)} from
            store.author_attributions, or None when the caller resolved none.

    Returns:
        {"author_kind": ..., "operated_by_name": ...} — both null when the author cannot
        be resolved (legacy push, deleted account, or no map supplied).

    Why:
        The report timeline entry and the report detail page must badge identically, and
        they are built in two different functions. One helper means the "what does an
        unresolvable author look like on the wire" answer is written once.

        Both fields stay null rather than defaulting to "human": a legacy push genuinely
        carried no identity, and emitting "human" would assert an attribution the relay
        never made. Null also keeps every pre-4a report byte-identical on the wire.
    """
    kind, operated_by_name = (attributions or {}).get(report.get("author_id"), (None, None))
    return {"author_kind": kind, "operated_by_name": operated_by_name}


def serialize_project(
    *,
    name: str,
    kind: str,
    reports: list[dict],
    checklist: list | None,
    observations: list[dict],
    producer_checklists: list[dict],
    discussions: list[dict],
    disciplines: dict | None,
    today: date,
    due_soon_days: int | None = None,
    attributions: dict | None = None,
    about: str | None = None,
    lifecycle: str = LIFECYCLE_ACTIVE,
) -> dict:
    """Serialize one project's full detail (/api/projects/:name).

    Args:
        name: The project name.
        kind: The project's kind ("project" | "tracker").
        reports: The project's reports newest-first (store.history).
        checklist: The live checklist items (store.get_checklist), or None.
        observations: The project's observed history (store.observed_history), each row carrying
            its recording producer's author_id, for per-producer slippage (C3 Inc 2.5).
        producer_checklists: Each identified producer's own live checklist
            (store.producer_checklists_for) — {"author_id", "author_name", "items",
            "effective_producer_id", "effective_producer_name", ...} per producer, for the
            per-producer cards (C3 Inc 2). author_id keys each card's own slippage stream but
            is not emitted; the effective-producer fields drive Unit 4b's fold, so an agent's
            row groups under the human it acts for. Empty for a legacy-only / single-writer
            project.
        discussions: The project's discussion thread oldest-first
            (store.discussion_items_for_project) — the supervisor-interaction loop (E2 Inc 5).
            The single conversation surface since KI-28 Stage 2 retired per-report comments.
        disciplines: The project's observed disciplines with their freshness stamp
            (store.project_disciplines) — {"cards", "updated_at"} — or None when the project
            has never pushed disciplines. Backs the "Working agreements" section (Unit 5).
        today: The reference date (display zone).
        due_soon_days: The project's due-soon horizon (store.get_due_soon_days), or None when
            unset ⇒ the 7-day default. Applied to the milestones, the checklist rows (aggregate
            AND per-producer), and the next-due state, so the whole page uses one window.
        attributions: {author_id: (account_kind, operated_by_name)} from
            store.author_attributions, for the report timeline's agent badge (Unit 4a). None
            or a missing id both mean "nothing to badge", so a caller that does not pass it
            gets today's output unchanged.
        about: The project's About line (store.get_about), or None ⇒ no About band. Rendered
            under the title on the project page (KB surface Unit 2).
        lifecycle: "active" | "past" (store.get_project_lifecycle). Defaults to "active" so a
            caller that does not pass it gets today's output unchanged. When "past", the
            page's forward-looking NEXT DUE is suppressed — see below.

    Returns:
        The project-detail shape: stats, milestones, checklist, producer_checklists, reports,
        discussions, disciplines.

    Why:
        One project page draws from four stores (reports, live checklist, observation
        history, discussion thread) plus three derivations (milestones, slippage, per-item
        state).
        Assembling them here keeps server.py a thin fetch-and-emit and makes the whole shape
        unit-testable with fixed inputs and a fixed `today`. slipping is resolved once (a set
        of keys) and applied to both the per-item rows and the milestone roll-ups so they
        always agree.
    """
    # Partition slippage by producer ONCE (C3 Inc 2.5): each producer card marks only its own
    # stream's slippage, while the aggregate rows + milestone roll-ups use the project-wide
    # union. Union it locally rather than calling slipping_item_keys again (same result, one
    # partition instead of two).
    slipping_by_author = slipping_item_keys_by_author(observations, today)
    slipping = {key for keys in slipping_by_author.values() for key in keys}
    items = checklist or []
    done = sum(1 for item in items if item.get("done"))
    # E1.2: resolve the project's due-soon window ONCE (None ⇒ default) and thread it into
    # every deadline derivation below so the milestones, per-item rows, and next-due state all
    # classify against the same horizon the portfolio at-risk badge used.
    resolved_due_soon_days = _resolve_due_soon_days(due_soon_days)

    # Per-milestone slipping: a group slips when any of its OPEN items is in the slipping
    # set. milestones() returns group summaries without item keys, so we resolve membership
    # here from the same checklist + slipping set.
    milestone_rows = []
    for m in milestones(checklist, today, resolved_due_soon_days):
        # This group's OPEN items that are slipping (a deadline that moved later, or one
        # lingering open past due). Counted once: the boolean `slipping` is just "any of
        # these", and Unit 5 surfaces the exact count beside it. Deriving both from one
        # comprehension keeps them from ever disagreeing (DRY).
        group_slipping_count = sum(
            1
            for item in items
            if item.get("group") == m["group"]
            and not item.get("done")
            and _item_key(item) in slipping
        )
        milestone_rows.append(
            {**m, "slipping": group_slipping_count > 0, "slipping_count": group_slipping_count}
        )

    checklist_rows = _checklist_rows(items, today, slipping, resolved_due_soon_days)

    # C3 Inc 2: one card per identified producer, each the same row shape as the aggregate.
    # C3 Inc 2.5: each card marks slipping from its OWN producer's stream, not the
    # project-wide union — so a deadline that only one machine let slip shows on that
    # producer's card alone. Internal ids key the lookups but are never emitted (the wire
    # hides them). Empty list ⇒ the SPA shows no per-producer section (single-writer /
    # legacy projects render unchanged).
    #
    # Unit 4b: cards are keyed by EFFECTIVE producer, so an agent's card folds into its
    # operator's — a person plus their agents is one contributor. The slipping lookup stays
    # keyed on the RAW author ids and is UNIONED across the folded group. That asymmetry is
    # the whole point: slippage is derived per raw stream (a streak needs ≥2 observations in
    # the SAME stream), so folding before deriving would interleave a human's and an agent's
    # histories and manufacture the false postponement C3 Inc 2.5 fixed. Fold for DISPLAY,
    # never for derivation.
    folded_producers = fold_producer_checklists(producer_checklists)
    producer_checklist_rows = [
        {
            "author_name": pc["author_name"],
            "progress": _progress(
                sum(1 for item in pc["items"] if item.get("done")), len(pc["items"])
            ),
            "items": _checklist_rows(
                pc["items"],
                today,
                # The union of every raw stream that folded into this card.
                set().union(
                    *(
                        slipping_by_author.get(author_id, set())
                        for author_id in pc["author_ids"]
                    )
                )
                if pc["author_ids"]
                else set(),
                resolved_due_soon_days,
            ),
        }
        for pc in folded_producers
    ]

    # Per-project ordinal for each report (the timeline shows #N, not the gappy global id).
    report_numbers = _report_numbers(reports)

    return {
        "name": name,
        "kind": kind,
        # KB surface (Unit 2): the project's observed About line (its doc's first paragraph),
        # or None ⇒ no band. Mechanically observed from the project's doc, NOT an authored
        # project blurb. (The old always-null `description` gap-5 field retired in DR1-R U3;
        # an authored blurb, if ever wanted, is an additive re-add.)
        "about": about,
        # S2.2: 'active' | 'past' — a declared fact (an admin marked it), never derived from
        # staleness. The SPA badges the header on 'past'.
        "lifecycle": lifecycle,
        "stats": {
            "progress": _progress(done, len(items)),
            # S2.2: a past project has no NEXT DUE — its deadlines are history, not a
            # forward look. Suppressed at the PAGE level only: the milestone roll-ups and the
            # per-item rows below keep their real dates and states, because that is the record
            # of what happened, and it sits behind a collapsed group rather than in a headline.
            "next_due": (
                None
                if lifecycle == LIFECYCLE_PAST
                else _next_due(checklist, today, resolved_due_soon_days)
            ),
            "reports_count": len(reports),
        },
        "milestones": milestone_rows,
        "checklist": checklist_rows,
        "producer_checklists": producer_checklist_rows,
        "reports": [
            {
                "id": r["id"],
                "number": report_numbers[r["id"]],  # per-project ordinal (id stays identity)
                # The report's display title for the timeline — the body headline (its first
                # line), the same rule serialize_report uses, so the timeline entry and the
                # report page agree on the title.
                "title": _headline(r["body"]) if r["body"] else "",
                "generated_at": r["generated_at"],
                "lane": r["lane"],
                "share_level": r["share_level"],
                "section_count": len(r["sections"]),
                # C3 Inc 2: producer who pushed it, or null for a legacy/old report (the
                # timeline shows a "pushed by" only when present). author_id stays off the wire.
                "author_name": r.get("author_name"),
                # Unit 4a: what KIND of producer, and (for an agent) whose behalf it acted
                # on — resolved from the live account, so a rename or reassignment is
                # correct on every past report. Both null ⇒ nothing to badge.
                **_attribution_fields(r, attributions),
                "source_tags": [],  # contract gap 4: collector set not stored
            }
            for r in reports
        ],
        "discussions": [_discussion_item(d) for d in discussions],
        # Unit 5: the project's "Working agreements" — all of its discipline cards regardless
        # of LLM-judged scope (the global/project split no longer gates placement), plus the
        # freshness stamp the section's "updated <date>" line renders. None (no row) or an
        # empty card set both serialize to null so the SPA simply omits the section.
        "disciplines": (
            {
                "cards": [_discipline_card(c) for c in disciplines["cards"]],
                "updated_at": disciplines["updated_at"],
            }
            if disciplines and disciplines["cards"]
            else None
        ),
    }


def _discussion_item(d: dict) -> dict:
    """Serialize one discussion-thread item to the wire shape (E2 Inc 5, Unit 2).

    Args:
        d: A store discussion dict ({"id","author_name","role","body","created_at", ...}).

    Returns:
        {"id","author_name","role","body","created_at"} — like a comment, but role is a
        REAL value ("supervisor" | "developer"), not null: discussion items carry first-class
        identity, which is what closes contract gap 7 (the role badge) for this surface.

    Why:
        The thread renders on the project page (and, later, a cross-project inbox), so the
        per-item shape is defined once here. author_id is intentionally dropped from the
        wire: the badge needs author_name + role, and the internal relay_users id is not the
        SPA's business. project is dropped too (the panel already knows its project), exactly
        as _comment drops report_id.
    """
    return {
        "id": d["id"],
        "author_name": d["author_name"],
        "role": d["role"],
        "body": d["body"],
        "created_at": d["created_at"],
    }


def _report_numbers(history: list[dict]) -> dict:
    """Map each report id to its per-PROJECT ordinal (#1 = oldest, this project only).

    Args:
        history: The project's reports newest-first (store.history).

    Returns:
        {report_id: ordinal}, where the OLDEST report in THIS project is 1 and the newest is
        len(history).

    Why:
        The relay's report `id` is a single GLOBAL autoincrement across all projects, so per
        project it reads gappy (e.g. orion #1-5 then #18-29 because another project's reports
        took the ids in between). The dashboard shows a per-project ordinal for legibility
        while keeping `id` as the stable identity for URLs / fetching / comment attachment.
        history is newest-first, so the oldest (last) report gets 1. The ordinal is
        position-derived, so it is stable only because reports are append-only and
        time-ordered; `id` remains the permanent identity.
    """
    n = len(history)
    return {r["id"]: n - i for i, r in enumerate(history)}


def _report_nav(report_id: int, history: list[dict], numbers: dict) -> dict:
    """Find the older/newer reports around `report_id`, with their ids AND per-project numbers.

    Args:
        report_id: The current report's id.
        history: The project's reports newest-first (store.history).
        numbers: The id->ordinal map from _report_numbers (for the neighbours' display labels).

    Returns:
        {"prev_id","prev_number","next_id","next_number"}: prev_* the OLDER neighbour, next_*
        the NEWER one, each None at the ends. The ids drive routing; the numbers drive the
        "Report #N" labels. None throughout when the id is not in the history (defensive).

    Why:
        The report header offers "Report #N →" (older) and a back-link. history is
        newest-first, so the newer neighbour sits at index-1 and the older at index+1. The
        label shows the per-project ordinal, not the global id (see _report_numbers).
    """
    ids = [r["id"] for r in history]
    if report_id not in ids:
        return {"prev_id": None, "prev_number": None, "next_id": None, "next_number": None}
    i = ids.index(report_id)
    newer = ids[i - 1] if i > 0 else None
    older = ids[i + 1] if i + 1 < len(ids) else None
    return {
        "prev_id": older,
        "prev_number": numbers.get(older),
        "next_id": newer,
        "next_number": numbers.get(newer),
    }


def serialize_report(
    *,
    report: dict,
    checklist: list | None,
    history: list[dict],
    today: date,
    due_soon_days: int | None = None,
    attributions: dict | None = None,
) -> dict:
    """Serialize one report in full (/api/reports/:id).

    Args:
        report: The report (store.get / _row_to_report shape).
        checklist: The report's project's live checklist (store.get_checklist), for the rail
            snapshot, or None.
        history: The project's reports newest-first (store.history), for prev/next nav.
        today: The reference date (display zone), for the snapshot rows' states.
        due_soon_days: The report's project horizon (store.get_due_soon_days), or None ⇒ the
            7-day default. Applied to the checklist snapshot rows so their due-ness matches the
            project page's window.
        attributions: {author_id: (account_kind, operated_by_name)} from
            store.author_attributions, for this report's agent badge (Unit 4a). None or a
            missing id both mean "nothing to badge".

    Returns:
        The report-detail shape: body + sections, metadata, participants, checklist
        snapshot, and prev/next nav. The one conversation surface is the project-level
        Discussion thread (KI-28 Stage 2 retired per-report comments).

    Why:
        The report reader's body+rail layout pulls the report, its project's live checklist
        (the snapshot), and the neighbouring report ids together. Assembling it here keeps
        the shape testable and the title rule (the body headline, distinct from the section
        labels) in one place.
    """
    snapshot_items = checklist or []
    snapshot_done = sum(1 for item in snapshot_items if item.get("done"))
    # E1.2: the project's due-soon window (None ⇒ default) for the snapshot rows' states.
    resolved_due_soon_days = _resolve_due_soon_days(due_soon_days)
    numbers = _report_numbers(history)
    return {
        "id": report["id"],
        "number": numbers.get(report["id"]),  # per-project ordinal (id stays the identity)
        "project": report["project"],
        "title": _headline(report["body"]) if report["body"] else "",
        "sections": report["sections"],
        "body": report["body"],
        "lane": report["lane"],
        "share_level": report["share_level"],
        "generated_at": report["generated_at"],
        "ingested_at": report["ingested_at"],
        "orion_version": report["orion_version"],
        # C3 Inc 2: who pushed this report — the producer's server-derived display name, or
        # null for a legacy/old report with no identity. author_id stays off the wire (mirrors
        # the discussion convention); .get keeps pre-attribution report dicts safe.
        "author_name": report.get("author_name"),
        # Unit 4a: the producer's KIND, and (for an agent) the human it acted on behalf of.
        # Resolved from the live account at read time, so both go null once that account is
        # deleted even though the denormalized author_name above survives.
        **_attribution_fields(report, attributions),
        # participants are stored as plain name strings; role is null until they carry an
        # identity (contract gap 3).
        "participants": [{"name": p, "role": None} for p in report["participants"]],
        "source_tags": [],  # contract gap 4
        "checklist_snapshot": {
            "done": snapshot_done,
            "total": len(snapshot_items),
            "rows": [
                {
                    "text": item["text"],
                    "done": bool(item.get("done")),
                    "state": _item_state(item, today, resolved_due_soon_days),
                    "due_date": item.get("due_date"),
                }
                for item in snapshot_items
            ],
        },
        "nav": _report_nav(report["id"], history, numbers),
    }

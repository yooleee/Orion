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

from datetime import date
from zoneinfo import ZoneInfo

from .derive import (
    OVERDUE,
    bucket_counts,
    classify_item,
    milestones,
    next_open_due,
    slipping_item_keys,
)

# The display title / portfolio headline is the report body's first line. This extraction
# moved here when render.py retired (E2 Inc 4, KI-23) — the SPA is now the only front-end,
# so api.py owns the rule outright (it formerly lived in render.py and was imported here).
_HEADLINE_MAX_CHARS = 100


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
        if stripped:
            # Add the ellipsis only when we actually cut text (a first line exactly `limit`
            # long is shown whole). One "…" char keeps the rendered length predictable.
            if len(stripped) > limit:
                return stripped[:limit].rstrip() + "…"
            return stripped
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
        observation history stored. One helper keeps that rule in lockstep with store.py.
    """
    return item.get("key") or item["text"]


def _deadline_state(due_iso: str | None, today: date) -> str | None:
    """Classify a deadline date as overdue / due_soon / upcoming, or None when absent.

    Args:
        due_iso: An ISO "YYYY-MM-DD" deadline string, or None.
        today: The reference date (display zone).

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
    state = classify_item({"due_date": due_iso, "done": False}, today)
    return state if state is not None else _UPCOMING


def _next_due(checklist: list | None, today: date) -> dict | None:
    """Build the {"due_date","state"} for a checklist's nearest open deadline, or None.

    Args:
        checklist: A project's live checklist items (or None).
        today: The reference date (display zone).

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
    return {"due_date": due, "state": _deadline_state(due, today)}


def _item_state(item: dict, today: date) -> str:
    """Resolve one checklist item's per-row state (4a vocabulary).

    Args:
        item: A checklist wire dict.
        today: The reference date (display zone).

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
    state = classify_item(item, today)
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


def _at_risk_items(checklist: list | None, today: date) -> list[dict]:
    """List a checklist's at-risk items as forward-signal chips, most urgent first.

    Args:
        checklist: A project's live checklist items (or None).
        today: The reference date (display zone).

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
        state = classify_item(item, today)
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
    """
    done = row["checklist_done"] or 0
    total = row["checklist_total"] or 0
    entry = {
        "name": row["project"],
        "kind": row["kind"],
        "progress": _progress(done, total),
        "at_risk": row["checklist_at_risk"] or 0,
        "slipping": row["checklist_slipping"] or 0,
        "next_due": _next_due(items, today),
        # Last activity: a report's time when there is one, else the checklist's receive
        # clock — the same fallback the old portfolio card used.
        "updated_at": row["latest_generated_at"] or row["checklist_updated_at"],
    }
    if row["kind"] == "tracker":
        # A general checklist: the segmented bar + the chip list, plus an item count.
        entry["item_count"] = total
        entry["segments"] = bucket_counts(items, today)
        entry["at_risk_items"] = _at_risk_items(items, today)
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


def serialize_disciplines(projects: list[dict], allowed: set | None) -> dict:
    """Serialize observed disciplines, split into Global and per-project (/api/disciplines).

    Args:
        projects: Scope-FILTERED entries, each {"name": str, "disciplines": list | None}
            where the list is get_disciplines' result (stored {title, why, scope, source}
            dicts) or None. The server applies scope BEFORE calling, so a global principle
            declared only in an out-of-scope project never reaches a scoped viewer
            (existence-hiding, consistent with the rest of the relay).
        allowed: The viewer's read scope (None unrestricted, else granted names) — reported
            back only; filtering already happened.

    Returns:
        {"scope", "global", "projects"}:
          - global: deduped global-scope cards (across all in-scope projects), sorted by title.
          - projects: [{name, principles}] for each project with project-scope cards, sorted
            by name, each principles list sorted by title.

    Why:
        Mirrors serialize_portfolio's server-side split: the Global-vs-project grouping is
        derived HERE, not in the collector (which only sees one project) nor the store (which
        keeps a flat per-project list). A global principle may be stated in several projects'
        docs, so we dedupe by normalized title and pick the source deterministically (the
        lexicographically first (project, source)) — otherwise the footer would flicker with
        ingest order. No `today`: disciplines are dateless.
    """
    # Collect + dedupe global cards by normalized title. For each title we keep the
    # candidate with the smallest (project, source) so the chosen source is stable.
    globals_by_title: dict[str, tuple[str, str, dict]] = {}
    for entry in projects:
        name = entry["name"]
        for card in entry.get("disciplines") or []:
            if card.get("scope") != "global":
                continue
            norm = card.get("title", "").strip().casefold()
            if not norm:
                continue
            source = card.get("source", "")
            chosen = globals_by_title.get(norm)
            if chosen is None or (name, source) < (chosen[0], chosen[1]):
                globals_by_title[norm] = (name, source, card)
    global_cards = [
        _discipline_card(c[2])
        for c in sorted(globals_by_title.values(), key=lambda c: c[2].get("title", ""))
    ]

    # Per-project groups: each project's project-scope cards, projects sorted by name and
    # cards sorted by title. Projects with no project-scope cards are omitted (no empty group).
    project_groups = []
    for entry in sorted(projects, key=lambda e: e["name"]):
        cards = [
            _discipline_card(card)
            for card in (entry.get("disciplines") or [])
            if card.get("scope") == "project"
        ]
        if cards:
            cards.sort(key=lambda c: c["title"])
            project_groups.append({"name": entry["name"], "principles": cards})

    return {
        "scope": {
            "unrestricted": allowed is None,
            "projects": None if allowed is None else sorted(allowed),
        },
        "global": global_cards,
        "projects": project_groups,
    }


# The canonical order of a skill's evidence signals on the wire. Defined HERE (not
# imported from the producer's extract.SKILL_SIGNALS) so the relay stays independent of
# producer code — the relay validates against this same set in the push handler. Any
# unknown kind a card carries is simply dropped from the merged output.
_SKILL_SIGNAL_ORDER = ("git", "tasks", "docs")

# Depth-bucket boundaries for the comb's tooth height (E2 Inc 4 slice 4c; RE-TUNED for
# the global two-pass rework). A merged skill's score is `summed per-project weight +
# (breadth - 1)`, so BREADTH (how many projects surface the skill) and per-project
# CENTRALITY both raise the tooth. Boundaries are named constants (not magic numbers) so
# they are tunable in one place and pinned by test.
#
# Why re-tuned: the OLD boundaries (1/2/3) assumed "most skills are single-project,"
# because per-project independent extraction failed to merge cross-project duplicates and
# under-counted breadth. The global rework makes breadth ACCURATE — the same competency
# across N projects now merges into one skill with breadth N — so scores shift UP and the
# old scale pinned most teeth at depth 4 (a flat comb). The wider boundaries below spread
# the post-dedup distribution back across 1..4: a single-project incidental skill (weight 1)
# stays depth 1, a single-project central one (weight 3) lands depth 2, a skill genuinely
# shared across a couple of projects reaches depth 3, and a broadly cross-cutting one
# (high summed weight AND breadth) tops out at depth 4.
#
# PROVISIONAL: these are calibrated against the EXPECTED post-dedup shape, not yet against
# a real `skills-sync` run. The CP1 eyes-on calibration (run sync on the real portfolio,
# inspect the depth spread) is what finalizes them; adjust here if the real comb still
# clusters. CP2's visual reads whatever scale this lands on.
_DEPTH_T1 = 2  # score <= 2  -> depth 1
_DEPTH_T2 = 4  # score <= 4  -> depth 2
_DEPTH_T3 = 6  # score <= 6  -> depth 3  (else depth 4)


def _skill_depth(total_weight: int, breadth: int) -> int:
    """Derive a merged skill's comb depth (1-4) from its evidence weight and breadth.

    Args:
        total_weight: The sum of the skill's per-project weights (each 1-3).
        breadth: How many in-scope projects independently surface the skill.

    Returns:
        An ordinal depth 1-4 driving the tooth height.

    Why:
        This is the ONE place that sees the whole portfolio, so depth is derived here,
        not in the producer (which sees one project). Adding a breadth bonus to the
        summed weight means a skill demonstrated across several projects out-ranks an
        equally-weighted one confined to a single project — encoding "central across the
        work," which is what the comb is meant to show. Monotonic and bounded, so a
        thin portfolio yields short teeth rather than a misleadingly tall one.
    """
    score = total_weight + (breadth - 1)
    if score <= _DEPTH_T1:
        return 1
    if score <= _DEPTH_T2:
        return 2
    if score <= _DEPTH_T3:
        return 3
    return 4


def serialize_skills(projects: list[dict], allowed: set | None) -> dict:
    """Serialize observed skills, merged across projects into the comb (/api/skills).

    Args:
        projects: Scope-FILTERED entries, each {"name": str, "skills": list | None} where
            the list is get_skills' result (stored {name, category, evidence, weight,
            signals} dicts) or None. The server applies scope BEFORE
            calling, so a skill evidenced only in an out-of-scope project never reaches a
            scoped viewer, and the `projects` anchor on each merged skill can only name
            in-scope projects (existence-hiding, consistent with serialize_disciplines).
        allowed: The viewer's read scope (None unrestricted, else granted names) —
            reported back only; filtering already happened.

    Returns:
        {"scope", "categories", "skills"}:
          - categories: the distinct skill categories, ordered by total depth (the comb's
            strongest groupings first), tie-broken by name — the comb's section order.
          - skills: the MERGED skills, each {name, category, depth, projects, evidence,
            signals}. Sorted by (category order, then descending depth, then name) so the
            tallest teeth lead each group.

    Why:
        A skill is a CROSS-PROJECT entity (the same competency may be demonstrated by
        several projects), unlike a discipline (per-project). So the merge — not just a
        grouping — lives here: we union by normalized name, sum weights and collect the
        evidencing projects to derive depth (breadth + centrality), and pick the
        canonical name/category/evidence deterministically (the lexicographically-first
        (project) contributor) so the card does not flicker with push order. This mirrors
        serialize_disciplines' stability rule while doing the heavier cross-project fold
        the comb needs.
    """
    # Merge by normalized (casefolded) name. For each merged skill we accumulate the
    # contributing projects, the summed weight, the unioned signals, and — keyed on the
    # lexicographically-first (project, name) — the canonical display name/category/why.
    merged: dict[str, dict] = {}
    for entry in sorted(projects, key=lambda e: e["name"]):
        project_name = entry["name"]
        for card in entry.get("skills") or []:
            name = card.get("name", "")
            norm = name.strip().casefold()
            if not norm:
                continue
            category = card.get("category", "").strip()
            evidence = card.get("evidence", "").strip()
            # Defensive weight coercion: the store should hold a 1-3 int, but a stray
            # value must not distort a tooth — clamp to [1, 3] (booleans are not ints here).
            raw_weight = card.get("weight", 1)
            weight = raw_weight if isinstance(raw_weight, int) and not isinstance(raw_weight, bool) else 1
            weight = max(1, min(3, weight))
            raw_signals = card.get("signals")
            signals = set(raw_signals) if isinstance(raw_signals, list) else set()

            slot = merged.get(norm)
            if slot is None:
                slot = {
                    "name": name.strip(),
                    "category": category,
                    "evidence": evidence,
                    "first_key": (project_name, name.strip()),
                    "projects": set(),
                    "total_weight": 0,
                    "signals": set(),
                }
                merged[norm] = slot
            else:
                # Keep the canonical name/category/evidence from the smallest
                # (project, name) so the chosen text is stable regardless of push order.
                if (project_name, name.strip()) < slot["first_key"]:
                    slot["first_key"] = (project_name, name.strip())
                    slot["name"] = name.strip()
                    slot["category"] = category
                    slot["evidence"] = evidence
            slot["projects"].add(project_name)
            slot["total_weight"] += weight
            slot["signals"].update(signals)

    # Build the wire cards: derive depth, sort projects, and order signals canonically.
    skills = [
        {
            "name": slot["name"],
            "category": slot["category"],
            "depth": _skill_depth(slot["total_weight"], len(slot["projects"])),
            "projects": sorted(slot["projects"]),
            "evidence": slot["evidence"],
            "signals": [s for s in _SKILL_SIGNAL_ORDER if s in slot["signals"]],
        }
        for slot in merged.values()
    ]

    # Category order: strongest groupings first (by summed depth), tie-broken by name —
    # so the comb leads with the developer's deepest area. Deterministic for the SPA.
    depth_by_category: dict[str, int] = {}
    for s in skills:
        depth_by_category[s["category"]] = depth_by_category.get(s["category"], 0) + s["depth"]
    categories = sorted(
        depth_by_category, key=lambda c: (-depth_by_category[c], c)
    )
    category_rank = {c: i for i, c in enumerate(categories)}

    # Flat skills list ordered by (category rank, tallest tooth first, then name) so the
    # SPA can render each category group in a sensible, stable order without re-sorting.
    skills.sort(key=lambda s: (category_rank[s["category"]], -s["depth"], s["name"]))

    return {
        "scope": {
            "unrestricted": allowed is None,
            "projects": None if allowed is None else sorted(allowed),
        },
        "categories": categories,
        "skills": skills,
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
            "items" (that project's checklist, or None), and "observations" (its
            observed_history, for slippage). The server fetches + filters before calling,
            mirroring serialize_portfolio.
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
    """
    buckets: dict[str, list] = {"overdue": [], "this_week": [], "later": []}
    summary = {"overdue": 0, "due_this_week": 0, "slipping": 0}
    # _deadline_state's three open-deadline states → the design's three time buckets.
    bucket_of = {OVERDUE: "overdue", "due_soon": "this_week", _UPCOMING: "later"}
    for proj in projects:
        items = proj.get("items") or []
        slipping = slipping_item_keys(proj.get("observations") or [], today)
        for item in items:
            if item.get("done"):
                continue  # finished — off the timeline
            state = _deadline_state(item.get("due_date"), today)
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


def _checklist_rows(items: list, today: date, slipping: set) -> list[dict]:
    """Serialize checklist items into the dashboard's per-item row shape.

    Args:
        items: The checklist items (validated {"text", "done"[, ...]} dicts), in file order.
        today: The reference date (display zone), for the per-item state derivation.
        slipping: The project-wide set of slipping item keys (from slipping_item_keys).

    Returns:
        A list of row dicts {text, done, due_date, key, group, state, status, slipping}.

    Why:
        The aggregate checklist AND each per-producer checklist (C3 Inc 2) render the SAME row
        shape, so building it lives in one place (DRY). `state` is self-contained per item;
        `slipping` is a PROJECT-LEVEL signal (derived from the shared observation history), so a
        producer's rows reuse the same set the aggregate uses — true per-producer slippage
        (per-producer observation history) is out of scope here.
    """
    return [
        {
            "text": item["text"],
            "done": bool(item.get("done")),
            "due_date": item.get("due_date"),
            "key": item.get("key"),
            "group": item.get("group"),
            "state": _item_state(item, today),
            # The raw observed status (E2 Inc 4, gap 8): None for items without one. Shipped
            # alongside the derived `state` so the tracker's circular indicator renders the
            # in_progress/submitted treatment directly, and so future consumers get status as
            # a first-class fact rather than re-deriving it from `state`.
            "status": item.get("status"),
            "slipping": _item_key(item) in slipping,
        }
        for item in items
    ]


def serialize_project(
    *,
    name: str,
    kind: str,
    reports: list[dict],
    checklist: list | None,
    observations: list[dict],
    producer_checklists: list[dict],
    discussions: list[dict],
    today: date,
) -> dict:
    """Serialize one project's full detail (/api/projects/:name).

    Args:
        name: The project name.
        kind: The project's kind ("project" | "tracker").
        reports: The project's reports newest-first (store.history).
        checklist: The live checklist items (store.get_checklist), or None.
        observations: The project's observed history (store.observed_history), for slippage.
        producer_checklists: Each identified producer's own live checklist
            (store.producer_checklists_for) — {"author_name", "items"} per producer, for the
            per-producer cards (C3 Inc 2). Empty for a legacy-only / single-writer project.
        discussions: The project's discussion thread oldest-first
            (store.discussion_items_for_project) — the supervisor-interaction loop (E2 Inc 5).
            The single conversation surface since KI-28 Stage 2 retired per-report comments.
        today: The reference date (display zone).

    Returns:
        The project-detail shape: stats, milestones, checklist, producer_checklists, reports,
        discussions.

    Why:
        One project page draws from four stores (reports, live checklist, observation
        history, discussion thread) plus three derivations (milestones, slippage, per-item
        state).
        Assembling them here keeps server.py a thin fetch-and-emit and makes the whole shape
        unit-testable with fixed inputs and a fixed `today`. slipping is resolved once (a set
        of keys) and applied to both the per-item rows and the milestone roll-ups so they
        always agree.
    """
    slipping = slipping_item_keys(observations, today)
    items = checklist or []
    done = sum(1 for item in items if item.get("done"))

    # Per-milestone slipping: a group slips when any of its OPEN items is in the slipping
    # set. milestones() returns group summaries without item keys, so we resolve membership
    # here from the same checklist + slipping set.
    milestone_rows = []
    for m in milestones(checklist, today):
        group_slipping = any(
            _item_key(item) in slipping
            for item in items
            if item.get("group") == m["group"] and not item.get("done")
        )
        milestone_rows.append({**m, "slipping": group_slipping})

    checklist_rows = _checklist_rows(items, today, slipping)

    # C3 Inc 2: one card per identified producer, each the same row shape as the aggregate.
    # slipping is the shared project-level set (see _checklist_rows). Empty list ⇒ the SPA
    # simply shows no per-producer section (single-writer / legacy projects render unchanged).
    producer_checklist_rows = [
        {
            "author_name": pc["author_name"],
            "progress": _progress(
                sum(1 for item in pc["items"] if item.get("done")), len(pc["items"])
            ),
            "items": _checklist_rows(pc["items"], today, slipping),
        }
        for pc in producer_checklists
    ]

    # Per-project ordinal for each report (the timeline shows #N, not the gappy global id).
    report_numbers = _report_numbers(reports)

    return {
        "name": name,
        "kind": kind,
        "description": None,  # contract gap 5: no project description field stored yet
        "stats": {
            "progress": _progress(done, len(items)),
            "next_due": _next_due(checklist, today),
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
                "source_tags": [],  # contract gap 4: collector set not stored
            }
            for r in reports
        ],
        "discussions": [_discussion_item(d) for d in discussions],
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
) -> dict:
    """Serialize one report in full (/api/reports/:id).

    Args:
        report: The report (store.get / _row_to_report shape).
        checklist: The report's project's live checklist (store.get_checklist), for the rail
            snapshot, or None.
        history: The project's reports newest-first (store.history), for prev/next nav.
        today: The reference date (display zone), for the snapshot rows' states.

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
                    "state": _item_state(item, today),
                    "due_date": item.get("due_date"),
                }
                for item in snapshot_items
            ],
        },
        "nav": _report_nav(report["id"], history, numbers),
    }

<!-- =========================================================================
docs/e2-inc3-kickoff.md
---------------------------------------------------------------------------
Responsible for: The kickoff for E2 Inc 3 — the forward-looking planning layer
                 (rung 1 of the knowledge-base ladder: due-dates, at-risk,
                 slippage, derived milestones), built on an observe-and-remember
                 (never originate) model.
Role in project: Read it at the START of the session, THEN build unit by unit
                 (each is its own PR; stop for review at each boundary). The
                 strategic forks are already settled here. Roadmap:
                 plans/orion-plan.md (Horizon E table + E2 ladder). Strategy
                 invariant: docs/orion-strategy.md. Built on: E2 Inc 2.6 (the
                 tracker collector) + the consolidation slice (PRs #52-#54).
                 Deploy: docs/deployment.md. Access model: docs/dashboard-auth.md.
========================================================================= -->

# Kickoff: E2 Inc 3 — forward-looking layer, rung 1 (observe + remember)

## Context

E2 Inc 3 is the forward-looking planning layer (E1): surfacing **due-dates, at-risk, and
milestones** so family/the developer can see at a glance what's due and what's slipping. The
roadmap flagged it as "the heavy one, gated on the forward-state schema decision." That gate
is now settled.

**The settled decision (2026-06-26):** Orion stays an **observer/reframer**, never an author.
The user authors plans elsewhere (the incubator is the authoring surface); Orion ingests and
**reframes** projects/tasks/ideas as a **knowledge base** with **memory over time**. Memory is
required, not optional: reframing whether a project is slipping — and later, whether two
projects connect — needs history, which a point-in-time read of today's docs cannot give.

The key distinction that resolves the "derive vs persist" question: the line is **observe vs
originate**, not derive vs persist. This increment *persists*, but only **observed** state — a
**downstream projection** of what the source docs claimed over time, rebuildable from the
append-only record. Nothing is authored inside Orion. So:

- **Invariant HELD:** "Orion does not *originate* forward facts." Authoring stays at the
  incubator/source docs.
- **Invariant CLARIFIED (small doc change, not a rewrite):** the strategy's older "no new
  forward-state schema" line becomes "no *authored* forward state; *observed* state may be
  remembered over time as a downstream projection." Reconciles with KI-8 (which removed *dead*
  state; this adds *live, purposeful, re-derivable* memory).

The knowledge-base "brain" lives **relay-side** (the only place all projects are visible
together); the local side stays "parse the docs and deliver," so local-first holds. Inc 3 is
**rung 1** of a ladder — it builds the observe-and-remember data layer + concrete single-project
forward views, with a **cross-project-capable** data model so the connection work (E4) and the
sectioned-dashboard frontend redesign land additively later.

## Architecture

Four layers, flowing the existing pipeline (collector → blob → relay → render), all additive:

1. **Parse (local).** The deadline material already exists in the tracker docs: the parser
   collects every `- **Field:**` into `Section.fields` and every table cell into `Table.rows`,
   so a `- **Deadline:**` line / `Due` column is *already parsed and then discarded*
   (`collectors/tracker.py` explicitly defers it to "E2 Inc 3"). Add a `_parse_deadline` helper
   (symmetric to `_canonical_status`) and carry it on the item.
2. **Carry (wire).** Extend `ChecklistItem` (`collectors/tasks.py`) additively — the dataclass
   docstring already reserves a seam for "a section/heading group" and additive fields. Add
   `due_date: str | None = None` and `group: str | None = None`. `serialize_blob`
   (`report.py`) emits them per item when present (optional-field pattern). The relay ingest
   already tolerates extra item fields (`_checklist_items_error` checks only `text`/`done`).
3. **Remember (relay store).** A first-class **observational projection**: a new
   `observed_item(project, item_key, due_date, done, observed_at)` table, recorded on each
   `/checklist` push and report ingest. **Rebuildable** from the append-only record → a
   projection, not authored truth. This is *required* for the checklist-only `applications`
   project, whose live checklist is a single upserted row today (no history anywhere).
4. **Derive + render (relay).** Pure functions compute overdue / due-soon / at-risk / slippage
   / milestone roll-ups from the live checklist + `observed_item` history; the dashboard
   surfaces them, reusing `_time_tag` + the existing relative-time JS (already renders "in 5
   days" / "3 days overdue") and extending the checklist class+glyph CSS pattern.

### Coupling & parallelization map

- **Unit 1 (local parse/carry)** is upstream of everything; self-contained.
- After Unit 1, **Unit 2 (derive+surface static views)** and **Unit 3 (memory store)** are
  independent (derive-from-current vs accumulate-history) → the parallelizable seam. **Unit 4
  (slippage)** depends on 3. **Unit 5 (milestones)** depends on 1's `group` + 2's render seam.
- Per the repo's sequential-review discipline, ship as ordered PRs even where 2∥3 could fan
  out. Update `docs/parallelization.md` if the analysis shifts.
- The `observed_item` table is keyed `(project, item_key)` — already **cross-project-capable**;
  the cross-project *connection* derivation/UI is a later rung (E4), additive on this shape.

## The unit ladder (each its own PR; stop for review at each boundary)

**Unit 0 — strategy-doc clarification (docs-only, do first).** Amend `docs/orion-strategy.md`
and the plan's hard-constraints framing per the "observe vs originate" clarification above.
Small, but it's a written-invariant change — land it explicitly before the code so the
increment's premise is on record.

**Unit 1 — Parse & carry deadlines (local; no surfacing yet).**
- `collectors/tracker.py`: `_parse_deadline()` reading `section.fields` (`deadline`/`due`) and a
  `Due`/`Deadline`/`Target` table column (reuse the case-insensitive `_task_column` matching);
  normalize to ISO `YYYY-MM-DD`; unparseable → `None` (never fail the parse).
- `collectors/tasks.py`: add `due_date: str | None = None` to `ChecklistItem` (additive).
- `report.py`: `serialize_blob` emits `due_date` per item when present.
- Tests: accepted/garbage date formats; item carries `due_date`; blob round-trips; no-deadline
  path unchanged. **Local-only → no deploy.**

**Unit 2 — Derive & surface STATIC forward views (dashboard; first visible win).**
- `relay/store.py`: confirm `get_checklist` decodes the new per-item fields (they ride the
  stored `items` JSON).
- Derivation (pure functions): `overdue` (due < today & open), `due_soon` (due ≤ N days & open),
  `at_risk` (overdue ∪ due_soon). Default N = 7 (a constant; per-project config later).
  "today" = relay `display_tz`; a date-only deadline = end-of-day in that zone.
- `relay/render.py`: per-item due date via `_time_tag` (relative-time JS gives "in/overdue");
  overdue/at-risk class+glyph extending the checklist pattern; `.overdue`/`.at-risk` tokens in
  `_PAGE_CSS` (hash auto-tracks); an at-risk count badge on the portfolio card
  (`render_portfolio` + `latest_report_per_project`).
- Tests: derivation truth table; render shows due date + at-risk treatment; portfolio badge.
  **Relay → `fly deploy` after merge; eyes-on.**

**Unit 3 — Remember observations over time (the projection store).**
- `relay/store.py`: `observed_item(project, item_key, due_date, done, observed_at)`; record on
  `/checklist` push (`relay/server.py`) and report ingest. **`item_key` = a stable
  title/group-based identity, NOT the status-embedded `text`** (tracker text embeds status, so
  text changes when status does — see Design decisions). Rebuildable from the append-only
  record.
- Tests: observations accumulate; identity stable across a status change; rebuild matches.
  **Relay → deploy.**

**Unit 4 — Slippage derivation + surfacing.**
- Derive "slipping" from `observed_item` history (due_date moved later for the same `item_key`,
  and/or long-open-past-due); surface a "slipping" indicator on the item/project.
- Tests (history fixtures) + eyes-on. **Relay → deploy.**

**Unit 5 — Derived milestones.**
- `ChecklistItem.group` set by the tracker (section heading / table name) and serialized;
  derive milestones by grouping items → per-group progress %, nearest open deadline, at-risk
  roll-up. Render a per-project "Milestones" view; portfolio nearest-milestone hint. Projects
  without a structured tracker simply get no milestones (graceful).
- Tests + eyes-on. **Relay → deploy.**

## Key design decisions (settle in each unit's plan-mode pass)

- **Item identity for memory (`item_key`):** must be stable across status changes, so derive it
  from the item **title/group**, not the status-embedded `text`. Prior art + caveat: KI-6
  (text-based identity). Consider a new KI documenting the forward-store's identity model and
  its rename/dedupe edge cases.
- **Accepted deadline formats:** ISO `YYYY-MM-DD` primary; support a small documented set;
  unparseable → ignored (never fail). Field names `- **Deadline:**` / `- **Due:**`; columns
  `Due` / `Deadline` / `Target`.
- **At-risk thresholds:** defaults only this rung (overdue / due ≤ 7 days / slipping); a
  per-project `due_soon_days` config knob is a later add (build the seam, not the future).
- **Timezone:** compute against the relay `display_tz`; date-only = end-of-day in that zone.
- **Milestone grouping source:** tracker section headings / table names via `group`. No
  structured tracker → no milestones.

## Living docs (update in the same sessions as the code)

- `docs/orion-strategy.md` — the observe-vs-originate clarification (Unit 0).
- `plans/orion-plan.md` — re-sync the **canonical E2 row** + ladder as each unit lands (the
  roadmap table is the canonical map).
- `docs/known-issues.md` — add a KI for the forward-store item-identity model if warranted;
  keep it a living doc.
- `CHANGELOG.md` — an entry per shipped unit (or one slice entry as the rung completes).
- `docs/parallelization.md` — update if the coupling analysis shifts.

## Verification

- **Per unit:** `PYTHONPATH=src pytest` green, run from the **main checkout** (worktree
  editable-install gotcha).
- **End-to-end eyes-on (the point of the rung):** add a `- **Deadline:**` to a tracker doc
  (the live `applications` tracker is the natural real workload — application due dates), push
  the checklist, open `https://orion-relay-horizon-c.fly.dev/` logged in as admin, and confirm:
  the item shows its due date (relative time), an approaching/passed deadline renders
  at-risk/overdue, the portfolio card shows an at-risk count, and a milestone roll-up appears.
  Then push again with a **moved** deadline and confirm slippage shows.
- **Deploy:** Units 2–5 touch the relay → `fly deploy` after each merge (Unit 0 docs / Unit 1
  local need no deploy). CI is quota-capped until 2026-07-01 → verify on local-green, merge,
  deploy.

## Sequencing / boundaries

Order: **Unit 0 (strategy doc)** → **Unit 1 (parse/carry)** → **Unit 2 (static views + first
deploy)** → **Unit 3 (memory store)** → **Unit 4 (slippage)** → **Unit 5 (milestones)**. Code
via branch + PR (`main` is PR-gated); stop for review at each boundary. **Out of scope for this
rung (later ladder steps):** cross-project connection/knowledge-graph (E4), the sectioned-
dashboard frontend redesign (user-driven), scheduling/cadence convergence (KI-13), and any
authoring surface (held firm at the incubator).

## Adjacent future rung (recorded 2026-06-26, deferred) — disciplines & directions

A separate dimension of the knowledge base, not part of this rung: Orion tracking **global and
project-specific disciplines/directions** and the **why** behind them, surfaced as a dashboard
"disciplines & directions" doc/section (per-project + global). Same observe-not-originate model
— disciplines are authored *elsewhere* (`CLAUDE.md` global/project files, design/strategy docs,
decision records); Orion **observes and surfaces** them, never writes them. Architecturally a
new **signal/collector** (doc-centric) plus a dashboard **section**, pairing with memory (track
how disciplines evolve, and why). Value: it makes a project legible — a third party (family,
supervisors) sees *how the developer orchestrates and builds*, which is especially strong in the
showcase lens. **Design note:** keep it a distinct additive signal + view (doc-centric), NOT
folded into the item-centric `observed_item` forward-state store — separate shapes under the one
knowledge-base umbrella. Slots into the sectioned-dashboard vision (projects / to-dos /
scheduling / disciplines). Deferred; record it, don't build it in rung 1.

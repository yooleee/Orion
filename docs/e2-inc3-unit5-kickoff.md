<!-- =========================================================================
docs/e2-inc3-unit5-kickoff.md
---------------------------------------------------------------------------
Responsible for: The grounded kickoff for E2 Inc 3 **Unit 5 — derived
                 milestones**, the LAST unit of rung 1. The group-mapping
                 design is settled here; the build is a vertical slice.
Role in project: Read this at the START of the next session, THEN build (one
                 PR; stop for review at the boundary). Master ladder:
                 docs/e2-inc3-kickoff.md. Roadmap: plans/orion-plan.md
                 (Horizon E / E2 Inc 3 row). Built on: Units 0-4 (PRs #55-#59),
                 all shipped AND deployed to the live relay (version 15).
========================================================================= -->

# Kickoff: E2 Inc 3 — Unit 5 (derived milestones), the last rung-1 unit

## Where we are (context)

Rung 1 of the forward-looking layer is **nearly complete**. Units 0-4 have shipped and the
relay is deployed through **version 15** (`orion-relay-horizon-c.fly.dev`):

- **Unit 0** — strategy invariant clarified (observe + remember, never originate). PR #55.
- **Unit 1** — `_parse_deadline` + `ChecklistItem.due_date`, carried on the wire. PR #56.
- **Unit 2** — `relay/derive.py` (overdue / due-soon / at-risk); due dates + at-risk treatment
  on the project page; "N at risk" portfolio badge. PR #57, deployed.
- **Unit 3** — `relay_observed_items` append-only memory store + a stable `item_key`
  (`ChecklistItem.key` = the tracker's bare title). Identity model in **KI-21**. PR #58, deployed.
- **Unit 4** — slippage (`is_slipping` / `slipping_item_keys`): deadline moved later, or
  lingering open past due; a per-item "↘ slipping" marker + an "N slipping" portfolio badge.
  PR #59, deployed.

**Unit 5 is the last unit of rung 1.** After it, the rung is done (cross-project connection is
E4, the sectioned-dashboard redesign is user-driven, and `group` for richer milestones can grow
later — all out of scope here).

## Goal

Derive **milestones** by grouping checklist items, and surface them: per-group **progress**,
**nearest open deadline**, and an **at-risk roll-up**. Render a per-project **"Milestones"** view
and a portfolio **"next milestone"** hint. A project without a structured tracker simply gets no
milestones (graceful — the tasks/checkbox collector sets no group).

## The settled design decision — how `group` is derived (the crux)

A milestone is a *group* of checklist items. The grouping comes from the tracker's document
structure, carried on a new additive `ChecklistItem.group` field (mirroring `due_date`/`key`):

- **Application items** (`_application_items`) → **`group = "Applications"`**. The numbered
  `## N. Title` sections are all applications, so one "Applications" milestone (e.g. 0/4 done,
  nearest deadline) is more useful than four groups of one. (Chosen over type-based grouping —
  job/program/course/internship from the title parenthetical — which would mostly yield
  groups of one; and over the raw H1 "Applications To-Do", to keep the label clean.)
- **Table items** (`_table_items`) → **`group = the table's nearest preceding heading`** (any
  level), so the main "Non-Application To-Do" table and each `### Task N` breakdown table become
  their **own** milestones. This needs a small enhancement to the parser (below).
- **Tasks/checkbox items and any ungrouped item** → **`group = None`** → no milestone.

### Required parser enhancement (`collectors/_markdown.py`)

`parse_tables()` today returns `Table(headers, rows)` with **no reference to the heading a table
sits under** (it scans pipe-tables independently of headings — confirmed by reading the file).
So `_table_items` can't currently know a table's group. Fix, additively:

- Add `heading: str | None = None` to the `Table` dataclass.
- In `parse_tables()`, track the most recent heading line seen while scanning (reuse
  `_HEADING_RE`, which already matches levels 1-6), and set it on each `Table` it emits. A table
  with no preceding heading gets `None`.
- This is purely additive: the existing `Table(headers=..., rows=...)` shape and both current
  callers (`tracker._table_items`, the `add-project --seed-tasks-from` bootstrap) are unaffected
  by a new defaulted field.

## File-by-file plan (one vertical slice — local `group` → wire → relay milestone view)

This mirrors Unit 3's shape (a producer-supplied field the relay consumes). ~8-9 files; cohesive.

1. **`src/orion/collectors/_markdown.py`** — `Table.heading` field + `parse_tables` tracks the
   nearest preceding heading. (The enabling change — do it first.)
2. **`src/orion/collectors/tasks.py`** — add `group: str | None = None` to `ChecklistItem`
   (additive; update the docstring to list due_date / key / group as the additive fields).
3. **`src/orion/collectors/tracker.py`** — `_application_items` sets `group="Applications"`;
   `_table_items` sets `group=table.heading`.
4. **`src/orion/report.py`** — `serialize_checklist_item` emits `group` when present (mirror the
   `due_date`/`key` None-omitted pattern; one line).
5. **`src/orion/cli.py`** — `_redacted_checklist` redacts + carries `group` (a heading is user
   text, so redact it like `key`; do **not** re-count its hits — same reasoning as `key`).
6. **`relay/store.py`** — `get_checklist` needs **no change** (`group` rides the stored items
   JSON, like `due_date`/`key`). `latest_report_per_project` computes the portfolio
   **`nearest_milestone`** hint (the soonest milestone group + its nearest open deadline) from the
   live items, **today-gated** like `checklist_at_risk` / `checklist_slipping`.
7. **`relay/derive.py`** — `milestones(checklist, today)` → a list of per-group dicts
   `{group, done, total, at_risk, nearest_due}`, in first-seen order; ungrouped items excluded;
   `[]` when nothing is grouped. Reuse `count_at_risk` for the at-risk roll-up. `nearest_due` =
   the soonest open item's `due_date` in the group (None if none).
8. **`relay/server.py`** — the project handler already fetches `get_checklist`; it just passes the
   checklist (which now carries `group`) — `render_project` derives milestones at render. (Verify
   no extra wiring is needed; the home handler already passes `today` for the portfolio hint.)
9. **`relay/render.py`** — `_render_milestones(milestones)` → a "Milestones" `<section>` on the
   **project page** (place it near the checklist), each row e.g.
   `Applications — 0/4 · next due Jun 12 · 2 at risk`; a portfolio **"next milestone"** hint on the
   card from `nearest_milestone`. Add CSS tokens (the CSP hash auto-tracks `_PAGE_CSS`). Reuse
   `_due_span`/`_MONTH_ABBR` style for any date rendering; escape everything via `_esc`.

## Key decisions

**Settled (in this kickoff):**
- `group` mapping: apps → `"Applications"`; table rows → nearest preceding heading; else None.
- `Table.heading` is the additive parser enhancement that enables table grouping.
- Milestone shape: `{group, done, total, at_risk, nearest_due}`. At-risk roll-up only — **slipping
  roll-up is deferred** (slipping is shown per-item in Unit 4; add a milestone slipping count later
  if wanted — build the seam, not the future).
- `group` is redacted on the structured lane (like `key`), hits not re-counted.

**Open (settle in the plan-mode pass tomorrow):**
- **Milestones section placement** — above the checklist (summary-first) or below it. Lean: above,
  as an at-a-glance roll-up before the item detail.
- **Portfolio hint scope** — a single "next milestone" line per card (recommended, minimal), vs. a
  fuller per-group breakdown on the card (too heavy for the home). Lean: the single line.
- **App `group` label** — `"Applications"` (recommended) vs. the document H1. If the tracker is
  ever used for a non-applications doc, revisit; out of scope now.

## Verification

- **Per unit:** `PYTHONPATH=src pytest` green, run from the **main checkout** (worktree
  editable-install gotcha). New tests: `Table.heading` is the nearest preceding heading; tracker
  app items group=`"Applications"` and table items group=heading (tasks group=None); serialize +
  redaction carry `group`; `milestones()` truth-table (grouping, progress, nearest deadline,
  at-risk roll-up, ungrouped excluded, empty when nothing grouped); render shows the Milestones
  section + the portfolio hint.
- **Eyes-on (the rung's finish):** render the project page for the **live applications tracker**
  and confirm two milestones — **"Applications"** (the 4 numbered apps: progress, nearest deadline
  June 12, at-risk count) and **"Non-Application To-Do"** (the table rows) — plus the portfolio
  "next milestone" hint. Use the local render-to-file + screenshot approach from Units 2/4.
- **Deploy:** relay-side → `fly deploy` after merge (the **final rung-1 deploy**). CI is
  quota-capped until 2026-07-01 → verify on local-green, merge, deploy.

## Boundary / housekeeping

- One PR; stop for review at the boundary, then the final deploy completes rung 1.
- `git` is healthy: `main` matches `origin/main`; the `backup/local-main-pre-reset` safety branch
  from the 2026-06-26 divergence fix is still parked and can be deleted once you're satisfied.
- After Unit 5: rung 1 is **done**. The forward-looking layer observes deadlines, remembers them
  over time, flags at-risk and slipping, and rolls up milestones — all observe-not-originate.

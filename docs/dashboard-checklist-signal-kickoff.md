<!-- =========================================================================
docs/dashboard-checklist-signal-kickoff.md
---------------------------------------------------------------------------
Responsible for: The kickoff for E2 Increment 2 — surfacing the to-do/milestone
                 CHECKLIST signal on the relay dashboard (the next rung after the
                 portfolio overview, Inc 1).
Role in project: A kickoff doc (like the archived phase kickoffs and the
                 dashboard-hardening kickoff). Read it at the start of the next
                 session, THEN do a plan-mode scoping pass — unlike the hardening
                 slice, this one has real design decisions to settle before code.
                 Roadmap: plans/orion-plan.md "Horizon E" table + the E2 ladder note.
                 Access model: docs/dashboard-auth.md. Deploy: docs/deployment.md.
========================================================================= -->

# Kickoff: surface the to-do/milestone checklist on the dashboard (E2 Inc 2)

## Why

Inc 1 (portfolio overview, shipped + deployed 2026-06-25) made the dashboard home a
cross-project surface, but it still shows only prose progress (git-derived reports). The
user's core want is the **checklist signal** visible: what's **done** and what's **open /
planned**, so family can see what he's working on and intends to do, and comment back. This
is the second rung of the E2 dashboard-visibility ladder, validated by the project's founding
family-visibility intent (not the dogfood, which tested reporting).

**Depth target (decided): "mirror the current checklist."** Show done + open items parsed
from the user's existing checklist file. This stays **reframing, not originating** — Orion
reads the file the user already maintains and holds no new authored state. It is explicitly
**short of** the forward-looking planning layer (due dates, sprints, "at-risk"); that is
Inc 3 (≡ E1), gated on a forward-state schema decision, and out of scope here.

## The data-flow reality (the crux — confirmed in code this session)

Surfacing a real checklist is **not** a render tweak. Item-level structure does not reach the
dashboard at all today; it is flattened to prose at every stage:

- **Collector** (`src/orion/collectors/tasks.py`) is **retrospective-only**: `_COMPLETED_RE`
  matches only `[x]` items and **deliberately excludes** open `[ ]` items (line 46-47). It
  emits a Markdown bullet string of *newly-completed* items as `raw_text`, plus a marker that
  is the JSON set of completed item texts. **Open/planned items are never captured.**
- **Blob** (`report.py serialize_blob`): the structured signal becomes a `sections` entry, a
  `[title, body]` **text** pair. No item-level / done-state structure survives.
- **Store** (`relay/store.py relay_reports`): `sections` is stored as JSON text; there is **no
  structured column** for checklist items.
- **Render** (`relay/render.py`): shows section bodies as prose `<pre>`.

So Inc 2 is a **vertical slice through the whole pipeline**: collector → blob field → store
column → render. Per `docs/parallelization.md`, this **spans the `relay/` ⟂ `src/orion/`
seam** (it is not a relay-only change like Inc 1) and touches the blob contract and the
config spine, so build it as one coordinated slice.

## Scope (this increment)

Capture the **current checklist state** (each item + done/open) from the existing
per-project `tasks_file`, carry it additively to the relay, and render it on the dashboard.
Additive and reframing-only. No forward-looking metadata. No new always-on state.

## The vertical slice (sketch — settle the details in the plan-mode pass)

1. **Collector** — extract the **full current checklist** (open `[ ]` and done `[x]`) as
   structured items, preserving file order and any section/heading grouping. **Invariant: do
   NOT break the existing retrospective "newly completed" report behavior** — the prose
   section that today feeds reports/messages must keep working. The structured snapshot is an
   *addition* alongside it.
2. **Blob** — a new **optional** structured field (e.g. `checklist`: a list of
   `{text, done}`, possibly grouped). Optional so old producers/blobs still validate
   (`_validate_blob` in `relay/server.py` treats it as not-required, like `source_marker`).
3. **Store** — a new JSON column on `relay_reports` (additive, decoded in `_row_to_report`,
   mirroring `sections`).
4. **Render** — a checklist view: a "Tasks" / "Checklist" block on the **report page** (done
   vs. open, escaped), and optionally a small "3/5 done" summary on the **portfolio card**.
   CSP-safe (styling in `_PAGE_CSS`; no new inline `<style>`/`<script>`/`style=`).

Checkpoint it as smallest-reviewable units (collector+blob, store, render), each with tests.

## Open decisions to settle in the plan-mode pass

1. **Per-report snapshot vs. project-level "live" checklist.** A report is a point-in-time
   delta; a checklist is current state. **Default-lean: attach a snapshot to each report**
   (the checklist as-of that report's generation), so it rides the existing additive
   blob→store→render pipeline with no new project-level state and stays reframing. Decide
   explicitly — a project-level live checklist is different (and bigger) plumbing.
2. **Collector shape.** How `tasks.py` returns BOTH the existing newly-completed prose AND the
   new structured snapshot — a new field on `CollectorResult`, or a sibling return — without
   regressing the current report/message behavior or its tests.
3. **Grouping / sections.** Whether to preserve checklist headings (e.g. `## Milestone A`) as
   groups, or render a flat list first (simpler; grouping additive later).
4. **Where it renders.** Report page only, or also a count badge on the portfolio card and/or
   the project page. Function-first; pick the minimum that delivers the visible value.
5. **Redaction.** Checklist items are user text → the structured-lane redaction safety net
   still applies before they leave the machine. Confirm the path covers the new field.

## Constraints to carry

- **Reframing, not originating** — read the existing file; introduce no authored/forward state.
- **CSP-safe** — the dashboard runs a hash-based CSP; styling goes in `_PAGE_CSS` (hash
  auto-recomputes, pinned by the contract test), no new inline blocks or `style=` attributes.
- **Function before looks** — minimal usable layout; the aesthetic pass is a separate slice.
- **Cross-platform, stdlib-only, fully annotated** (house style). Spans the relay⟂CLI seam and
  touches the `config.py`/`cli.py` spine if a new knob is added — one owner for the spine.
- **PR-gated** (relay + core code). Merge on local-green while CI is capped (until 2026-07-01).

## Verification

- `PYTHONPATH=src python -m pytest -q` green (collector units for open+done extraction and the
  retrospective-behavior-unchanged invariant; blob/store round-trip of the new field; render
  units incl. escaping; an end-to-end checklist-on-the-dashboard test).
- **Eyes-on:** run the relay locally with a seeded report carrying a checklist; confirm the
  done/open items render, no CSP violation, and the existing report/message prose still works.
  Then verify against the live Fly relay after merge + deploy.

## Out of scope (named, deferred)

Forward-looking planning layer — due dates, sprints, "at-risk", progress-over-time (Inc 3 ≡ E1,
forward-state schema). The no-login guest view (viewer logins suffice for family today).
Non-project / non-code items (e.g. applications). The dashboard aesthetic pass. Chat-surface
enrichment (E3, parked).

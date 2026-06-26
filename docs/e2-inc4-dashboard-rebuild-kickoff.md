<!-- =========================================================================
docs/e2-inc4-dashboard-rebuild-kickoff.md
---------------------------------------------------------------------------
Responsible for: The grounded kickoff for E2 Inc 4 — the sectioned dashboard
                 rebuild (a richer-client SPA recreating the design/ handoff).
Role in project: Read this at the START of the next session, THEN plan the
                 first slice in plan mode and build. Builds on: rung 1 complete
                 (E2 Inc 3, PR #60, deployed). Roadmap: plans/orion-plan.md
                 (Horizon E / E2 row). Content brief: docs/dashboard-design-brief.md.
                 Visual spec: design/ (README + screenshots + .dc.html prototypes).
========================================================================= -->

# Kickoff: E2 Inc 4 — the sectioned dashboard rebuild (richer-client SPA)

## Where we are

Rung 1 of the forward-looking layer (E2 Inc 3, Units 0–5) is **complete and deployed** (PR #60,
2026-06-26). The dashboard now observes deadlines, remembers them over time, flags at-risk and
slipping, and rolls up milestones — all observe-not-originate. The current dashboard is the **stdlib
server-rendered** one in `relay/render.py` + `relay/server.py`.

A full **visual design** has been produced (via Claude design) and committed under
[`design/`](../design/): a README spec, 11 desktop screenshots, 3 themes (Dark / **Sepia default** /
Light), a mobile pass, and interactive `.dc.html` prototypes. It is a complete, high-fidelity
**single-page-app** spec for a **sectioned** dashboard. The README is explicit: recreate it in a real
frontend stack (it recommends React + CSS-variable theming), **do not ship the prototype HTML**.

## Goal

Rebuild the dashboard as a **richer client** faithful to `design/`, replacing the server-rendered HTML
at parity. Every recorded future band becomes a **section**: Projects, To-dos (Tracker), Scheduling,
Disciplines & directions, Cross-project Connections — plus the Report reader, a public Showcase/guest
view, login, viewer-scoping, empty state, and mobile.

## Settled decisions (from the 2026-06-26 planning session)

- **Stack:** **React + Vite (TypeScript)**, themes via CSS-variable `data-theme` (mirrors how the
  design is authored). React is the design's own recommendation and the developer's preference. (Preact
  is a trivial future swap if bundle size ever matters.)
- **Hosting:** **single host.** The existing **Fly relay serves the built SPA assets + a read-only JSON
  API**. Not two hosts (Cloudflare Pages + Fly was judged not worth the coordination cost). A
  Cloudflare-everything consolidation is a possible *future* migration, not now.
- **Backend shape:** the relay **becomes a read-only JSON API**. It already holds the data
  (`store.latest_report_per_project`, `store.get_checklist`, `store.history`, `derive.milestones`,
  `store.observed_history`, `derive.slipping_item_keys`), per-user auth + `projects_for_user` scoping,
  and a JSON `/api/comments` endpoint. Most of the API is JSON re-serialization of what `render.py`
  already assembles, plus two new derivations (Scheduling, Connections) and one new signal (Disciplines).
- **Retire, don't duplicate:** the server-rendered HTML (`render.py` views) **retires at parity**. The
  relay keeps the JSON API, the comment write path, the auth/cookie/CSP machinery, and the store/derive
  layers. (Keep the old views working until the SPA reaches parity, then remove.)
- **Simplicity reframed:** the project goal moved from "stdlib-minimal / 10-min clone" to "easy and
  straightforward to set up across usability levels." A frontend build step is now acceptable.
  "Minimize complexity" stays live. (See `CLAUDE.md` + `plans/orion-plan.md` Horizon E framing.)

## Invariants that carry through UNCHANGED

- **Observe, not originate.** All domain data is observed from external sources and **read-only** in the
  UI. The **only** user-authored content is comments (the existing write path).
- **Untrusted text renders inert.** In the SPA this is React's default text binding; **never**
  `dangerouslySetInnerHTML` for stored content. The XSS property the old dashboard guaranteed by
  `html.escape` must hold in the new one by construction.
- **Privacy/safety, redaction, preview-before-send, per-user scoping** — all unchanged.
- **State legible without colour alone** — the design keeps glyph + label + colour for every state
  (`○ ◐ ✓ ◷ ▲ △ ↝`), an accessibility requirement *and* an observed product principle.

## Section-readiness map (what the API needs)

| Section | Backend status | Source |
| --- | --- | --- |
| Projects home | **data-ready** | `latest_report_per_project` (+ `milestones`, at-risk/slipping counts) |
| Project page | **data-ready** | `history`, `get_checklist`, `derive.milestones`, `observed_history` |
| Report detail | **data-ready** | `store.get` + comments |
| Tracker / To-dos | **data-ready** | the tracker checklist + `group` (rung 1) |
| Login / viewer-scoping / empty | **ready** | C3 per-user keys + `projects_for_user` |
| Showcase / guest (no-login) | **small new** | a curated, public, read-only surface (the deferred C3-Inc-3 item) |
| **Scheduling** | **small new derivation** | cross-project, time-bucketed aggregation of every project's + the tracker's deadlines |
| **Disciplines & directions** (4b) | **new signal** | a doc-centric collector reading CLAUDE.md / design / decision docs → store → section |
| **Cross-project Connections** (4c) | **new derivation** | cross-project relationships (shared items/topics, feeds) → the SVG graph |

## Coupling / parallelization (the carried thought-process)

- **The JSON API contract is the hub.** Define it early from the design's data needs. Once fixed, it is
  a clean seam: the **frontend (SPA screens)** and the **backend (JSON serializers + new derivations)**
  fan out in parallel against the agreed shapes. Classic API-seam split.
- **Backend bands are mutually independent:** Scheduling aggregation, the Disciplines collector, and the
  Connections derivation touch different code and can be built in any order or concurrently. Only their
  *sections* depend on the SPA shell existing.
- **Frontend is serial at the start** (shell + theming + routing must exist before screens), then
  screen-parallel once the shell is up.
- **Sequence:** API contract + SPA shell + theming first (unblocks everything), then the data-ready
  sections (fastest value), then 4b/4c as their derivations land. Retire `render.py` HTML at parity.
- Living map: [`docs/parallelization.md`](parallelization.md).

## First concrete steps (next session)

1. **Plan-mode pass for slice 4a** (per the repo discipline) — file-by-file, surfacing 4a's open
   decisions (repo layout for the frontend, dev-proxy vs. same-origin, auth-cookie flow for the SPA,
   how the relay serves built assets).
2. **Define the JSON API contract** from the design's data needs (one document or typed schema). Map
   each screen in `design/` to the store/derive call(s) that feed it. This is the seam.
3. **Scaffold the frontend** (`frontend/` or `web/` at repo root) with Vite + React + TS, and **build
   the three-theme token system first** (the README says theming first): the `data-theme` CSS-variable
   blocks from `design/README.md` "Design Tokens".
4. **Build the shell** (sidebar nav + routing) + **Projects home** + **Report detail** against the live
   relay JSON API, checked against the screenshots.
5. Then Tracker + Scheduling, then 4b (Disciplines), then 4c (Connections). Showcase/guest folds into 4a
   or a small 4d.

## Verification

- **Per slice:** the existing `PYTHONPATH=src pytest` stays green for backend changes (run from the main
  checkout — worktree editable-install gotcha). New backend derivations (Scheduling, Connections,
  Disciplines) get pure unit tests like `derive.milestones` did. Frontend gets its own test setup
  (decide in the 4a plan-mode pass — likely Vitest + a component test or two for the state-legibility
  and inert-text guarantees).
- **Eyes-on is the real gate:** the design **screenshots are the visual oracle**. Render each section
  against **live relay data** (the real `applications` tracker + the `orion` project) and compare to the
  matching `design/screenshots/*.png`, across all three themes and the mobile pass. Use the same
  serve-locally + screenshot approach as the rung-1 units.
- **Parity check before retiring `render.py`:** every URL the old dashboard served has a SPA equivalent,
  and the inert-text + scoping guarantees are preserved.

## Boundary / scope

- Inc 4 ships **slice by slice** (4a → 4b → 4c), each its own PR, stop for review at every boundary —
  same discipline as rung 1.
- **Out of scope:** E4 *multi-party* cross-project coordination (C3-gated); the read-write dashboard
  inflection (E5); chat/bots (E3, parked); scheduling/cadence convergence (KI-13); any authoring surface
  (held at the incubator). Cloudflare-everything consolidation is a possible future migration, not now.

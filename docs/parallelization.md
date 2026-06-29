# Parallelization & coupling map

> **This is a standing practice, not a one-off.** For any brainstorming / planning / analysis task on
> Orion, also map **what could proceed in parallel vs. what is intertwined** (see `CLAUDE.md` "How to
> work in this repo" and the carried-lens entry in [`orion-strategy.md`](orion-strategy.md)). It serves
> three ends:
> 1. **Efficiency** — independent tracks can fan out to multiple Claude Code agents (sub-agents in one
>    session, or several sessions on separate branches).
> 2. **Architectural understanding** — a live map of which parts are coupled vs. separable.
> 3. **Verification** — the coupling view surfaces hidden dependencies and risk before they bite.
>
> This file is the **living map**: it is a snapshot that shifts as work lands, so update it when the
> analysis changes. Mechanism constraint we hold to: **Claude Code only** (sub-agents and/or multiple
> Claude Code sessions), **not** cross-harness (no Claude + Codex-style mixing).

_Last synced: 2026-06-29 (E2 **Inc 5 — supervisor-interaction loop SHIPPED + MERGED** (PR #74): the read-write/identity watershed's first concrete step — a per-project, identity-first, **two-way** discussion (supervisor ↔ developer) across **relay + CLI + SPA**. It played out as the predicted **vertical slice, build-coordinated** (store → relay → CLI → SPA against a fixed `/api/discussions` wire): Unit 1 (store + `supervisor` role) gated all; Units 2 (cookie endpoints) and 3 (Bearer machine routes + `orion discussions pull/reply` watermark) shared the wire; Unit 4 (SPA panel) depended only on the fixed read shape. The genuine coupling was the identity/role work touching C3 auth (`relay_users`, `_authenticate`, `_allowed_projects`), kept minimal (an allowlist add + the deliberate non-change of leaving `supervisor` viewer-scoped). On review, comments + discussion were judged **two overlapping conversation systems** (KI-28): **consolidation Stage 1 shipped** (project page = single Discussion surface), **Stage 2 deferred** (data-model unification + a parity migration — the one slice with genuine **live-data coupling**; kickoff `docs/stage2-comments-discussion-consolidation-kickoff.md`). Prior: 2026-06-28 (E2 **4c Skills-comb rework CP1 SHIPPED** — global two-pass `skills-sync` + atomic `/skills-batch` + depth re-tune; CP2 comb visual pending — see the 4c-rework coupling note below). Prior: 2026-06-27 (E2 **Inc 4 COMPLETE** — 4a band + 4b Disciplines + 4c **Skills comb** (reframed from Connections) all shipped). Prior: 2026-06-26 (E2 **Inc 3 rung 1 COMPLETE + DEPLOYED** (PR #60); **Inc 4 4a BUILT (in review)** —
the SPA ⟂ JSON-API seam realized, single-host serving shipped — see the (updated) coupling fact #3 and the
Inc 4 map below, plus the kickoff
[`docs/e2-inc4-dashboard-rebuild-kickoff.md`](e2-inc4-dashboard-rebuild-kickoff.md). The forward-looking
layer. Units 0
(strategy invariant), 1 (local parse/carry of `due_date`), 2 (relay derive + surface due-date/at-risk;
deployed), 3 (the `relay_observed_items` memory store + a stable `item_key`; deployed), 4 (slippage
derivation + surface, relay-side) and **5 (derived milestones)** have all shipped + deployed. The
coupling played out as mapped: Unit 2 was a clean relay-only slice once Unit 1
put `due_date` on the wire; Unit 4 was relay-only too (it reads the history Unit 3 stored); Units 3 and
5 were the two **vertical slices** (local field → wire → relay consumer), because each needs a field
only the producer can supply — Unit 3 a status-independent identity (`key`), Unit 5 the section grouping
(`group`, via a new `Table.heading` on the Markdown parser). Each slice stayed serial top-to-bottom (no
fan-out): every layer consumes the previous. The per-unit coupling map (Unit 1 upstream; Units 2∥3
independent; 4→3; 5→1+2) lives in [`docs/e2-inc3-kickoff.md`](e2-inc3-kickoff.md). Earlier:
dashboard CSP/headers hardening, **E2 Inc 1 — portfolio overview**, **Inc 2 — live checklist signal**
[PR #47], **Inc 2.5** [PR #49], the **CSRF fix** [PR #48] — all shipped + deployed.) Excludes Horizon
P, decision-gated launch work._

## Remaining buildable inventory + file footprint

- **Horizon D follow-ons:** `graduate-idea` (`cli.py` / `scaffold.py` / reads `collectors/incubator.py`);
  KI-20 relay-dashboard timezone (`relay/`); summarizer-prompt style check (`summarize.py`).
- **Deferred C-track:** C2c native Discord bot (`bot/` + `config` platform tuple + `cli` dispatch);
  C2d reply-targeting (`bot/`; uses existing `relay/server` `report_id`); C3 multi-party identity
  (broad: `config` participant graph, `report.py` author field / KI-17, `relay/` auth+identity, `state`).
- **Horizon E:** E1 light planning layer (`state` new tables, `config`, a planning module, `cli`,
  `relay/render`); **E2 dashboard-visibility track — now building incrementally:** Inc 1 portfolio
  overview ✅ shipped (purely `relay/` render+store+server — the clean relay-only seam); Inc 2 surface
  the to-do/milestone signal ✅ **shipped (PR #47)** — the predicted **relay⟂CLI vertical slice** held
  (local `collectors/tasks.snapshot` + `report.serialize_blob` blob field + `relay/store` new
  `relay_project_checklists` **table** [project-level live, not a per-report column → no DB migration] +
  `relay/server` validation/upsert + `relay/render` badge+block); Inc 2.5 near-real-time checklist push
  ✅ **shipped (PR #49)** — same relay⟂CLI seam (a `POST /checklist` endpoint + `delivery/relay`
  client + a `cli` `checklist-push`/`--watch` poll command + a project-page render), all additive on
  Inc 2's store/helpers; Inc 3 forward-looking layer **≡ E1 ≡
  B5** (forward-state). E3 enriched chat bots
  (`bot/`); E4 cross-project coordination (broad); **E5 read-write dashboard — the watershed: first step
  SHIPPED** as the **supervisor-interaction loop** (E2 Inc 5, PR #74 merged) — the two-way discussion
  across `relay/store`+`relay/server`+`relay/api` (the `/api/discussions` cookie + Bearer routes), `state`
  (`discussion_watermark`) + `delivery/relay` + `cli` (`orion discussions`), and `web/` (the SPA panel).
  Remaining E5: **comments→discussion consolidation Stage 2 (KI-28)** — `relay/store` (+nullable
  `report_id`) + `relay/server`/`relay/api` (retire comment routes) + a one-time **`report_comments`
  migration** (the live-data-coupled unit) + `cli`/`bot` (retire `orion comments` / `/api/comments`) +
  `web/` (report page → discussion components); then full read-write + hosting-as-primary stay aspirational.
- **Independent KIs (small):** KI-5 compose unknown-channel raise (`compose.py`, keep in sync with
  `cli._sender_for`); KI-8 vestigial state cleanup (`state.py` + `report.py` migration). (KI-19 dashboard
  CSP ✅ resolved 2026-06-24 — hash-based CSP + headers.) KI-4 is an eval experiment (not code).
  KI-6/7/10/11/14/16 are by-design "build only on demand."
- **B5 scheduling layer / KI-13:** deferred → converges with E1 (both need Orion's own forward state).

## The two structural facts that drive everything

1. **The cleanest seam: `relay/` (hosted dashboard) ⟂ `src/orion/` (local CLI).** Separate top-level
   packages communicating *only* via the portable blob over HTTP (`report.serialize_blob` → relay
   ingest). Dashboard work (E2, KI-19, KI-20) shares ~zero files with local-CLI work. Highest-confidence
   parallel split.
2. **The contention spine: `cli.py` + `config.py`.** Almost every *local* feature adds lines here, so
   two agents editing them collide even in separate worktrees. Local features that each touch the spine
   must be serialized, or an orchestrator owns the spine while workers produce isolated modules.
3. **Inc 4: the SPA ⟂ relay-JSON-API seam — REALIZED in 4a.** The dashboard rebuild splits the hosted half
   into a **React/Vite frontend** (top-level `web/`, not `frontend/`) and the **relay as a read-only JSON
   API**. The **JSON API contract was the seam** (`docs/dashboard-api-contract.md` + `relay/api.py`), fixed
   first in 4a.0; frontend screens and backend serializers then fanned out against the agreed shapes — the
   split held in practice. The frontend was serial only at its start (shell + theming + routing before
   screens), then screen-parallel (Projects/Project/Report). Single-host won over two-host: the relay
   serves the built SPA (`--web-dir`), so there is no Cloudflare/Fly coordination edge. The two new backend
   bands (Disciplines collector, the **Skills comb** collector+merge — 4c, reframed from the planned
   Connections derivation) and the Scheduling aggregation stayed mutually file-disjoint; only their
   *sections* depended on the (now-built) shell. **Inc 4 is COMPLETE** — the seam analysis held across all
   three new signals (each: a producer collector → push → store → a cross-project serializer → a section,
   fanned out against the agreed JSON).

## Dependency graph (cannot parallelize across these edges)

- **C3 → {E4, E5, KI-17 author identity}** — multi-party identity is the prerequisite seam.
- **C2c → C2d** — reply-targeting needs a bot to exist.
- **E1 ≡ B5** — both need "Orion's own forward state"; build together, not as two parallel tracks.
- **E2 → D4 (done) + cross-project data**; **E5 → C3** (write paths + auth).
- **E2 Inc 1 (portfolio overview) was relay-only** (Tier-1, the clean seam — shipped). **E2 Inc 2
  (checklist signal) is NOT** — it is a vertical slice spanning local `collectors/` → blob → `relay/`
  store+render, so it serializes against the `report.serialize_blob` contract and the relay schema; build
  it as one coordinated slice, not a relay-only change. **E2 Inc 3 ≡ E1 ≡ B5** (Tier 3, forward-state).
- **E2 Inc 4 (sectioned dashboard rebuild): API contract → then frontend ∥ backend.** The JSON API
  contract gates everything; once fixed, the SPA and the backend (JSON serializers + new derivations) run
  in parallel. Within it: **4a** data-ready sections need only re-serialization (low coupling); **4b**
  Disciplines and **4c** the **Skills comb** (reframed from Connections) each need a new backend band before
  their section — gate the section on the band, not on each other. **E4-developer-view = Inc 4c** shipped as
  the Skills comb (a literal cross-project relationship graph was deferred — projects too independent);
  **E4-multi-party stays C3-gated.** **Inc 4 COMPLETE.**
- **4c Skills-comb rework: Track A (sourcing) → then Track B (visual) ∥-ready on a stable wire shape.** The
  rework splits cleanly along the **`GET /api/skills` wire shape**, which is held stable. **Track A
  (producer/sourcing, CP1 — SHIPPED):** the global two-pass `skills-sync` + the atomic `POST /skills-batch`
  write + depth re-tune — a vertical slice spanning `extract.py` → `collectors/skills.py` → `cli.py` →
  `relay/{store,server,api}.py`, built as one coordinated unit (the producer and the batch store/endpoint are
  intertwined). **Track B (SPA comb visual, CP2 — SHIPPED):** `web/src/routes/Skills.tsx` +
  `lib/skillsComb.ts` + `base.css`, depended ONLY on the (unchanged) wire shape, so it stayed independent of
  Track A — sequenced after A so its tooth-length scale calibrated on the real re-tuned depth distribution,
  not placeholder data (it did: eyes-on-verified on real seeded data, 3 themes). The one genuine coupling
  (scope-safety of a global extraction) was resolved **structurally** in A: pass 2 is per-project and blind,
  so nothing leaks regardless of the visual. **4c rework COMPLETE** (the A→B-on-a-stable-wire split held).

## Three tiers of parallelizability

- **Tier 1 — safe to parallelize now (independent, ~no file overlap):** the relay-track vs CLI-track
  split; the three D follow-ons (mutually file-disjoint — only `graduate-idea` touches `cli.py`);
  independent KI fixes (KI-5, KI-8). (E2 Inc 1 portfolio overview was a clean Tier-1 relay-only slice —
  shipped.)
- **Tier 2 — parallel *within* a phase (intra-phase fan-out):** module + its unit tests by one agent
  while another drafts docs (the cli/config wiring is the serial tail); multi-file sweeps by file;
  read-only fan-out (exploration, multi-dimension review, adversarial verification) is always safe.
  (E2 Inc 2.6 illustrates the *serial-seam* limit: its tracker collector [Unit A] and the `add-project`
  tasks_file bootstrap [Unit B] both depend on the shared `collectors/_markdown.py`, so A1 had to land
  first — the shared parser is a serial point, with the per-unit tests the parallelizable tail.)
- **Tier 3 — NOT parallelizable:** anything gated on C3 (E4, E5, author identity); C2d before the bots;
  E1 split from B5; multiple local features rewriting the `cli.py`/`config.py` spine at once.

## Mechanism (Claude Code only)

- **Single session, orchestrator + worktree-isolated sub-agents** (the Agent tool's
  `isolation: "worktree"`): best for Tier-1 independent modules and Tier-2 fan-out; results merge back.
- **Multiple Claude Code sessions on separate branches/worktrees:** for two large independent tracks
  in parallel terminals (e.g. a dashboard track [E2/KI-19/KI-20] and a local-CLI track), each its own
  PR, merge-coordinated by the human.
- Either way: **isolate writes per worktree; keep one owner for the `cli.py`/`config.py` spine.**

## Discipline caveat

The project favors *sequential, reviewable* work (`CLAUDE.md`: build phase-by-phase, stop at each
boundary; smallest reviewable unit). So parallelism's best, discipline-consistent uses are **(a)
read-only fan-out**, **(b) intra-phase fan-out across disjoint files**, and **(c) genuinely independent
small items** (the relay-vs-CLI split; independent KI fixes). Using it to build several *coupled feature
phases* at once would bypass phase-boundary review and collide on the spine — so treat parallelism as a
force-multiplier *inside* a slice and across independent small items, not as a way to run the feature
roadmap concurrently.

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

_Last synced: 2026-06-24 (after Horizons A–D shipped; D follow-ons + Horizon E + deferred C-track
ahead). Excludes Horizon P, which is decision-gated launch work._

## Remaining buildable inventory + file footprint

- **Horizon D follow-ons:** `graduate-idea` (`cli.py` / `scaffold.py` / reads `collectors/incubator.py`);
  KI-20 relay-dashboard timezone (`relay/`); summarizer-prompt style check (`summarize.py`).
- **Deferred C-track:** C2c native Discord bot (`bot/` + `config` platform tuple + `cli` dispatch);
  C2d reply-targeting (`bot/`; uses existing `relay/server` `report_id`); C3 multi-party identity
  (broad: `config` participant graph, `report.py` author field / KI-17, `relay/` auth+identity, `state`).
- **Horizon E:** E1 light planning layer (`state` new tables, `config`, a planning module, `cli`,
  `relay/render`); E2 dashboard meta-layer (`relay/` render/store/server; builds on D4 + cross-project);
  E3 enriched chat bots (`bot/`); E4 cross-project coordination (broad); E5 read-write dashboard
  (`relay/` write paths + auth — the watershed).
- **Independent KIs (small):** KI-5 compose unknown-channel raise (`compose.py`, keep in sync with
  `cli._sender_for`); KI-8 vestigial state cleanup (`state.py` + `report.py` migration); KI-19 dashboard
  CSP/nonce (`relay/render.py`). KI-4 is an eval experiment (not code). KI-6/7/10/11/14/16 are
  by-design "build only on demand."
- **B5 scheduling layer / KI-13:** deferred → converges with E1 (both need Orion's own forward state).

## The two structural facts that drive everything

1. **The cleanest seam: `relay/` (hosted dashboard) ⟂ `src/orion/` (local CLI).** Separate top-level
   packages communicating *only* via the portable blob over HTTP (`report.serialize_blob` → relay
   ingest). Dashboard work (E2, KI-19, KI-20) shares ~zero files with local-CLI work. Highest-confidence
   parallel split.
2. **The contention spine: `cli.py` + `config.py`.** Almost every *local* feature adds lines here, so
   two agents editing them collide even in separate worktrees. Local features that each touch the spine
   must be serialized, or an orchestrator owns the spine while workers produce isolated modules.

## Dependency graph (cannot parallelize across these edges)

- **C3 → {E4, E5, KI-17 author identity}** — multi-party identity is the prerequisite seam.
- **C2c → C2d** — reply-targeting needs a bot to exist.
- **E1 ≡ B5** — both need "Orion's own forward state"; build together, not as two parallel tracks.
- **E2 → D4 (done) + cross-project data**; **E5 → C3** (write paths + auth).

## Three tiers of parallelizability

- **Tier 1 — safe to parallelize now (independent, ~no file overlap):** the relay-track vs CLI-track
  split; the three D follow-ons (mutually file-disjoint — only `graduate-idea` touches `cli.py`);
  independent KI fixes (KI-5, KI-8, KI-19, KI-20).
- **Tier 2 — parallel *within* a phase (intra-phase fan-out):** module + its unit tests by one agent
  while another drafts docs (the cli/config wiring is the serial tail); multi-file sweeps by file;
  read-only fan-out (exploration, multi-dimension review, adversarial verification) is always safe.
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

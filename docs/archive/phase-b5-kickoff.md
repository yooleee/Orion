# Phase B5 Kickoff — Scheduling *layer* (conditional; only if OS-delegation is outgrown)

> **Outcome (2026-06-17): gate settled — B5 DEFERRED, folded into Horizon C; the carried-over
> B4 live verification COMPLETED.** The build-or-defer gate below was evaluated with Yousuf and
> the decision is to **not build B5 now** (no concrete mixed-cadence need; the real need likely
> arrives with the Horizon-C listener that would host an in-process scheduler). See the **Phase
> B5 status** section in [`plans/orion-plan.md`](../../plans/orion-plan.md) and **KI-13** in
> [`known-issues.md`](../known-issues.md). The B4 live/manual verification (next section) was run
> end to end and passed — default Anthropic path, a local Ollama backend (`qwen2.5:0.5b`), and
> local fail-closed. The rest of this doc is preserved as the record of the gate analysis.

> **Read this, then [`plans/orion-plan.md`](../../plans/orion-plan.md) in full, before doing
> anything** — especially the **roadmap B5 row**, the **Phase 4 status** block, **KI-13**, and
> **"When a built-in scheduling *layer* becomes right"** in the Future-direction section. Like B3
> and B4, B5's design is **NOT pre-settled** — and unlike them, **B5 is conditional**: the first
> question is not *how* to build it but *whether* to build it at all this phase. Do a full
> **plan-mode pass**: settle the build-or-defer gate first, then (only if building) surface the
> open decisions with a recommendation for each, settle them with Yousuf, and build checkpoint by
> checkpoint, stopping at each boundary for review.

## ⚠ Before you start: carried-over B4 live/manual verification

B4 was **signed off (2026-06-17)** on the strength of the 172-test suite, but its **optional
live/manual verification was deliberately deferred to this session** (it needs an API key / a
running local server and sends to real channels). Do this **first**, before any B5 work:

1. **Default path unchanged** — run a real Anthropic-backed `report <project>` (a project with
   git activity, no `[summarizer]` table) and confirm the preview renders a summary and delivery
   works end to end exactly as before B4.
2. **Local backend works** — add a `[summarizer]` table with `provider = "local"` pointing at a
   running OpenAI-compatible endpoint (e.g. Ollama at `http://localhost:11434/v1`, `model =
   "qwen2.5:0.5b"` — a small model is plenty; this checks wiring, not quality), then run a real
   `report` and confirm the preview shows a local-model summary.
3. **Local backend fails closed** — with the local server **stopped**, confirm the run surfaces a
   clean `SummarizerError`, sends nothing, and does **not** advance state (a re-run re-reports the
   same delta).

Record the outcome in the **Phase B4 status** block (and a CHANGELOG note if anything is worth
flagging). If anything misbehaves, treat it as a **B4 follow-up fix before B5**, not a B5 task.

## Where things stand (as of 2026-06-17)

- **Horizon A** (A1–A4) shipped & signed off. **Horizon B:** B1, B2, B3, B4, and B6 are signed
  off. `pytest`: **172/172**. **B5 is the only remaining Horizon-B phase** — and it is
  **conditional** (it may be deferred into Horizon C; see the gate below).
- Doc map: roadmap + design → [`plans/orion-plan.md`](../../plans/orion-plan.md); shipped →
  [`CHANGELOG.md`](../../CHANGELOG.md); open concerns → [`known-issues.md`](../known-issues.md);
  the Phase-4 OS-scheduling runbook → [`scheduling.md`](../scheduling.md).

## What B5 is (and is not)

A **scheduling *layer* inside Orion** — the point where deciding *what* to send and *when it is
due* needs **Orion's own state**, not just an OS timer. Candidate scope (KI-13 and the roadmap):
**activity-gating** ("only send if something changed"), a cadence-aware **`report --all --due`**
filter (one scheduler entry serves mixed per-project cadences), **quiet hours**, **per-recipient
cadence**, and a unified **next-run / last-error view**.

It is **NOT** a scheduler. Phase 4 already settled that Orion ships **no timer of its own** and
delegates the wake-up to the OS (cron / launchd / Task Scheduler) — that stance is unchanged. B5,
if built, is the **hybrid**: the OS still provides the wake-up; Orion owns the *decision*. Because
`orion report` is already a clean non-interactive entry point, this is **additive — no rewrite**.

## The gate to settle FIRST (the heart of B5): build, trim, or defer?

B5 is marked **⏳ Conditional** for a reason. The plan's test is explicit: **does cadence need
Orion's own state yet?** Settle this with Yousuf before any design:

- **While cadence = "run a command at time T," the OS tool wins** — and mixed cadences are already
  expressible with multiple scheduler entries (one per cadence group). If that is still adequate,
  **B5 should be deferred**, and this is a clean place to pause Horizon B.
- **B5 becomes right** once there is a real need for activity-gating, backoff, quiet hours,
  per-recipient cadence, or a unified status view — logic that *must* live in Orion.
- **Sequencing caveat (important):** the plan notes that the **bidirectional listener** (Horizon
  C) and the "cadence needs state" moment **likely arrive together** — once an always-on process
  exists to host a listener, an in-process scheduler is nearly free. So a live option is to
  **fold B5 into Horizon C** rather than build it standalone now. Weigh this explicitly.

**Recommended framing:** treat this as build-the-*smallest*-piece-with-a-real-need, or defer.
Don't build the full activity-gating + quiet-hours + per-recipient-cadence + status surface on
spec — that is "building the future." If one concrete need exists (most likely `--due` so a single
scheduler entry serves mixed cadences, per KI-13), scope B5 to just that and keep the rest as
clean seams.

## Open decisions to settle in plan mode (only if the gate says "build")

Surface each with a recommendation; settle with Yousuf before coding:

1. **Scope minimization.** Which of {`--due` filter, activity-gating, quiet hours, per-recipient
   cadence, status view} actually ships in B5 vs stays a deferred seam? Bias: the **minimum** with
   a demonstrated need.
2. **Schedule config surface.** Where does per-project cadence live — a per-project `schedule`
   field (interval? named cadence `daily`/`weekly`? a cron-ish string)? Validate like
   `share_level` / `auto_send`. Decide the *expressiveness* deliberately (a named cadence is
   simplest; a cron string is powerful but is re-implementing the OS layer Orion delegates to).
3. **State for "due".** "Due" = schedule + last **successful** report time. The state store
   already records report history with timestamps (`record_report`, `generated_at`) — reuse that,
   or add an explicit per-project `last_reported_at` / `next_due` table? Prefer reuse if it
   suffices.
4. **`--due` semantics & edge cases.** How is "due now" computed? Does a **no-activity** run count
   as "reported" for cadence purposes (resets the clock) or not? How does `--due` compose with the
   existing `--all` and the `--yes` + `auto_send` gate (which must stay intact)?
5. **Activity-gating vs cadence.** Are they independent switches — "send on schedule even as a
   heartbeat" vs "only send if something changed"? Today every run is already a no-op when there's
   no delta; clarify what activity-gating *adds* beyond that.
6. **Quiet hours / backoff / per-recipient cadence.** In scope now or deferred? (Strong bias:
   defer unless a concrete need exists — these are the most speculative.)
7. **Status view.** A unified `orion status` (next-run / last-error / last-sent per project) — in
   B5, or its own later ergonomics phase? Note it would partly need data the OS scheduler owns.

## Seams / files likely involved (confirm in plan mode)

- `src/orion/config.py` — a per-project `schedule` field + validation (mirror the `share_level` /
  `auto_send` constants and checks).
- `src/orion/state.py` — last-successful-report time per project for "due" gating. Check whether
  the existing history table already provides this before adding schema.
- `src/orion/cli.py` — a `report --all --due` filter layered on the existing `--all` loop (the
  `_run_report` per-project pipeline stays put); possibly a `status` command.
- `docs/scheduling.md` — update from "pure OS delegation" to the **hybrid** model (OS wakes Orion;
  Orion decides what's due); keep the per-OS setup and the WSL2 caveat.
- Tests + living docs — `test_config.py` (schedule validation), `test_schedule.py` (the `--due`
  gating + edge cases), `docs/testing.md`, `README`, `CHANGELOG`, `plans/orion-plan.md` (flip the
  B5 row), and **resolve/​update KI-13**.

## Security / safety must-holds (non-negotiable, unchanged)

- The **`--yes` + `auto_send`** preview-bypass gate is untouched: a scheduled, due, unattended run
  still sends only projects opted in with both signals. `--due` filters *which* projects run; it
  must never relax *whether* a project may send unattended.
- Redaction (both passes) and preview-before-send are unchanged. B5 changes *timing/selection*,
  not the safety pipeline.
- Local-first is unchanged: B5 adds **no** always-on process, no daemon, no inbound surface (that
  is Horizon C). The OS still provides the wake-up.

## How to work this phase (project rules)

- **Plan before code:** a real plan-mode pass — settle the build-or-defer **gate** first, then any
  open decisions, with recommendations; only then build.
- **Smallest reviewable unit**; checkpoint after each and wait for review.
- **Keep docs living**; every change made cross-platform-minded (Windows / macOS / Linux).
- **Sign-off pattern:** implement (mark "awaiting sign-off"), then a separate "Sign off Phase B5"
  commit flips the markers. Commit/push only when Yousuf asks.

## First commands to run next session

```bash
# 1. Confirm the baseline is still green (expect 172 passing).
.venv/bin/python -m pytest -q     # or: uv run --no-sync python -m pytest -q

# 2. Then run the carried-over B4 live/manual verification (section above) BEFORE any B5 work.
```

Then read [`plans/orion-plan.md`](../../plans/orion-plan.md) — the B5 row, the Phase 4 status block,
KI-13, and "When a built-in scheduling *layer* becomes right" — and start the **plan-mode pass**
with the build-or-defer gate.

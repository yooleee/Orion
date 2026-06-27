# Orion — Roadmap Checklist

A reframing of the project roadmap (`plans/orion-plan.md`, the canonical horizons & phases
table) into a live checklist. Observed from the plan, not authored here: each item mirrors a
roadmap phase and its shipped/open state. Update the boxes as the plan's statuses change.

Status legend: `[x]` shipped / signed off · `[ ]` planned or in progress.

## Horizon A — Local single-user reporting core

- [x] A1 — `report`: git → redact → conditional Haiku summary → preview → Discord
- [x] A2 — Structured lane: intake, to-dos, notes (no-LLM passthrough)
- [x] A3 — Slack delivery + recipient routing
- [x] A3.5 — Cross-platform portability pass (audit, fixes, scheduling stance)
- [x] A4 — Scheduled digests: unattended `report --all --yes`

## Horizon B — Local automation, ingestion & polish

- [x] B1 — Event-driven triggers: git `post-commit` / `pre-push` hook delegating to `report`
- [x] B2 — Claude Code session skill: summarize a coding session and push it via `intake`
- [x] B3 — Richer rendering: Slack Block Kit + Discord embeds
- [x] B4 — Summarizer flexibility: provider-agnostic seam + optional local model
- [x] B6 — CLI ergonomics: read-only config-inspect commands (`projects`/`show`/`check`)
- [ ] B5 — Scheduling layer (activity-gating, quiet hours, per-recipient cadence) — deferred into Horizon E1

## Horizon C — Two-way & hosted

- [x] C1 — Web dashboard (read) + hosted/hybrid relay on Fly.io
- [x] C2a — Dashboard comments + `orion comments` pull
- [x] C2b — Native Slack bot (Socket Mode) → comment store
- [x] C3 (Increment 1) — Multi-party identity, per-user login keys, roles, per-project scope, sessions
- [ ] C2c — Native Discord bot (Gateway) — deferred, demand-gated
- [ ] C2d — Reply-targeting (thread a reply to a specific report) — deferred, demand-gated
- [ ] C3 (later increments) — Subscriptions / routing, per-recipient state, report-submitter authZ

## Horizon D — OSS-readiness & local enhancements

- [x] D1 — `orion add-project`: explicit, append-only config writer + onboarding
- [x] D2 — `orion status`: unreported-across-projects backlog/digest
- [x] D3 — OSS-readiness polish: honest README positioning, ≤10-min setup test
- [x] D4 — Incubator-as-fifth-signal: `collectors/incubator.py` idea-pipeline updates
- [x] D5 — Lightweight audience-typed routing: per-recipient `signals` filter + per-audience compose

## Horizon E2 Inc 4 — Sectioned dashboard rebuild (richer-client SPA)

- [x] Relay → read-only JSON API (me / portfolio / project / report) + JSON auth + `kind` flag
- [x] SPA shell: Vite/React/TS, three-theme `data-theme` tokens, routing + login
- [x] Projects home (rows + tracker card + scope banner + empty state)
- [x] Project page + Report detail
- [x] Single-host serving: relay serves built SPA (`--web-dir`, CSP, path-traversal guard)
- [x] Tracker page (first-class producer `status` field, closing gap-8)
- [x] Scheduling section (`GET /api/scheduling` cross-project deadline buckets)
- [x] First production deployment (`project-orion.fly.dev`)
- [x] Comment writes (`POST /api/reports/:id/comments`, cookie-authed)
- [x] Public Showcase guest view (`GET /api/showcase`, opt-in + allowlist)
- [x] Mobile responsive pass (sidebar → bottom tab bar, rails → stacked cards)
- [ ] Retire `render.py` at parity (remove server-rendered HTML, keep store/derive/api/auth)
- [ ] 4b — Disciplines & directions (doc-centric collector → principles section)
- [ ] 4c — Cross-project Connections (cross-project relationship derivation → graph section)

## Horizon E — Coordination & visibility hub (forward)

- [x] E1 — Forward-looking layer: derived milestones, due-dates, at-risk, slippage
- [ ] E3 — Enriched Slack/Discord bots (threads, slash commands, routing) — parked, long-range
- [ ] E4 — Multi-party cross-project coordination — long-range, C3-gated
- [ ] E5 — Read-only → read-write dashboard inflection — aspirational

## Horizon P — Publish / OSS launch

- [ ] Decision-gated public/OSS launch (sequenced after the dashboard track matures)

# Project Orion — Progress Tracker & Reporter (Design Plan)

> **Status: Horizon A (the local single-user reporting core) is complete and signed off
> (2026-06-15)** — `report` over git + structured signals + `intake`, two-pass redaction,
> preview-before-send, dual-channel (Discord **and** Slack) delivery with routing,
> cross-platform support, and safe **unattended scheduled digests** (`report --all --yes`).
> **Horizon B (local automation, ingestion & polish) is complete — B1 (git-hook triggers),
> B2 (the Claude Code session skill), B3 (richer rendering), B4 (summarizer flexibility), and
> B6 (read-only config-inspect commands) are all signed off; B5 (a scheduling *layer*) was
> evaluated at its gate and deliberately deferred into Horizon C (2026-06-17).** All four
> ingestion signals (git, tasks, notes, sessions) now feed Orion. This is task #7 on the non-application to-do ("Build progress tracker
> (Project Orion)").
>
> The **Roadmap** below is organized into **horizons** (A shipped · B next · C the
> multi-party/hosted pivot, kept coarse), plus a **decision-gated Horizon P (Publish / OSS-launch)**
> that is sequenced by the go-public decision rather than by dependency. This file looks **forward**
> (design + phase plan).
> For what actually shipped, see `[CHANGELOG.md](../CHANGELOG.md)`; for open cross-phase
> concerns, see `[docs/known-issues.md](../docs/known-issues.md)`; for which remaining work is
> parallelizable vs. intertwined (the living coupling map), see
> `[docs/parallelization.md](../docs/parallelization.md)`.
>
> **Strategy overlay:** the *why/what-success* layer above this roadmap — Objective, Goals,
> differentiators, and deferred long-range directions — lives in
> `[docs/orion-strategy.md](../docs/orion-strategy.md)` (this roadmap is its "Actions"). Settled in
> the post-C1 direction-setting pass (2026-06-18); see "Horizon-C direction settled" below.

## Roadmap (horizons & phases)

> **⚑ This table is the canonical map — keep it current.** Re-sync it (statuses, new rows for
> shipped/decided work, coarse forward bands) **as part of finishing each slice**; the dated prose
> sections below are the *detail record*, **not** a substitute for updating this table. Letting the
> map lag reality has coincided with slower progress — a current map keeps the next step unambiguous.
> (The `CLAUDE.md` living-doc rule, applied to this table specifically.)

> **Numbering.** Phases are grouped into **horizons** (A, B, C, … and future D, …); numbering
> is **horizon-scoped and restarts each horizon**, so a phase number never grows into unwieldy
> double digits as the project runs on. **Horizon A keeps the original phase numbers unchanged,
> just prefixed** (A1 = the former "Phase 1", … A3.5 = "Phase 3.5", A4 = "Phase 4"), so every
> existing reference in `[CHANGELOG.md](../CHANGELOG.md)`, commits, and the kickoff docs still
> maps by inspection. From Horizon B onward, numbering is fresh (B1, B2, …). The detailed
> per-phase "status" sections further down keep their legacy "Phase N" headings (N == A-N) as
> the historical shipping record.

**Horizon A — Local single-user reporting core** *(shipped ✅)*


| Phase          | Scope                                                                  | Status                    |
| -------------- | ---------------------------------------------------------------------- | ------------------------- |
| A1 (was 1)     | `report`: git → redact → conditional Haiku summary → preview → Discord | ✅ Signed off (2026-06-15) |
| A2 (was 2)     | Structured lane: intake, to-dos, notes (no-LLM passthrough)            | ✅ Signed off (2026-06-15) |
| A3 (was 3)     | Slack delivery + recipient routing                                     | ✅ Signed off (2026-06-15) |
| A3.5 (was 3.5) | Cross-platform portability pass (audit + fixes + scheduling stance)    | ✅ Signed off (2026-06-15) |
| A4 (was 4)     | Scheduled digests — unattended `report --all --yes`                    | ✅ Signed off (2026-06-15) |


**Horizon B — Local automation, ingestion & polish** *(next; local-first preserved)*


| Phase | Scope                                                                                                                                                                                                                                   | Status                    |
| ----- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------- |
| B1    | Event-driven triggers — git `post-commit` (and/or `pre-push`) hook delegating to `report` (fire-on-commit); opt-in, cross-platform. *(Note: git has no client-side `post-push` hook — `post-commit`/`pre-push` are the local options.)* | ✅ Signed off (2026-06-16) |
| B2    | Claude Code session skill — summarize a coding session and push it via `intake` (the session signal)                                                                                                                                    | ✅ Signed off (2026-06-16) |
| B3    | Richer rendering — Slack Block Kit + Discord embeds, done together (KI-9); likely a small `ReportBlob`/`compose` change to carry structured sections                                                                                | ✅ Signed off (2026-06-17) |
| B4    | Summarizer flexibility — provider-agnostic summarizer seam + optional local model (OpenAI-compatible). Per-step model choice **deferred** (one LLM step today; seam keeps it additive). Keeps the "lightest adequate model" default (Haiku) | ✅ Signed off (2026-06-17) |
| B5    | Scheduling *layer* — activity-gating, `report --all --due`, quiet hours, per-recipient cadence (KI-13). Built **only if** OS-delegation is outgrown; sits at the B→C boundary. **Gate evaluated 2026-06-17 → defer.**                       | ⏭️ Deferred → Horizon E1   |
| B6    | CLI ergonomics — **read-only** config-inspect commands (`projects`/`show`/`check`) for visibility/discoverability. Orion still never *writes* config (hand-edited TOML stays the way to change it). Closes KI-15                 | ✅ Signed off (2026-06-16) |


**Horizon C — Two-way & hosted** *(C1–C2 shipped; C3 multi-party — Increment 1 shipped + deployed)*

These converge into one horizon: bidirectional interaction (supervisors acting back) forces an
always-on **listener**, which is what tips local-first → **hosted/hybrid**, which is where
**multi-party** data must meet. So they are dependency-ordered, not finely pre-phased:


| Phase | Scope                                                                                                                                                                                          | Status            |
| ----- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------- |
| C1    | Web dashboard (read) + hosted/hybrid relay — collection stays local; delivery/presentation move hosted along the portable report/intake blob seam                                              | ✅ Deployed (2026-06-19) — Fly.io (Path B), HTTPS, verified end to end. (The dashboard's HTTP-Basic read auth was **superseded by C3's per-user cookie login**, 2026-06-25.) Hosting settled: Path B (managed/Cloudflare + E2E deferred). Detail: "Phase C1 status" + "Hosting decision" below. |
| C2    | Bidirectional replies — supervisors comment back (dashboard first; native Discord/Slack threads as a richer add-on); brings inbound validation + authorization                                 | ✅ Built (2026-06-19) — two slices, C2a + C2b below (full inbound checklist; all stdlib + optional `slack-bolt`). Detail: "C2-bots status" below + [`docs/phase-c2-kickoff.md`](../docs/archive/phase-c2-kickoff.md), [`docs/slack-bot.md`](../docs/slack-bot.md). |
| C2a | Dashboard comments + `orion comments` pull (Bearer GET, local watermark) | ✅ Built (2026-06-19) |
| C2b | Native Slack bot (Socket Mode) → `POST /api/comments` → existing comment store | ✅ Built (2026-06-19) |
| C2c | Native **Discord** bot (Gateway) — first steps of the chat-surface track (Horizon E) | 🔭 Deferred (demand-gated) |
| C2d | Reply-targeting — bot sends the optional `report_id` (thread a reply to a specific report) | 🔭 Deferred (demand-gated) |
| C3    | Multi-party: identity, subscriptions & authorization — a participant graph (not an implicit "me"), per-supervisor per-project/task/todo subscriptions (the routing future), and access control | ✅ **Increment 1 SHIPPED + DEPLOYED (2026-06-25):** merged to `main` (PRs #39, #43, #41, #42; #43 replaced #40) and live at `orion-relay-horizon-c.fly.dev`. Dashboard-integrated identity + authZ — per-user login keys, roles (admin/viewer), per-project read scope, signed cookie sessions (login/logout + stateless revocation), a `relay-user` provisioning CLI over a relay admin API, and authenticated comment authorship. Subscriptions/routing, per-recipient state, E2E, and KI-17's report-submitter half remain ahead (the comment half shipped). Detail: "C3 status (Increment 1)" below. |


**C3 status (Increment 1 — multi-party access, shipped + deployed 2026-06-25).** A deliberate decision to
bring multi-party identity/access into the dashboard now, integrated from the start, rather than bolted on
later. The driver is real near-term dogfooding: share a project's state with a helper or supervisor, control
who sees which project, and a guest/showcase view for the portfolio. This moved C3 from "deferred / demand-
gated" to **built in small reviewed increments** — Increment 1 is now shipped + deployed. Settled with the user: auth is **per-user keys
+ scopes** (server-minted high-entropy keys, no passwords, no OAuth, stdlib-only). A Codex `/second-opinion`
hardened the design (independent secrets for session signing / key pepper / admin token, never derived from
the view or ingest tokens; the signed cookie carries only id + version + expiry with role and scope re-read
from the DB each request; stateless revocation via `active` + `session_version`; a peppered key verifier; a
gated, deprecated legacy view-key admin; canonical-Origin CSRF). Scoping calls: out-of-scope resources return
**404** (hide existence, since the audience may include guests); CSRF is Origin + `SameSite` now with a signed
token deferred; revocation ships in Increment 1.

What Increment 1 delivers (two stacked PRs): a per-user store (`relay_users` / `relay_user_projects` /
`relay_admin_audit`), signed session cookies + a peppered key verifier, `/login` and `/logout` with DB-driven
authentication and stateless revocation, a gated legacy bootstrap admin, per-project authZ on every route
(admin sees all, a viewer is scoped, out-of-scope is 404), a relay admin API (`POST`/`GET /api/users` +
revoke, admin-token gated), and the `relay-user add` / `list` / `revoke` CLI. Three new independent env
secrets join the existing `ORION_RELAY_VIEW_TOKEN` (now the bootstrap-admin login key, not HTTP Basic):
`ORION_RELAY_SESSION_KEY`, `ORION_RELAY_USER_PEPPER`, `ORION_RELAY_ADMIN_TOKEN`. The increment ladder ahead:
2 = contribute/write access, 3 = guest/demo mode, then subscriptions/routing. The C2 dashboard's HTTP-Basic
read auth is superseded by the cookie login. **Follow-on hardening slice shipped (2026-06-24):** a hash-based
Content-Security-Policy + standard security headers on every dashboard response (relay-local, no behavior
change), resolving **KI-19**.


**Framing.** Orion is **personal infrastructure + a portfolio piece** (open-source is now *secondary*
to the developer's own goals — see the simplicity reframe below). Horizon **D** is complete (D1–D5
shipped); the current near-term work is Horizon **E**'s **dashboard-visibility track**. E2 Inc 1–3
have **shipped and deployed** (portfolio overview → live checklist → near-real-time push → status-aware
tracker → the forward-looking layer ≡ E1, all observe + remember, never originate). The **active next
work is E2 Inc 4 — the sectioned dashboard rebuild**: recreating the ingested Claude-design (`design/`)
as a **React/Vite single-page app, single-host on the relay** (the relay becomes a read-only JSON API;
the server-rendered HTML retires at parity). The rest of Horizon **E** (chat/E3, read-write/E5) stays a
recorded direction with the seams kept clean — **not built**.

**Two stage-appropriate framings shifted with Inc 4 (2026-06-26):** (1) **Stack** — the dashboard moves
from a stdlib server-rendered surface to a richer client (React/Vite SPA), single-host on the existing
Fly relay (two-host Cloudflare-Pages-plus-Fly was judged not worth the coordination cost; a
Cloudflare-everything consolidation is a possible *future* migration, not now). (2) **Simplicity goal
reframed** — from "stdlib-minimal / clone-and-run-in-ten-minutes" to **"easy and straightforward to set
up across usability levels"** (single dev to multi-party). OSS is secondary to the developer's goals for
now, the hard stdlib/10-min gate relaxes, and "minimize complexity" stays a live discipline. The
**privacy/safety invariants and observe-not-originate are untouched** by both shifts. Forward bands stay
coarse:
sequenced by dependency, detail firms up as each nears. **Horizon P
(Publish / OSS-launch)** sits outside that dependency order: it is **decision-gated** — the discrete
work to make the repo public, consolidated in one band and triggered when the go-public decision is
made, not after any particular horizon.

**Horizon D — OSS-readiness & local enhancements** *(the active near-term band)*

| Phase | Scope | Status |
| ----- | ----- | ------ |
| D1 | `orion add-project` — explicit, append-only config writer; cwd inference; onboarding | ✅ Shipped (2026-06-23) |
| D2 | `orion status` — unreported-across-projects backlog/digest (idea #6; derivable from `report_history`, no new schema) | ✅ Shipped (2026-06-24) |
| D3 | OSS-readiness polish — honest README positioning vs incumbents (Gitmore/Gitrecap/dev-journal), the ≤10-min setup test (G3), fix the absolute-path README assessment pointer, trim dogfood friction | ✅ Shipped (2026-06-24) |
| D4 | Incubator-as-fifth-signal (idea #1) — a new structured-lane collector (`collectors/incubator.py`) emitting idea-pipeline updates (new ideas + status transitions) from an `index.md` table; the first real test of the "modular signals" direction. Configured as a dedicated `[projects.incubator]`, routed to mentors/family via D5 | ✅ Shipped (2026-06-24) |
| D5 | Lightweight audience-typed routing (idea #2) — per-recipient `signals` filter + per-audience compose grouping on today's named-recipient seam, no C3 identity. Each run composes one filtered message per distinct `(channel, signals)` audience; relay still gets the full report | ✅ Shipped (2026-06-24) |

Small follow-ons (no separate phase) — **all ✅ Shipped (2026-06-24)**: `graduate-idea` → calls
`add-project` (idea #4 follow-on); summarizer-prompt clean-prose style (idea #7 — a prompt tune, not
an inheritance mechanism); relay-dashboard timezone configurable via `relay-serve --timezone`
(KI-20 follow-up). The last two were built in parallel via worktree-isolated agents (file-disjoint —
see [docs/parallelization.md](../docs/parallelization.md)).

**Horizon E — Coordination & visibility hub** *(mostly long-range; the **dashboard-visibility track is now validated and being built incrementally** — see E2)*

Surface-plural, **two distinct tracks**: the **dashboard** is the primary structured-visibility
surface; **Slack/Discord** are a parallel interaction surface that leverages chat-native channel
features and carries a build-and-maintain cost. They diverge in architecture and are sequenced
independently (chat = discussion · dashboard = structured overview · Orion = connective tissue + memory).

| Phase | Track | Scope | Status |
| ----- | ----- | ----- | ------ |
| E1 | dashboard | Light planning/tracking layer — derived milestones/sprints/due-dates/at-risk, *reframing not originating*; shares the "needs Orion's own forward state" threshold with the deferred scheduling layer (B5 / KI-13), but kept a **separate track** | ✅ **E2 Inc 3 rung 1 COMPLETE + DEPLOYED 2026-06-26** (Units 0–5 shipped, PR #60: due-dates/at-risk/observed-store/slippage/milestones; observe + remember, never originate; gate settled 2026-06-26). Richer-group follow-ons (milestone slipping count, per-project `due_soon_days`) deferred-additive |
| E2 | dashboard | Dashboard as a richer multi-signal, multi-project visibility/showcase surface (idea #5) — portfolio map + cross-project visibility + the to-do/milestone signal, a forward-looking layer, and a full **sectioned** redesign | 🛠️ **Inc 1–3 SHIPPED + DEPLOYED.** Inc 1 (portfolio overview), Inc 2 (live checklist signal, PR #47), Inc 2.5 (near-real-time push, PR #49), Inc 2.6 (status-aware `tracker` collector, PR #50), a consolidation slice (PRs #52–#54), and **Inc 3 (forward-looking layer ≡ E1, Units 0–5, deployed 2026-06-26)** all done. **Inc 4 — sectioned dashboard rebuild (richer-client SPA) IN PROGRESS.** Recreating the ingested Claude-design (`design/`) as a **React/Vite SPA, single-host on the relay** (relay → read-only JSON API); server-rendered HTML retires at parity (kept working meanwhile). **4a BUILT (in review):** relay → read-only JSON API (`relay/api.py`: me/portfolio/project/report) + JSON auth + explicit `kind` (project/tracker) flag; the `web/` SPA (Vite/React/TS) with the 3-theme `data-theme` token system, shell+routing+login, **Projects home** (rows + tracker card + scope banner + empty), **Project page**, **Report detail**; relay serves the built SPA single-host (`--web-dir` + SPA CSP + path-traversal guard + SPA fallback; Dockerfile multi-stage). **Tracker page SHIPPED** (gap-8/KI-22 closed via a first-class producer `status` field; verified across all three themes). **Scheduling SHIPPED** (`GET /api/scheduling` — pure read-only cross-project deadline bucketing; verified across all three themes). **First production deployment SHIPPED** — SPA cutover to a NEW Fly app `project-orion.fly.dev` (Fly has no in-place rename; DB + secrets migrated, old `orion-relay-horizon-c` destroyed). **Comment writes SHIPPED** (`POST /api/reports/:id/comments` — cookie-authed JSON reusing the form route's CSRF/auth/scope/identity guards; eyes-on incl. XSS-inert). **Still in 4a's band:** Showcase guest view, mobile pass, then retire `render.py` at parity. Then **4b** Disciplines & directions (new collector + section); **4c** Cross-project Connections (new derivation + section). Kickoff: [`docs/e2-inc4-dashboard-rebuild-kickoff.md`](../docs/e2-inc4-dashboard-rebuild-kickoff.md). API contract: [`docs/dashboard-api-contract.md`](../docs/dashboard-api-contract.md). Ladder below. |
| E3 | chat | Enriched Slack/Discord bots — leverage channel features (threads, slash commands, per-channel/topic routing); more ways to drive Orion from chat. A distinct direction (build/maintain bots), continuing C2b/C2c | 🔭 Long-range — **parked** (secondary to the dashboard) |
| E4 | both | Surface-plural coordination across multiple projects / cross-project (the registry already holds many) | 🛠️ The **developer's own** cross-project view arrives as **E2 Inc 4c** (Connections section; data model already `(project, item_key)`-keyed). Multi-party cross-project coordination stays 🔭 Long-range (C3-gated) |
| E5 | dashboard | The **read-only → read-write dashboard** inflection — the architectural watershed (write paths, auth, hosting-as-primary). The point to watch | 🔭 Aspirational |

C3 (multi-party identity) is the prerequisite seam under much of Horizon E. The discipline holds: keep
the **seams** clean (the portable summary+metadata blob, explicitly-named participants, the
provider-agnostic summarizer) so each horizon stays additive rather than a rewrite.

**E2 dashboard-visibility track — validated + incremental ladder (decided 2026-06-25).** The
post-hardening planning juncture surfaced that the dashboard's value isn't just progress reports: the
user wants it as the **one place to see all projects/progress at once, viewable by family** who
comment and give direction — the project's *founding intent*, now homed on the dashboard because
family isn't on Slack/Discord. That is real, present demand (personal use → which is exactly what
makes it a portfolio/OSS asset), so the dashboard-visibility thrust moves from "aspirational" to
**built in small reviewed increments**, the same idiom C3 followed. The ladder:
- **Inc 1 — portfolio overview home (✅ shipped 2026-06-25).** `GET /` is a cross-project card view
  (name → history, latest-report headline, count, relative last-activity), reusing existing relay
  data; relay-local, additive, CSP-safe. Family access uses **existing scoped viewer logins** (no
  guest-view build).
- **Inc 2 — surface the to-do/milestone checklist signal (✅ SHIPPED 2026-06-25, PR #47 merged).**
  A full-pipeline change (collector → blob field → store table → render), since structured
  item data is flattened to prose today. Depth target met: **mirror the current checklist** (done + open
  items from the existing file; stays *reframing, not originating*). **Decisions taken this session
  (override the kickoff's default-leans):** (1) a **project-level LIVE checklist** — the relay holds one
  current checklist row per project (`relay_project_checklists`, upserted on each push), *not* a
  per-report snapshot; (2) a **new per-project `checklist` config toggle** (opt-in, requires the `tasks`
  collector); (3) renders as a **portfolio-card "X/Y done" badge + a report-page "Current checklist"
  block** (CSP-safe, escaped). The full checklist is captured by a new `tasks.snapshot()` that reads the
  same file *independently* of the retrospective `collect()` (so the "newly completed" report behavior is
  untouched) and rides on `full_blob` only (the relay payload), redacted per item. **Known limitation
  (honest):** it updated on the **report push**, not the instant `tasks_file` changes — addressed by
  Inc 2.5 below. Shipped to the live Fly relay 2026-06-25. Kickoff archived at
  [`docs/archive/dashboard-checklist-signal-kickoff.md`](../docs/archive/dashboard-checklist-signal-kickoff.md).
- **Inc 2.5 — near-real-time checklist edit tracking (✅ SHIPPED 2026-06-25, PR #49 merged + deployed).**
  Realizes the dedicated checklist-only-push seam Inc 2 left open, so a `tasks_file` edit
  reaches the dashboard between reports. Adds: a Bearer-authed **`POST /checklist`** relay endpoint
  (reuses the ingest token; upserts via the existing `upsert_checklist`, no report row), a
  `push_checklist` client, and an **`orion checklist-push <project>`** command with a **`--watch`** poll
  loop (stdlib content-compare, ~3s; pushes only on change). Render reach extended: the live checklist
  now also shows on the **project page** (the persistent watch surface), reusing the `_render_checklist`
  helper (no new CSS → CSP unchanged). Redaction holds on the new lane via a shared `_redacted_checklist`.
  Decided: poll, not `watchdog` (stdlib/minimal-dep); event-watching + an `--all` watcher are additive
  later. Eyes-on confirmed the one-shot/watch push and the project-page render against a local relay,
  then shipped to the live Fly relay 2026-06-25.
- **First real workload (now IN USE, 2026-06-26):** Inc 2 + 2.5 shipped the checklist surfaces ahead of
  any configured use. The first real workload — an **applications tracker**
  (`/Users/yoolee/Developer/applications/to_do.md`) — uses rich `- **Status:** …` fields + deadline
  **tables**, not `[ ]`/`[x]` checkboxes, so the shipped `tasks` collector read nothing from it. Rather
  than reformat the file, the user built a richer status-aware collector → **Inc 2.6 below**. That
  collector is now live: the `applications` checklist is pushed to the relay, so the surfaces built in
  Inc 2/2.5 finally carry a real signal.
- **Inc 2.6 — status-aware "tracker" collector + `tasks_file` bootstrapping (✅ SHIPPED 2026-06-26, PR
  #50 merged to `main`; 607-test suite green).** Two reviewable units:
  - **Unit A — `tracker` collector.** Reads the applications tracker's native format (numbered
    `## N.` sections with a `- **Status:**` field, plus the Non-Application / sub-goal **tables**) into
    the existing `{text, done}` checklist surface, so **no relay/dashboard change**. A shared
    `collectors/_markdown.py` (`parse_sections` + `parse_tables`) is the DRY seam. Settled forks: new
    collector (not extend `tasks`); `Submitted`/`Closed` → done, `Not started`/`In progress` → open with
    the status word embedded in the item text; identity by title; **table rows included** (surfaced as
    open items); **deadlines deferred to Inc 3** (status-only v1). `checklist` config now accepts a
    `tasks` *or* `tracker` source (`CHECKLIST_COLLECTORS`); `checklist-push`/`--watch` generalized to
    either file. Verified eyes-on against the real `to_do.md` (15 items parsed).
  - **Unit B — `tasks_file` bootstrapping (B-i only).** `add-project`, when `tasks` is enabled without
    `--tasks-file`, defaults to `<repo>/TODO.md` and **creates a starter checklist** there (preview-gated,
    never overwriting); explicit `--tasks-file` stays config-only (the opt-out). Roadmap-derived seeding
    (`--seed-tasks-from`) was the parse-vs-generate fork — **deferred** to a later slice (chose structured
    parse when it lands; B-i ships the structural "every project can have a checklist" fix first).
    *(→ shipped as Unit 3 of the consolidation slice below.)*
  - **Live wiring DONE:** the `applications` project stanza is in the live `orion.toml` (tracker-only,
    no git; a placeholder recipient with an intentionally-unset env var so this private project can
    never be auto-delivered to the on-hold Alex/Sam chat channels), and its checklist is pushed to the
    production Fly relay (admin-visible on the dashboard). **Still pending (outbound, on hold per the
    user):** **family-as-dashboard-viewers** provisioning (`relay-user add … --role viewer --project
    applications`) — the real "share with family" path, awaiting the family members' names.
  - **Carried-over CSRF comment 403: RESOLVED + deployed (PR #51, separate track).** Root cause was the
    dashboard's own `Referrer-Policy: no-referrer` forcing every browser comment POST to `Origin: null`
    (PR #48's Safari/`public_origin` hypotheses were both wrong); fixed by `Referrer-Policy: same-origin`
    + opaque-Origin Referer fallback, verified live. Kickoff (archived):
    [`docs/archive/applications-tracker-kickoff.md`](../docs/archive/applications-tracker-kickoff.md).
- **Consolidation slice — dashboard-home visibility + `add-project` completeness + KI-8 (✅ SHIPPED
  2026-06-25, PRs #52–#54).** A post-2.6 cleanup, each unit its own PR. **Unit 1** — the dashboard home
  now shows checklist-only projects, so the `applications` card (previously reachable only by direct
  URL) appears in the portfolio; relay-only + deployed. **Units 2+3** — `add-project` gains
  `--tracker-file`/`--incubator-file` (fixes the tracker `KeyError` that forced hand-editing
  `orion.toml`) and `--seed-tasks-from` (the deferred Inc 2.6 Unit B-ii: seed a created `tasks_file`
  from a doc's Markdown tables, parse not LLM). **Unit 4** — KI-8: dropped the vestigial Phase-1
  `project_state` table and the always-`""` `source_marker`. Kickoff:
  [`docs/consolidation-slice-kickoff.md`](../docs/consolidation-slice-kickoff.md).
- **Inc 3 — forward-looking planning layer** (milestones/due-dates/at-risk) — this is **E1**. The
  forward-state gate is **settled (2026-06-26): observe + remember, never originate** — Orion is a
  knowledge base that persists *observed* state (a downstream projection), not authored forward facts.
  **Kickoff + six-unit ladder: [`docs/e2-inc3-kickoff.md`](../docs/e2-inc3-kickoff.md)** (rung 1 =
  due-dates/at-risk/slippage/derived milestones; scheduling/KI-13 stays separate). On-ramp: the
  deadlines the Inc 2.6 tracker already held. **Progress:** Unit 0 (strategy invariant clarified,
  PR #55), Unit 1 (tracker `_parse_deadline` + `ChecklistItem.due_date`, carried on both wire paths;
  local-only), Unit 2 (new pure `relay/derive.py` → overdue/due-soon/at-risk; due dates + a `⚠`/red
  overdue and amber at-risk treatment on the project page, an "N at risk" portfolio badge; relay-side,
  **deployed** to the live relay 2026-06-26) and Unit 3 (the **`relay_observed_items`** append-only
  memory store + a stable `item_key` = the tracker's bare title, carried as `ChecklistItem.key`;
  observations recorded on every push; **KI-21** documents the identity model; deployed 2026-06-26) and
  Unit 4 (slippage from the observation history — `is_slipping`/`slipping_item_keys`: deadline moved
  later, or lingering open past due; a per-item "↘ slipping" marker + an "N slipping" portfolio badge;
  deployed 2026-06-26) and **Unit 5 (derived milestones — the LAST rung-1 unit)** have shipped. Unit 5
  groups the checklist by the tracker's own structure (a new additive `ChecklistItem.group`: apps →
  "Applications", table rows → the table's nearest heading via a new `Table.heading` on the Markdown
  parser), and a pure `relay.derive.milestones()` rolls each group up into `{group, done, total,
  at_risk, nearest_due}` — surfaced as a **"Milestones"** section above the project-page checklist and
  a **"Next: <group> by <date>"** hint on each portfolio card. At-risk roll-up only (a milestone
  slipping count is deferred — the seam is there). Verified eyes-on against the live tracker
  ("Applications — 0/4 done · next due Jun 12, 2026 · 2 at risk"). Grounding: **[`docs/e2-inc3-unit5-kickoff.md`](../docs/e2-inc3-unit5-kickoff.md)**.
  **Rung 1 is COMPLETE and DEPLOYED** (PR #60, deployed to the live relay 2026-06-26): the forward layer
  observes deadlines, remembers them over time, flags at-risk and slipping, and rolls up milestones — all
  observe-not-originate. (The live dashboard reflects milestones after the next tracker push, since the
  stored data predates `group`.) Richer-group follow-ons (milestone slipping count, per-project
  `due_soon_days`) are recorded as deferred-additive, folded in where milestones are re-presented in Inc 4.
- **Inc 4 — sectioned dashboard rebuild (richer-client SPA)** — IN PROGRESS. A full Claude-design
  handoff (committed under [`design/`](../design/): README + 11 screenshots + 3 themes + mobile + `.dc.html`
  prototypes) specifies a sectioned SPA covering every band as a section. **Settled**: recreate it as a
  **React/Vite (TS) SPA, single-host on the relay** (relay → read-only JSON API; reuses the existing
  store/derive + auth/scoping; the server-rendered HTML retires at parity); themes via CSS-variable
  `data-theme`; content stays observed-not-authored and text renders inert (React default binding).
  **4a SHIPPED (in review, sub-checkpoints 4a.0–4a.5):** the JSON API seam ([`docs/dashboard-api-contract.md`](../docs/dashboard-api-contract.md)
  + `relay/api.py` + JSON login/logout + the explicit `kind` project/tracker flag through config→wire→store);
  the `web/` SPA (3-theme tokens, shell, routing, login, Projects home, Project page, Report detail), with the
  composer rendered-but-inert (comment writes deferred to their own slice); and single-host serving
  (`relay-serve --web-dir` + a dedicated SPA CSP + path-traversal guard + index.html fallback; multi-stage
  Dockerfile). Verified end-to-end against live data across all three themes; backend + Vitest green.
  **Tracker page SHIPPED** (branch `e2-inc4-4a-tracker-page`): the "current focus" general-checklist page —
  circular indicators, legend, grouped roll-ups (`Tracker.tsx` + `TrackerRow`/`TrackerGroup`). It closed
  **gap-8 / KI-22** via a first-class producer `status` field (`not_started|in_progress|submitted|closed`,
  additive — legacy text embed kept; relay folds `in_progress` into `state` + passes raw `status` through),
  chosen over a relay/client text-parse so status is a clean observed property end-to-end. Verified eyes-on
  vs `desktop-04-tracker-sepia.png` across all three themes; backend 759 + Vitest 19 green.
  **Scheduling SHIPPED** (branch `e2-inc4-4a-scheduling`): a pure read-only cross-project derivation —
  `GET /api/scheduling` (`api.serialize_scheduling`) buckets every open dated deadline into OVERDUE / THIS
  WEEK / LATER with a per-row source tag (◇ project / ⊟ tracker) + a summary chip row; `Scheduling.tsx` +
  `ScheduleRow`. Reuses `_deadline_state` / `slipping_item_keys` (no new derivation, no producer/wire
  change). Verified vs `desktop-05-scheduling-sepia.png` across all three themes; backend 764 + Vitest 23
  green. **First production deployment SHIPPED** — the SPA cutover, deployed as a NEW Fly app
  `project-orion.fly.dev` (Fly has no in-place rename); the old `orion-relay-horizon-c`'s DB (reports,
  comments, users) + all secrets were migrated over and the old app destroyed. **Comment writes SHIPPED**
  (`POST /api/reports/:id/comments` — cookie-authed JSON reusing the form route's CSRF/auth/scope/identity
  guards; verified eyes-on incl. XSS-inert render). **Still ahead in 4a's band:** the Showcase guest view,
  the mobile pass, and retiring `render.py` at parity. **Remaining sub-steps:** **4b**
  Disciplines & directions (the
  deferred doc-centric signal, promoted from "adjacent rung": a new collector reading CLAUDE.md/design/
  decision docs + a section); **4c** Cross-project Connections (a new cross-project relationship derivation
  + the SVG-graph section — E4's developer-view flavor). **Kickoff:**
  [`docs/e2-inc4-dashboard-rebuild-kickoff.md`](../docs/e2-inc4-dashboard-rebuild-kickoff.md).
- **Deferred seams (not now):** the no-login guest/showcase view (C3 Inc 3 — viewer logins suffice for
  family today), and non-project/non-code items (e.g. **the applications to-do list above**) via
  `intake` or a new collector (reachable, no recorded pattern yet). Chat-surface enrichment (E3) is
  **parked** — secondary to the dashboard. **Function before looks:** a dedicated dashboard *aesthetic*
  pass is its own later slice.

**Horizon P — Publish / OSS-launch** *(decision-gated, order-flexible — triggered when going public)*

The discrete, one-time push to make the repo publishable. Lettered **P** (not F) on purpose: A–E
imply dependency order, but this band is **not** sequenced after E — it lands whenever the go-public
decision is made (before, after, or partly parallel to E), the same demand/decision-gated idiom as
B5 / C2c / C3. Open-source aspiration is the project's stated long-range goal (personal
infrastructure + portfolio *with eventual open-source*); consolidating the launch work here keeps it
from being interleaved into feature horizons, since it is largely independent of them. Kept coarse
until the decision firms it up.

| Phase | Scope | Status |
| ----- | ----- | ------ |
| P1 | Personal-reference scrub — remove family / a name / `sar_hackathon` and any private paths from committed planning docs (the deferred D3 publish-prep item, now homed here) | 🔭 Decision-gated |
| P2 | OSS scaffolding — `LICENSE`, `CONTRIBUTING.md`, `SECURITY.md`, issue/PR templates, Code of Conduct (the standard public-repo files Orion currently lacks) | 🔭 Decision-gated |
| P3 | Public-repo hygiene / CI — CI suitable for a public, fork-friendly repo (ties to the Actions-quota cap), secret-scanning, branch protection | 🔭 Decision-gated |
| P4 | Final readiness sweep — re-run the ≤10-min setup test from a clean clone, final README/docs pass, confirm no machine-local paths or secrets in any committed artifact | 🔭 Decision-gated |

*The **continuous** "stay OSS-clean / simple setup" discipline stays cross-cutting (below); Horizon P
is only the **one-time launch push**, not that ongoing hygiene.*

**Cross-cutting through every horizon:** security & privacy (redaction + preview, gaining an
inbound validate/authorize side in C), open-source-friendly simple setup (the *continuous* discipline;
the *discrete* launch push is **Horizon P**), cross-platform portability, and cross-machine
interoperability (UTF-8 / UTC-ISO-8601 / canonical `\n`, no machine-local paths in any cross-machine
artifact). The rationale behind Horizon C lives in "Future direction & guiding principles" and
"Cross-platform & future-direction rationale" below.

## Context

Yousuf wants a system that tracks progress on any project he works on (via Claude Code
and git) and reports that progress to people he designates ("supervisors") through Discord
or Slack. The goal is to turn raw development activity into readable progress updates and
deliver them on a chosen cadence.

Decisions locked via Q&A:

- **Signals (all four):** git activity, Claude Code session activity, a task/milestone
list, and manual notes.
- **Cadence (all three):** on-demand, scheduled digest, and event-driven.
- **Channels:** both Discord and Slack.

Refinements (2026-06-13 follow-up):

- **Orion lives in its own repo / directory**, separate from this `applications`
workspace. This workspace becomes just one of the projects Orion tracks. (Resolves the
largest open question.)
- **Claude Code sessions feed Orion via a Claude skill/plugin, not by Orion parsing
session files.** The skill runs inside a coding session, summarizes it, and pushes that
summary to Orion as the latest progress update for that project. The user can also write
a progress update by hand; the skill is support, not a requirement. This flips the
session signal from "Orion parses fragile JSONL" to "Orion receives a ready-made
summary," which is cleaner, less fragile, and more privacy-safe.
- **Not every report needs an LLM.** Structured/manual updates (a to-do or milestone
change, a hand-written note) are passed through and formatted directly by default. The
Claude summarizer is an *optional, conditional* step — not imposed — that by default runs
on raw activity that needs narrating (git diffs, or a session when the skill is not used)
and is skipped for already-written content. The point is that the LLM is available but not
forced: not every action is significant enough to warrant a summary, and some users would
rather write their own. This keeps cost and latency down and matches the "lightest
adequate" principle at the level of the whole pipeline, not just the model. (It is *not* a
rule that git is the only thing the LLM may ever touch — a user could opt in to LLM
summarization elsewhere.)
- **Supervisor replies are a planned later phase**, with two possible paths: native
Discord/Slack threads (works only if both user and supervisor are on that platform), or
comments on a future web dashboard. Deferred, not designed in detail yet.

## The shape of the problem (two conclusions that drive the design)

1. **Local-first, not hosted (at the current stage).** The collectors must read local git
  repos and local Claude Code session files, which live on Yousuf's machine. A Cloudflare
   Worker (his usual hosting preference) cannot reach local files, so the **core runs
   locally**. Only the delivery step makes outbound HTTPS calls to Discord/Slack. This is a
   deliberate deviation from the Cloudflare preference, justified by where the data lives.
   *Local-first is a stage-appropriate choice, not a permanent principle:* as the project
   grows (multi-party collaboration, a hosted dashboard, a shared service), the primary
   grounding may shift toward hosted/hybrid — a deliberate decision to weigh when complexity
   warrants, kept additive by the portable report/intake blob. A hosted dashboard could be
   added later, but it is not the core today. (The privacy/safety guarantees are the part that
   stays permanent through any such shift.)
2. **Reports are LLM-summarized *when they need to be*, with privacy guardrails.** Raw
  diffs and session transcripts can contain secrets and read as noise to a supervisor.
   For those, Orion redacts obvious secrets, then has Claude summarize activity into an
   abstracted, audience-appropriate update. Structured or already-written updates (a
   to-do change, a milestone, a hand-written note, or a summary pushed in by the Claude
   skill) are **not routed through the LLM by default** — they are already report-ready, so
   forcing them through the model adds cost without value. Orion routes each update down one
   of two lanes (see Architecture), so the model runs only where it adds value, and is opt-in
   rather than imposed. A preview-before-send step guards against leaks on the first runs.
3. **Session activity arrives as a pushed summary, not by Orion reading session files.**
  A Claude skill/plugin summarizes a coding session in place and sends that summary to
   Orion. Orion exposes a simple intake (a CLI command and/or a tiny local endpoint) that
   accepts a project name plus an update body. This same intake is what a manual write-up
   uses. It keeps Orion decoupled from the (changeable, sensitive) raw session format.

## Tech choices (what / why / simpler alternative)

- **Python** — consistent with the rest of Yousuf's tooling; rich ecosystem for git,
HTTP, and the Anthropic SDK.
- **Git access via `subprocess` + `git`** — *What:* shell out to `git log` / `git diff`.
*Why:* zero dependency, robust, and we only need read-side commands. *Simpler
alternative considered:* `GitPython` (nicer objects, but an extra dependency for parsing
we can do with `git --format`). Start with subprocess.
- **Anthropic Python SDK (`anthropic`)** for summarization — *What:* official client for
the Messages API. *Why:* this is a summarization task over natural-language + diffs,
exactly a single-LLM-call use case. *Model:* **Haiku 4.5 (`claude-haiku-4-5`,
$1/$5 per MTok, 200K context)** is the lightest model adequate for summarization, which
matches the "lightest adequate model" preference. *Tradeoff:* if Haiku's summaries miss
nuance on large diffs, step up to Sonnet 4.6 (`claude-sonnet-4-6`) for that step only.
Decide empirically after seeing real output.
- **Delivery via incoming webhooks (Discord + Slack)** — *What:* each channel exposes an
incoming-webhook URL; Orion POSTs a JSON message via stdlib `urllib.request`. *Why:*
simplest possible outbound delivery — no always-on bot process, no gateway connection, just
an HTTPS POST, and a single POST needs no `requests` dependency. *(Phase 1 decision,
2026-06-14: `urllib.request` over `requests` — keeps runtime deps at 2.)* *Simpler
alternative / when to upgrade:* a full bot (discord.py / Slack Bolt) is only needed if
supervisors must interact back (commands, threads). For one-way reporting, webhooks win.
- **State store: SQLite (stdlib `sqlite3`)** — tracks per-project "last reported" markers
(last commit hash, last session timestamp, last report time), manual notes, and report
history, so each report covers only the delta since the previous one.
- **Project config: a TOML file** (human-edited, read-only via stdlib `tomllib`) — registry
of tracked projects: repo path, Claude Code session location, recipients/channels,
schedule, share level. *(Phase 1 decision, 2026-06-14: TOML over YAML — zero dependency,
and Orion never writes the config so TOML's read-only nature costs nothing.)*
- **Secrets via `.env` + `python-dotenv*`* (gitignored) — webhook URLs and the Anthropic
API key. Never committed.

## Architecture (components)

Two intake lanes feed a shared composer/delivery path. The **raw lane** needs redaction +
summarization; the **structured lane** is already report-ready and skips the LLM.

```
                 ┌──────────────────────── Orion (local) ────────────────────────┐
 git repo ─────▶ │ Collector ─┐                                                   │
 (raw lane)      │            ├─▶ Redactor ─▶ Summarizer (Claude, conditional) ─┐ │
                 │            │                                                  │ │
 task/milestone ▶│ Collector ─┤                                                 ├─▶ Composer ─▶ Discord
 to-do / notes   │            │   (structured lane: format directly, no LLM) ───┘ │            └▶ Slack
 (structured)    │            │                                                    │
 Claude skill ──▶│ Intake (CLI / local endpoint): project + update body ──────────┘            │
 manual write-up │                                                                              │
                 │ State store (SQLite: per-project deltas, report history) ─────────────────── │
                 │ Config (YAML: projects, recipients, channels, share level) ───────────────── │
                 └──────────────────────────────────────────────────────────────────────────────┘
   Triggers: CLI (on-demand) · cron (scheduled) · git hook (event-driven)
```

- **Collectors** — one per signal. Each returns "what changed since the last report":
git (commits/diffstat) on the raw lane; task/milestone list (newly completed items) and
manual notes on the structured lane.
- **Intake** — a CLI command (and later a tiny local endpoint) that accepts a project name
plus an update body. This is how the **Claude skill/plugin** pushes a session summary in,
and how a **hand-written** update enters. Pushed summaries are already audience-ready, so
they travel the structured lane.
- **Redactor** — strips obvious secrets (API keys, `.env` contents, tokens) before any raw
text reaches the LLM or a channel. Runs on the raw lane; still applied as a safety net on
the structured lane.
- **Summarizer (conditional, opt-in)** — an LLM (Claude Haiku by default; config-selectable
behind a provider-agnostic seam since B4, including a local model) turns redacted *raw* activity
into a concise progress narrative at the project's configured share level. Skipped **by default** for
structured/already-written updates (the model is optional, not imposed — see the "Not every
report needs an LLM" decision above).
- **Composer** — merges whatever this run produced (a summary and/or structured items) and
formats it for each channel (Discord embed / Slack blocks, or plain markdown to start).
- **Delivery** — POSTs to the configured Discord and/or Slack webhooks per recipient.
- **State store** — records what was reported so the next run covers only the delta.

## Phasing (front-load value; defer the fragile parts)

> **Historical (the original MVP sequencing rationale).** The forward-looking phase plan now
> lives in the **Roadmap** section above (horizons A/B/C). This numbered list reflects the
> *original* 7-phase plan and is kept for *why* value was front-loaded the way it was — it has
> since been expanded and re-grouped into horizons. Where the two differ, the Roadmap wins:
> original Phases 1–4 are Horizon A (A1–A4); original 5 → B1; original 6 → B2; original 7
> (supervisor replies / dashboard) is expanded across Horizon C (C1–C3).

1. **MVP — git → summary → one channel, on-demand.** `orion report <project>` reads git
  activity since the last report, redacts, summarizes with Claude, **previews** the
   message, and on confirm POSTs to a Discord webhook. Plus the config + state store +
   `.env`. This is the smallest end-to-end slice that delivers real value.
2. **Easy signals + the structured lane.** Add the task/milestone list and manual notes,
  and the **intake** command that accepts a pushed update (project + body). Both ride the
   structured lane, so this is also where the no-LLM pass-through path gets built and
   proven. Simple, high-signal, and it unblocks the skill in Phase 6.
3. **Slack + routing.** Add Slack delivery and per-project / per-supervisor channel
  routing, so both channels work and different supervisors get different reports.
4. **Scheduled digests.** A `cron` (or systemd timer) entry runs `orion report --all` on a
  cadence. Note: on WSL2, cron only runs while WSL is running — call this out to Yousuf.
5. **Event-driven.** A git `post-commit` / `post-push` hook that accumulates deltas or
  triggers a report.
6. **Claude Code session signal — via a skill/plugin, not a parser.** Build a small Claude
  skill/plugin that, at the end of a coding session, summarizes the session and POSTs that
   summary to Orion's Phase-2 intake (project + body). Orion treats it as a structured,
   already-audience-ready update (no re-summarization). This replaces the original
   "Orion parses session JSONL" idea: it is less fragile (no dependency on a changeable
   file format), more privacy-safe (the summary, not the transcript, leaves the session),
   and reuses the intake from Phase 2. Lives in Orion's repo but is a separable component.
7. **Supervisor replies (future, two paths).** (a) Native Discord/Slack threads, so a
  supervisor can comment under an update — works only if both parties are on that
   platform, and pushes delivery from one-way webhooks toward a bot (discord.py / Slack
   Bolt) to read replies back. (b) A web dashboard where updates are shown and comments
   live. Deferred until the reporting core is solid; flagged here so the architecture
   leaves room for it.

> Deliberate MVP scoping: even though both channels are wanted, Phase 1 ships Discord only
> to prove the pipeline end to end; Slack arrives in Phase 3. Flag this to Yousuf so it is
> a conscious choice, not a silent cut.

## Privacy & safety (cross-cutting)

- Redaction pass before the LLM and before sending (secret patterns, `.env`, tokens).
- Per-project **share level** in config (high-level vs detailed) controls how much the
summary exposes.
- **Preview-before-send** is the default, at least until trust is established.
- The summarizer is prompted to report outcomes and progress, not raw code or secrets.

## Decisions settled (2026-06-13)

- **Where Orion lives:** its own repo / directory, separate from `applications`. Resolved.
- **Claude Code sessions:** fed via a skill/plugin that pushes a summary to Orion's intake,
not parsed by Orion. Resolved — so Orion no longer needs the raw session file format.
- **Supervisor replies:** wanted eventually; planned as Phase 7 with two paths (native
threads via a bot, or a web dashboard). Resolved as "later, leave room for it."
- **Model:** Haiku 4.5 is acceptable for the summarizer; still confirm quality on real
diffs in Phase 1, and remember the summarizer only runs on the raw lane. *(B4, 2026-06-17:
the model/provider is now config-selectable behind a provider-agnostic `Summarizer` seam —
Anthropic by default, or a local OpenAI-compatible model — with Haiku still the default;
see the Phase B4 status section.)*
- **Git payload to the LLM (Phase 1, 2026-06-14):** a **hybrid** — commit messages (intent)
  - diffstat (scope) + a line-capped, secret-filtered code diff (detail). The diff is sent
  only at `share_level = "detailed"`; `high_level` sends messages + diffstat only. Sensitive
  files are excluded from the diff at collection time (Python allowlist of literal paths
  passed to `git diff`), so secret file *contents* never reach the model — only redaction of
  inline secrets is left to the redactor.
- **State store (Phase 1, 2026-06-14):** stdlib `sqlite3` (atomic marker writes, clean
history table, zero dependency) over a JSON file.

## Phase 1 status (2026-06-14)

Phase 1 is **implemented** in `src/orion/` with a 52-test suite (`tests/`). Pipeline:
`config → secrets → state → git collect → redact → summarize (Haiku) → redact → compose → preview/confirm → Discord (urllib) → advance state`. The conditional-LLM lane seam is built
(only the raw lane is wired); leaf-module signatures are stable so Phase 2's structured lane
is additive. **Signed off 2026-06-15** — all release-gate criteria verified (52/52 tests,
redaction corpus + denylist-path tests, seeded-fake-key clean end to end, share-level
behavior). Orion now also tracks its own repo (`[projects.orion]`, `high_level`). The
Phase 2 starting brief lives in `[docs/phase-2-kickoff.md](../docs/archive/phase-2-kickoff.md)`.

**Live verification (2026-06-15):** ran the full path against a throwaway repo with a
seeded fake key and a real Anthropic key + Discord webhook — preview clean, initial and
incremental sends delivered, state advanced, re-run a no-op, stored body redacted. Two bugs
were found and fixed during this run (redaction hit-count double-counting; Discord 403 from
a missing `User-Agent`) — see `[CHANGELOG.md](../CHANGELOG.md)` for the details and fixes.

## Phase 2 status (2026-06-15)

Phase 2 — the **structured lane** — is **implemented** in `src/orion/` with a 90-test suite
(+38 over Phase 1). It was built in seven reviewed checkpoints: per-collector state markers
(+ non-destructive migration of the Phase-1 git marker) → config `tasks_file`/`notes_file`
→ tasks collector → notes collector → merge helper → multi-collector orchestrator rewrite →
`intake` command. The three open decisions were resolved with the user (see above): merge =
one sectioned body; task source = Markdown checklist; intake = CLI command.

What shipped (details in `[CHANGELOG.md](../CHANGELOG.md)`): the **tasks** and **notes**
structured collectors (no LLM), the `**orion intake`** push command (no collector / no LLM /
no marker), a `merge.py` step that combines signals into one sectioned message, and a generic
per-`(project, collector)` marker store so each signal tracks its own delta. The structured
lane never reaches Claude — a test proves a structured-only run does not call the summarizer
even when the summarizer is wired to raise. Frozen seams (`summarize_raw`, `redact`,
`build_report`, `compose`, `delivery.send`) were not changed; net new runtime dependencies: 0.
Two fields are now vestigial (`ReportBlob.source_marker`, `project_state.last_commit`) — see
`[docs/known-issues.md](../docs/known-issues.md)` KI-8.

**Signed off (2026-06-15).** Live check passed: the full structured-lane path ran against a
throwaway project (git + tasks + notes merged into one sectioned report) plus an `intake`
push to the real Discord webhook, with a seeded fake key redacted and an immediate re-run a
no-op. `pytest`: 90/90 at Phase 2 close.

## Phase 3 status (2026-06-15)

Phase 3 — **Slack delivery + recipient routing** — is **implemented** in `src/orion/` with a
105-test suite (+15 over Phase 2). Built in four reviewed checkpoints: Slack sender + channel
config → Slack compose rendering → orchestrator routing → docs + live verification. Three
decisions were settled with the user (see "Decisions settled" above-style notes in the session
plan): Slack format = plain mrkdwn (Block Kit deferred, to be paired with richer Discord
formatting later); preview = one block per channel + one combined confirm; routing scope =
channel routing only (same content per recipient, routed by channel).

What shipped (details in `[CHANGELOG.md](../CHANGELOG.md)`): `delivery/slack.py` (a `{"text": …}` mrkdwn POST mirroring Discord), a `slack` branch in `compose` with a structural
`_to_slack_mrkdwn` translator, and per-channel routing in the orchestrator — each run composes
once per distinct channel and delivers each recipient their channel's rendering via
`_sender_for(channel)`, with one labeled preview block per channel and a single combined
confirm. Both `report` and `intake` route. Frozen seams unchanged; net new runtime
dependencies: 0. A `Recipient` is a delivery *destination* (channel + webhook); the eventual
per-supervisor (per project/task/todo) routing model is deferred (see KI-11 and the session
plan), with today's explicit recipient naming keeping that door open.

**Signed off (2026-06-15).** Live dual-channel check passed: a project with a Discord and a
Slack recipient delivered one report (and one `intake` push) to the real Discord webhook and
the test Slack workspace, each correctly formatted (Discord `**`/`##`, Slack `*…*`), with a
seeded fake AWS key redacted in both. `pytest`: 105/105.

## Phase 3.5 status (2026-06-15)

Phase 3.5 — the **cross-platform portability pass** — is **implemented** in `src/orion/` and
the docs, with a 110-test suite (+5 over Phase 3). An audit (paths, the venv entry point,
`subprocess` git calls, console encoding, file I/O encoding, line endings) confirmed the core
was already highly portable — `pathlib` throughout, explicit `encoding="utf-8"` on every text
read, `splitlines()` for line endings, no `shell=True`, and a component-built timestamp that
already avoids `%-I`/`%#I`. This pass therefore made only targeted fixes, not a rewrite.

What shipped (details in `[CHANGELOG.md](../CHANGELOG.md)`): a `python -m orion` entry point
(`__main__.py`) as the OS-neutral canonical invocation; a console UTF-8 guard
(`cli._ensure_utf8_output`) that keeps the `⚠`/`✗` glyphs but prevents a `UnicodeEncodeError`
on redirected Windows output; a "Supported platforms" + per-OS setup rewrite of the README
(leading with `python -m orion`, OS-agnostic `python -m pip`/`python -m pytest`); and Windows
TOML-path guidance in the README and `orion.toml.example`. No collector/redaction/summarizer/
compose/delivery logic changed; net new runtime dependencies: 0.

Four decisions were settled with the user (rationale in "Cross-platform & future-direction
rationale" below): support matrix = **native Windows + macOS + Linux** (WSL counts as Linux);
canonical invocation = `**python -m orion`**; console Unicode = **keep glyphs + a guarded
`reconfigure("utf-8")`**; and the **scheduling stance** = Orion ships **no scheduler of its
own**, delegating cadence to each OS's native tool (cron / launchd / Task Scheduler),
documented per-OS — the key input to Phase 4 (the carried auto-send tension, then tracked as
KI-12, was resolved in Phase 4 — see the Phase 4 status above and `CHANGELOG.md`).

The test suite is catalogued (categories + why each matters + known gaps) in
`[docs/testing.md](../docs/testing.md)`; the manual cross-OS runbook is
`[docs/portability-smoke-test.md](../docs/portability-smoke-test.md)`. A post-3.5 audit
confirmed all test files are current (nothing stale/removable) and closed five additive
coverage gaps (Slack-token redaction, git noise-glob/diff-cap/subdir-sensitive, the encoding
guard's `OSError` arm).

**Signed off (2026-06-15).** `pytest`: 115/115.

## Phase 4 status (2026-06-15)

Phase 4 — **scheduled digests / unattended send** — is **implemented** in `src/orion/` and the
docs, with a 126-test suite (+11 over Phase 3.5). It was built in five reviewed checkpoints:
the `config.py` `auto_send` field → the `cli.py` `--yes` + `_run_report` refactor (with the
security-critical tests) → `report --all` (fail-soft + summary) → docs (`auto_send`/`--all`/
`--yes` in README + `orion.toml.example`, new `docs/scheduling.md`) → these living-doc updates.
The design was settled with the user up front (see `[docs/phase-4-kickoff.md](../docs/archive/phase-4-kickoff.md)`);
this phase was a build, not a re-plan.

What shipped (details in `[CHANGELOG.md](../CHANGELOG.md)`): the `**auto_send`** per-project
opt-in, the `**--yes**` non-interactive flag, and `**--all**` for every-project runs. The
load-bearing rule is that the human preview is bypassed **only** when `--yes` **and**
`auto_send=true` are both present — `--yes` alone never sends (a non-opted project is skipped and
logged), and `auto_send` alone never sends (without `--yes` the preview always shows). Redaction
is untouched: both passes still run on every path, so unattended delivery relaxes no
secret-scrubbing — it bypasses only the *human* preview, for opted-in projects. `--all` is
fail-soft (one project's error doesn't stop the rest) and exits non-zero **only on a real
failure**, so a scheduler alerts on genuine problems, not on routine no-activity/skipped runs.
Orion still ships **no scheduler of its own**: cadence is delegated to the OS, documented per-OS
in `[docs/scheduling.md](../docs/scheduling.md)` (with the WSL2 caveat and minimal-environment
gotchas). Frozen seams (`redact`, `summarize_raw`, `build_report`, `compose`, `delivery.send`,
the state store) were not changed; net new runtime dependencies: 0.

The security contract is pinned by `tests/test_schedule.py`, including the load-bearing test
that `auto_send` **without** `--yes` still previews, and that a seeded fake key is still redacted
on the auto-send path. `**pytest`: 126/126.** Resolves KI-12; the deferred cadence-aware
`report --all --due` filter is recorded as KI-13.

**Signed off (2026-06-15).** Live verification passed: against a throwaway repo with a real
Anthropic key + the real Discord/Slack webhooks, an `auto_send` project run with `--yes`
auto-sent without a preview and the delivered body had a seeded fake key redacted; the same
project **without** `--yes` still previewed; a `--yes` run of a non-opted project was skipped;
and `report --all --yes` auto-sent the opted-in project, skipped the rest, and a no-activity
re-run sent nothing (all exit 0). A dual-channel run delivered to Discord **and** Slack together,
and the real Phase 4 commit was reported live to both channels via `report orion`. `pytest`:
126/126. A native Windows/macOS scheduler smoke (per `[docs/scheduling.md](../docs/scheduling.md)`

- `[docs/portability-smoke-test.md](../docs/portability-smoke-test.md)`) remains a hardware-gated
follow-up, not a blocker.

## Phase B1 status (2026-06-16) — opens Horizon B

Phase B1 — **event-driven triggers (git hooks)** — is **implemented** in `src/orion/` and the
docs, with a 137-test suite (+11 over A4). Built in four reviewed checkpoints: a pure
`hooks.py` (`build_hook_script` + `resolve_hooks_dir`) → the `cli.py` `install-hook` command →
docs (`docs/git-hooks.md` + README) → these living-doc updates. Decisions settled with the user
(2026-06-16): deliver as a **command + runbook**; default trigger **pre-push** (post-commit also
supported via `--hook`).

What shipped (details in `[CHANGELOG.md](../CHANGELOG.md)`): `orion install-hook <project>`
installs a portable `#!/bin/sh` hook that runs `report <project> --yes` in the background and
**always exits 0**, so it never delays or blocks a commit/push; it embeds absolute paths (the
venv's python, the config, a `<git-dir>/orion-hook.log`), uses forward-slash paths so it's valid
under the `sh` git uses on Windows, refuses to clobber an existing hook without `--force`, offers
`--print` to review, and warns when the project isn't `auto_send`-opted. **The report pipeline is
untouched** — the hook only calls the existing `report --yes`, so all Horizon-A safety guarantees
carry over. No new config field; net new runtime dependencies: 0. Git has no client-side
`post-push` hook, so the local options are `post-commit`/`pre-push` (documented). The
single-file-hook model's limits (hook-manager coexistence, one-project-per-hook) are recorded as
KI-14. One small supporting change landed in `secrets.py`: `load_secrets` now also reads the
`.env` **beside the `--config` file**, so a hook/scheduled run (which starts in another directory)
finds Orion's central secrets via the config path it already passes — fixing secret discovery for
both unattended paths, with unchanged `override=False` precedence.

**Signed off (2026-06-16).** Live verification passed: on a throwaway repo, an installed
**pre-push** hook fired on `git push` (push returned in ~19 ms — never blocked), ran
`report --yes` in the background, and delivered to Discord; the hook log showed
`Auto-sending … / Sent to: …`. The **secrets fix** was confirmed separately: from a foreign
working directory with no exported env, `load_secrets(.../orion.toml)` loaded the central `.env`
purely via `--config`, and a `report orion` run from `/tmp` (no sourced env) delivered to **both
Discord and Slack** — proving unattended secret discovery and dual-channel delivery together.
`pytest`: 139/139.

## Phase B2 status (2026-06-16)

Phase B2 — the **Claude Code session skill** — is **implemented** (the fourth and final
ingestion signal). Built in four reviewed checkpoints: `intake --yes` (cli.py) + its
security tests → the skill artifact `skills/orion-session/SKILL.md` (+ `skills/README.md`) →
docs (README) → these living-doc updates. Decisions settled with the user (2026-06-16):
confirmation = **in-session review + `intake --yes`**; skill location = `skills/orion-session/`.

What shipped (details in [`CHANGELOG.md`](../CHANGELOG.md)): a `Claude Code skill` that drafts an
outcome-focused, secret-free session summary, **shows it for in-session approval**, then pushes it
via `intake … --yes`. Orion does **not** re-summarize — the skill's summary is delivered (after
redaction) on the existing structured lane. The only Orion-side change is `intake --yes`, which
skips the terminal preview for the (non-interactive) skill send; unlike `report --yes` it needs
**no `auto_send` gate** (intake is always an explicit push, never unattended), and redaction runs
unchanged. The skill is a separable artifact outside the Python package (no packaging change);
net new runtime dependencies: 0.

**Signed off (2026-06-16).** Verified two ways: the send mechanism (a sample summary piped
through `intake --yes` from a foreign CWD delivered to Discord + Slack with a seeded key redacted
to `[REDACTED_AWS_KEY]`), and then the **skill run fully end-to-end** — the `orion-session` skill
was installed into `~/.claude/skills/`, invoked in a real session, drafted a summary, showed it
for in-session approval, and on approval delivered it to both channels via `intake --yes`.
`pytest`: 141/141 (+2 for `intake --yes`).

## Phase B6 status (2026-06-16)

Phase B6 — **read-only config-inspect commands** — is **implemented**, closing the
visibility/discoverability gap surfaced while using `install-hook` (KI-15). Built in three
reviewed checkpoints: `projects` + `show` → `check` (validity + readiness) → docs + living docs.
Decisions settled with the user (2026-06-16): build **all three** commands; `check` does
**validity + readiness**; plain-text output.

What shipped (details in [`CHANGELOG.md`](../CHANGELOG.md)): `orion projects` (list every project
with `auto_send`/share level/collectors/channels), `orion show <project>` (one project's resolved
config), and `orion check` (validate the config, then report per-project send-readiness — repo
path exists, each webhook secret present, Anthropic key present for the git lane — by NAME as
set/MISSING, and a non-zero exit if anything required is missing, so it works as a pre-flight
gate). The load-bearing constraint holds: these commands **only read** config — Orion still never
writes it, and a `config set` style command was deliberately excluded (it would break that
decision and need a comment-preserving TOML writer dependency). No secret value is ever printed.
No new runtime dependencies; `config.py`/`secrets.py` reused unchanged. **Resolves KI-15.**

**Signed off (2026-06-16).** Implementation and the automated suite (`pytest`: 148/148, +7 for the
inspect commands) are complete, plus a live check against the real config: `projects`/`show`/
`check` all correct, `check` even caught a genuine missing repo path (a cleaned-up throwaway) and
exited non-zero, and zero secret values were printed by any command.

## Phase B3 status (2026-06-17)

Phase B3 — **richer rendering (Slack Block Kit + Discord embeds)** — is **implemented** in
`src/orion/` with a 154-test suite (+6 net over B6). Built in four reviewed checkpoints: carry
structured sections on `ReportBlob` + per-section redaction (CP1) → the `ComposedMessage`
payload seam with delivery as pure transport, no look change (CP2) → the rich per-channel
renderers + faithful preview + overflow fallback (CP3) → docs + living docs (CP4). The five open
decisions from `[docs/phase-b3-kickoff.md](../docs/archive/phase-b3-kickoff.md)` were settled with the
user (2026-06-17): **(D1)** carry `(title, body)` sections on the blob, `body` stays canonical +
fallback; **(D3)** redaction pass 2 runs per section before the blob is built; **(D2)** the
preview is rendered from the actual payload's text fields; **(D5)** `compose` returns a
`ComposedMessage(payload, preview)` and `send` takes the payload dict; **(D4)** rich rendering is
always-on with a built-in plain fallback (no config toggle; deferred as a clean seam) and a
graceful fallback to the plain message on size overflow.

What shipped (details in `[CHANGELOG.md](../CHANGELOG.md)`): Discord embeds (one field per
signal) and Slack Block Kit (header / context / per-signal section blocks + `text` fallback),
both built from the blob's sections; `compose` now owns rendering + truncation while delivery is
pure transport (`send(payload, url)`); the second redaction pass runs per section so no
block/embed field can bypass it; and a faithful preview rendered from the payload. Frozen seams
held except the deliberate, KI-9-sanctioned change to `compose`'s return type and `send`'s input
(the "message is one string" model). Net new runtime dependencies: 0. **Resolves KI-9**; **KI-10
updated** (the `_to_slack_mrkdwn` translator is still scoped to the two constructs Orion emits,
now applied to each section body rather than the flattened report).

**Live verification (2026-06-17):** the real B3 commit was reported via `report orion` to the
real Discord + Slack channels — Discord rendered the embed card, Slack rendered Block Kit (the
single-section `orion` case differs only subtly from plain, confirmed against a Block Kit probe);
a separate throwaway multi-signal project then delivered an unmistakable multi-section embed /
Block Kit (stacked section blocks + dividers) to both channels, with state advancing and the
re-run a no-op.

**Signed off (2026-06-17).** `pytest`: 154/154. Future rendering polish (e.g. splitting an
oversized report across multiple embeds/messages instead of falling back to plain) is deferred to
a later horizon (Horizon C or beyond), per the user.

## Phase B4 status (2026-06-17)

Phase B4 — **summarizer flexibility (provider-agnostic seam + optional local model)** — is
**implemented** in `src/orion/` with a 172-test suite (+18 net over B3). Built in four reviewed
checkpoints: the global `[summarizer]` config + validation (CP1) → the
`Summarizer` Protocol with the Anthropic backend refactored behind it, default behavior unchanged
(CP2) → the `LocalSummarizer` (OpenAI-compatible endpoint over stdlib `urllib`) + backend-aware
`check` (CP3) → docs + living docs (CP4). The open decisions from
`[docs/phase-b4-kickoff.md](../docs/archive/phase-b4-kickoff.md)` were settled with the user (2026-06-17):
**(1)** the seam is a one-method `Summarizer` Protocol, with `cli._build_summarizer` constructing
the configured backend via explicit provider dispatch (not a registry); **(2)** config is a
single **global** `[summarizer]` table (per-project override left as a clean, additive seam);
**(3)** ship the seam + the Anthropic refactor **and** one real local backend (a single
implementation would be a "fake seam"); **(4)** per-step model choice **deferred** — one LLM step
exists today, so the seam keeps it additive; **(5)** Anthropic keeps `ANTHROPIC_API_KEY`, a local
endpoint needs no key unless `api_key_env` names one; **(6)** every backend fails closed into
`SummarizerError`, and the local backend adds **0** runtime dependencies.

What shipped (details in `[CHANGELOG.md](../CHANGELOG.md)`): the module-level `MODEL` constant and
the standalone `summarize_raw` are gone, replaced by `AnthropicSummarizer` (now using the
configured model) and `LocalSummarizer` behind the `Summarizer` Protocol; both share the one
security-relevant system prompt and the empty-result guard. The default — a config with no
`[summarizer]` table — still routes the git lane through Anthropic/Haiku unchanged. Redaction and
preview-before-send are identical for every backend; a local model is simply more private (no
outbound summarization call). The local backend targets the **OpenAI-compatible** `/chat/
completions` shape (one path for Ollama / llama.cpp / LM Studio / vLLM) rather than a runtime's
native API — a deliberate "engineered enough" call, tracked with its tradeoff and
change-conditions as **KI-16**.

**Signed off (2026-06-17)** on the strength of the 172-test suite. `pytest`: 172/172. The
optional **live/manual verification** carried over from this sign-off was **completed at the
start of the B5 session (2026-06-17)**, against a throwaway git repo delivering to the real
Discord + Slack channels: (1) the **default Anthropic/Haiku** path (no `[summarizer]` table)
rendered a summary and delivered end to end, with a seeded fake AWS key redacted from the
detailed diff; (2) a **local** backend (`provider = "local"`, Ollama at
`http://localhost:11434/v1`, `model = "qwen2.5:0.5b"` — a tiny model, because the check is of
backend *wiring*, not summary quality) rendered a local-model summary and delivered; and (3) the
local backend **failed closed** — an unreachable endpoint surfaced a clean `SummarizerError`
(exit 1), sent nothing, and did **not** advance state (the same delta re-reported on the next
successful run). No B4 follow-up fixes were needed.

## Phase B5 status (2026-06-17) — gate evaluated: deferred into Horizon C

Phase B5 (a scheduling *layer* inside Orion — `report --all --due`, activity-gating, quiet
hours, per-recipient cadence, a unified status view) was always marked **⏳ Conditional**: the
first question is *whether* to build it at all, not *how*. That gate was evaluated this session
and the decision (with Yousuf, 2026-06-17) is to **defer B5 and fold it into Horizon C** — no
B5 code now.

**Why defer:**

- **No concrete need yet.** Mixed per-project cadences are already expressible with multiple
  OS-scheduler entries (one per cadence group), and activity-gating already exists implicitly —
  every run is a no-op when there is no delta. The one named candidate, the cadence-aware
  `--due` filter (KI-13), is a convenience over the multiple-entries approach, not a need anyone
  has hit.
- **Sequencing.** The plan's own analysis ("When a built-in scheduling *layer* becomes right" /
  "Bidirectional interaction moves this") notes that the Horizon-C bidirectional **listener** and
  the "cadence needs Orion's *own* state" moment likely arrive together: once an always-on
  process exists to host a listener, an in-process scheduler is nearly free. Building B5
  standalone now would be "building the future" before the process that should host it exists.
- **The seam is already clean.** Picking B5 up in Horizon C is additive, not a rewrite: a
  per-project `schedule` field would mirror the existing `share_level` / `auto_send` validation
  in `config.py`, and "last successful report time per project" is **derivable from the existing
  `report_history` table** (`SELECT MAX(sent_at) ... WHERE project = ?`) — no new schema needed.
  `report --all` is the layering point for a future `--due` filter.

**What this means for the roadmap:** with B1–B4 and B6 signed off and B5 deferred, **Horizon B's
local-automation scope is complete.** The security invariants are untouched — the `--yes` +
`auto_send` preview-bypass gate and both redaction passes carry forward unchanged into whenever
B5's logic is eventually built. KI-13 records the deferral and the no-new-schema implementation
note for whoever builds it.

Also completed this session: the carried-over **B4 live/manual verification** (see the Phase B4
status block above) — the default Anthropic path, a local Ollama backend, and local fail-closed
all verified end to end against real channels.

## Horizon B → C boundary review (2026-06-17)

With Horizon B complete (B1–B4, B6 signed off; B5 deferred), this is a deliberate **consolidation
pass at the B→C boundary** — *not* detailed Horizon-C planning, which stays deferred until C
nears (designing C1–C3 in detail now would be "building the future"). Its job is to lock in two
strategic insights from this session and confirm the seams C will lean on are clean.

**Insight 1 — the summarizer has a capability floor (≈Haiku-4.5).** The B4 local-backend
verification ran a very small model (`qwen2.5:0.5b`) and it was noticeably worse — rough, partly
hallucinated summaries — confirming the long-suspected point that the summarizer needs roughly
Haiku-level capability to be *usable*. Implication for the B4 local-model option: "lightest
**adequate** model" means a mid-capability model, not the tiniest; sub-adequate models produce
poor output. A **model-tier comparison** (cloud vs local; where the cost/adequacy sweet spot
sits) would be useful but is **non-foundational** — a future experiment, not a phase. Tracked in
KI-4; the local-model docs (`README`, `orion.toml.example`) were reconciled to this framing.

**Insight 2 — webhooks are an inbound dead-end, so bidirectionality means bots (a build *and
maintain* cost).** An incoming webhook is outbound-only by construction; there is no upgrade path
to *receive*. Reading a supervisor's reply requires a registered **bot** per platform — Discord
(Gateway WebSocket + `MESSAGE_CONTENT` intent, or an Interactions endpoint) or Slack (Events API
public endpoint, or Socket Mode) — i.e. a new **always-on listener** that tips local-first →
hosted, plus ongoing maintenance (OAuth scopes/intents, request signing, reconnect/dedup, rate
limits, platform review). Two mitigations, already latent in the design and made explicit here:

- **Delivery and interaction are separable.** A bot for *inbound* need not replace webhooks for
  *outbound*; and because delivery is already behind a per-channel seam (`delivery.send(payload,
  url)` + `_sender_for(channel)` + the portable blob), a bot *sender* — or a hosted relay — can be
  added **additively**, without touching the report pipeline.
- **Dashboard-first defers bots.** C1 (a read-only web dashboard + hosted relay) provides a
  comment surface and the hosted shift **without any bot**; native in-platform replies become a
  *richer add-on* (C2), not a foundation.
- **Decision (with Yousuf, 2026-06-17):** native in-platform Discord/Slack discussion **is a
  wanted feature**, but it does **not** need to precede the existing Horizon C content (the web
  dashboard, etc.). Its exact slot is left to detailed Horizon-C planning. The C2 gate to settle
  then: is native in-platform reply a *must-have*, or is the dashboard sufficient as the primary
  interaction surface (bots as the add-on)?

**Seams Horizon C depends on — confirmed clean (invariants to protect):**

- **Delivery transport is swappable (webhook → bot → hosted relay).** Per-channel sender dispatch
  plus the payload/blob seam keep an outbound bot sender or a hosted relay additive. *(Stated
  explicitly here as an invariant for the first time.)*
- **Portable report/intake blob** (summary + metadata; no machine-local paths) — the seam for
  moving delivery/presentation hosted.
- **Named recipients / participant model** (KI-11) — recipients are named destinations today; the
  per-supervisor participant graph (C3) is additive on top.

**On-ramp (sketch only, not a design):** the gentlest first C step is a **read-only dashboard fed
by the portable blob** — collection stays local, presentation moves hosted — which aligns with
the Cloudflare hosting preference. Bidirectional + bots follow later (C2), behind the dashboard,
at a slot to be decided in detailed planning. Detailed C1–C3 design remains deferred — C1 is
framed (open decisions surfaced, not settled) in
[`docs/phase-c1-kickoff.md`](../docs/archive/phase-c1-kickoff.md).

## Phase C1 status (2026-06-18) — first slice: hosting-agnostic relay + read-only dashboard

**Opens Horizon C.** Built the part of C1 that needs **no** hosting decision: a **vendor-neutral
outbound relay seam** on the local side, and a **Path-B reference implementation** of the hosted
half (a small stdlib Python relay + read-only dashboard) in a new, separately-deployable top-level
`relay/` package. **Zero new dependencies, no core-pipeline changes.** Implemented across eight
checkpoints (CP1 `serialize_blob` → CP2 `[relay]` config → CP3 `delivery/relay.py` → CP4 fail-soft
wiring into `report`/`intake` + `check` readiness → CP5 `relay/store.py` → CP6 `relay/server.py`
ingest → CP7 `relay/render.py` + dashboard routes → CP8 `orion relay-serve` + docs). `pytest`:
**226** (172 at B-close). Verified end to end by a local dogfood: `relay-serve` + a real `intake` →
delivery → relay push → store → dashboard (index → history → report). **Awaiting sign-off.**

**The vendor-neutral invariant (treat as PERMANENT):** local Orion knows *only* "serialize the
portable blob (JSON) + a Bearer token → POST to a configured URL," `orion_version`-stamped. That
single seam decouples the local core from any hosting choice, so the **same** outbound push works
unchanged against a future Cloudflare ingress — the hosting choice stays genuinely open and is now
*informed* by a working Path-B reference.

**Accepted caveat (this slice):** the local relay is **loopback-only** (`127.0.0.1`), so the
dashboard is for the user's own machine; informal supervisors still see Discord/Slack delivery, not
the dashboard, until hosting lands.

**Deferred — with a near-term revisit point (NOT indefinite).** These were scoped out of *this
slice* and are slated for a **next-phase planning/decision juncture right after C1 sign-off**
(sooner than later, per Yousuf):

- **The hosting decision (Path A Cloudflare vs Path B self-host)** — **RESOLVED 2026-06-18 →
  Path B** (self-host). See the "Hosting decision (settled 2026-06-18)" section just below for the
  rationale and the E2E bridge to a future managed option. Receiver stays loopback-only until an
  actual hosted deployment is built.
- **OSS-readiness polish pass — alongside that next-phases discussion (Yousuf's explicit call):**
  CI, CONTRIBUTING, SECURITY, issue/PR templates, README/docs polish (WSL2-cron caveat, a "you're
  ready" checkpoint), **plus** the friction items — KI-1 dual-channel partial-failure policy, a
  new-repo blob baseline / `--init`, and the `orion-session` abs-path ergonomics.
- **Dashboard maturation (rides with the hosting decision):** the C1 dashboard was deliberately
  minimal (plain server-rendered HTML) — right for a "does it work" pass, not a finished surface.
  **Visual design: done (2026-06-19).** With the dashboard now supervisor-facing (post-C2 deploy), a
  **refined-minimal restyle** landed (token-based palette, type scale, styled list/report/comment
  components) plus **California-time display** (DST-correct PDT/PST via `zoneinfo`+`tzdata`) and a
  **relative-timestamp progressive enhancement** (one inline script; the page stays fully functional
  with no JS). Decisions this pass: keep it inline-CSS + light JS (no static-asset routing, no
  framework); larger features (sidebars, etc.) deferred to a dedicated dashboard-expansion design.
  **Still deferred:** richer **report content** — submitter/author accountability (**KI-17**) — which
  is entangled with multi-party identity (C3) and reshapes the design, so it waits for C3. (New seams
  recorded: **KI-19** inline-assets vs. a future CSP; **KI-20** message-formatter still UTC.)
- **C2/C3 sequencing** (bidirectional, multi-party) — **juncture RUN 2026-06-19** (see "Horizon-C next
  slice decided" below). C2 shipped (dashboard comments + the comment pull-back), so the pass decided
  the *next* slice: **native Discord/Slack bots**, with **C3-proper and E2E demand-gated** and the
  dogfood reframed as a **refinement input, not a gate**. The kickoff is
  [`docs/phase-c2-bots-kickoff.md`](../docs/archive/phase-c2-bots-kickoff.md); it supersedes the archived
  [`docs/archive/horizon-planning-kickoff.md`](../docs/archive/horizon-planning-kickoff.md) for the
  post-C2 state.

Detail on the slice's settled decisions (D1–D7) is in
[`docs/phase-c1-kickoff.md`](../docs/archive/phase-c1-kickoff.md) and the approved checkpoint plan.

## Hosting decision (settled 2026-06-18) — Path B (self-host), with E2E as the bridge to managed hosting

**Decision: Path B — a portable, self-hostable Python relay (the C1 reference). NOT all-Cloudflare
(Path A), NOT the hybrid.** Settled with Yousuf after re-verifying current (mid-2026) platform facts.

**Why Path B (verified, not assumed — checked 2026-06-18):**

- **Path A would be a stack-divergent rewrite, not "deploy what we built."** Cloudflare Python
  Workers are **still in open beta** and require Cloudflare's `WorkerEntrypoint`/`fetch` handler
  model, storage via **D1 bindings** (not the `sqlite3` stdlib), and the `pywrangler` toolchain. Our
  `http.server` relay + `sqlite3` store don't port — "it's Python too" doesn't save the rewrite.
- **D1 exposes no external connection** to the raw data, locking the redacted-but-sensitive data
  inside Cloudflare's ecosystem — against own-your-data.
- **Path B deploys the code we already wrote** (same language/ecosystem), is vendor-neutral, runs
  anywhere (Fly ~$2/mo, Render free-with-sleep or $7 warm, a VPS, or **free on a Pi/home box**), and
  keeps the data on infrastructure the user controls — the cleanest "anyone can run it" story. Its
  only cost vs A is a few $/mo (or a box) + keeping a process alive; **zero if self-hosted**.
- **The hybrid is rejected for now:** it means building AND maintaining *two* implementations (the
  Python relay + a Cloudflare rewrite) in lockstep — a standing maintenance tax for one upside (free
  personal hosting), unjustified at this stage.

**Path A is NOT foreclosed.** Because C1 locked the vendor-neutral contract, a Cloudflare ingress
(a Worker speaking the same blob+token contract, writing D1) can be added **later as an additional
deploy target** without touching local Orion — a documented future managed-deploy *option behind the
seam*, not a parallel build we maintain.

**The privacy bridge — using managed hosting WITHOUT degrading own-your-data: end-to-end (client-
side) encryption.** Own-your-data has two routes: (1) hold the bytes yourself (self-host = Path B),
or (2) hold the *keys* yourself — let a host store **ciphertext it cannot read** ("zero-knowledge"),
decrypting in the viewer's browser with a key the host never sees (how password managers use
untrusted cloud). Path A *naive* (plaintext in D1) fails because it does neither. The future path to
privacy-preserving managed hosting is therefore:

- an **encrypt-before-push** step — local, *additive* to the existing seam; authenticated symmetric
  crypto (adds a `cryptography`/`pynacl` dependency, the one real cost against minimal-deps);
- the dashboard shifts **server-rendered → client-decrypting** (static page + ciphertext + in-browser
  decrypt — where Cloudflare **Pages** would finally fit);
- **key management** — single-user is one key; multi-supervisor needs key distribution / envelope
  encryption, so this is **entangled with C3** (multi-party identity *is* key distribution);
- **metadata** decisions (encrypted bodies still leak cleartext project/timestamps unless those are
  encrypted too).

This is **a future horizon (intertwined with C3), not now** — but the C1 seam doesn't block it
(encryption slots in additively before the push; the blob is `orion_version`-stamped). Mental model
to carry: **self-host = trust by ownership; managed + E2E = trust by cryptography** — both satisfy
own-your-data; Path A naive satisfies neither. **Near-term: Path B, plaintext, self-host;** E2E is
the documented bridge to a managed option later.

## Horizon-C direction settled + C1 second slice (2026-06-18)

The deliberate **post-C1 direction-setting pass** (the `plan-direction-before-building` juncture).
Run with foundational rigor — *don't default, justify*. The hackathon (6/20–21) was reframed as a
**readiness test, not a driver** of direction. Strategy detail lives in
`[docs/orion-strategy.md](../docs/orion-strategy.md)`; the as-launched kickoff is archived at
`[docs/archive/horizon-planning-kickoff.md](../docs/archive/horizon-planning-kickoff.md)`.

**Settled:**

- **North star — an excellent OSS *solo-dev → supervisor* tool, *scale-invariant by aspiration*** (no
  scale a second-class citizen; every multi-party feature zero-cost at one participant). Current build
  focus stays solo→supervisor; multi-party is a clean seam. **Driver: showcase / learning** — which is
  *why* the next destination is C2, not polish-only.
- **Differentiators (earned, not bolted on):** data sovereignty (own-your-data/local-first) ·
  derives-from-existing-work (*reframing, not originating*) · agentic-execution-native (ingests Claude
  Code sessions) · scale-invariance. Ease-of-use and redaction/preview are *enablers/quality bars*, not
  differentiators alone.
- **Sequence — C2 next · C3 deferred · E2E deferred as the documented bridge.** C2 (bidirectional) is
  inside the north star (a supervisor replying to *your* reports — the loop getting richer, not
  multi-party) and the richest architecture on the board. C3 (multi-party product leap) and E2E
  (managed-hosting privacy bridge) stay seams.
- **Methodology overlay — OGSM + Cagan's Product Operating Model carried as *thought processes*, not
  installed frameworks** (augment the discipline, never replace it): OGSM = a recurring
  success-articulation step; POM = a solo-scaled "outcomes over output / discovery / focus" mindset
  guarding against becoming an order-taker to one's own roadmap. Filled the gap this pass exposed — an
  explicit definition of success (see the strategy doc's Goals).

**C1 second slice (the immediate build, Thu–Fri pre-hackathon):** deploy the relay **beyond loopback**
(Path B) + **harden** the dashboard, as one push (hardening *is* the deployed thing's quality).
Security gate first: the dashboard GET routes have **no read auth** today (loopback-only), so leaving
loopback requires **HTTP Basic-Auth on GET routes + a fail-closed guard** (refuse a non-loopback bind
without the view secret) before any deploy. The local push side is already deploy-ready (the `[relay]`
URL accepts any host; Bearer auth + fail-soft + tests exist).

**Recorded deferred directions** (detail in the strategy doc — *seams kept clean, not built*):

- **Light planning/tracking layer** — the to-do/milestone leg evolves from retrospective-only toward a
  *derived* forward-looking layer (milestones/sprints/due-dates/at-risk), governed by **reframing, not
  originating** (no data re-entry; Orion stays downstream even for planning). Converges with the
  deferred scheduling layer (**KI-13**) and Horizon C's stateful process.
- **Long-range vision (Horizon E, aspirational/unvalidated):** Orion as a *coordination/visibility*
  layer (not an execution platform — complementary to Claude Code), **surface-plural** (native
  Slack/Discord *and* the dashboard), multi-project/cross-project. The inflection to watch is
  read-only → read-*write* dashboard.

## Horizon-C next slice decided + E2E confirmed (2026-06-19)

The post-C2 run of the horizon-planning juncture (supersedes the "Sequence — C2 next" snapshot from
the 2026-06-18 pass, now that C2 is done). Detail and the dogfood capture sheet live in
[`docs/phase-c2-bots-kickoff.md`](../docs/archive/phase-c2-bots-kickoff.md).

- **Next slice DECIDED: native Discord/Slack bots.** Among {OSS-readiness polish, light
  planning/tracking layer, native bots}, native bots is next — it deepens the *delivery surface*
  (where supervisors already are) into genuine two-way, the natural progression from C2's dashboard
  comments, and fits the recorded surface-plural long-range vision. OSS-polish and the planning layer
  remain valid *later* slices.
- **Dogfood (6/20–21) is a refinement input, NOT a gate.** It tests the current features; the realistic
  outcome is expand/refine, not remove — so the direction is decided now and the dogfood tunes the
  bots' scope / smallest-first-slice (esp. the "where was the value — delivery / dashboard / skill?"
  read). We do not defer the decision to "after the weekend."
- **E2E confirmed as a documented bridge, NOT a committed goal.** Stay self-host + plaintext (Path B);
  adopt E2E only if managed-hosting-*for-others* becomes real (genuinely C3-gated). The seam stays
  clean (encrypt-before-push is additive; the blob is version-stamped).
- **C3-proper stays demand-gated** (multi-party identity — KI-17 / KI-11 / KI-1) — committed only on
  real multi-party demand, separate from the dogfood.
- **Architectural learning carried into the bots design:** C2 needed **no local always-on listener** —
  it reused the deployed relay's ingest + comment POST + a **pull** (`orion comments` + a local
  watermark). Native bots WILL force an always-on listener (Gateway / Socket Mode), so how it relates
  to the existing pull/relay model is a first-order design question for that slice.

## C2-bots status (2026-06-19) — native Slack bot, first slice (built)

The native-bots slice, built in four reviewable checkpoints (PRs #16–#19), `pytest` **367**. Scoped
in [`docs/phase-c2-bots-kickoff.md`](../docs/archive/phase-c2-bots-kickoff.md); operator guide in
[`docs/slack-bot.md`](../docs/slack-bot.md). The four open design questions settled thus:

- **Platform / connection model — Slack, Socket Mode.** An *outbound* WebSocket → **no public inbound
  endpoint** to host or secure (the smallest, most secure posture; the Events-API HTTP path would add
  a public URL + signature verification). Discord (Gateway) is the planned next platform — additive
  via `SUPPORTED_BOT_PLATFORMS` + a new shell, since the config, the pure core, and the relay endpoint
  are all platform-neutral.
- **Listener ↔ pull/relay relationship (the first-order question) — the bot FEEDS the relay.** It is a
  third Bearer-authed machine client (alongside push and pull), POSTing replies to a new **`POST
  /api/comments`** that lands them in the *existing* `report_comments` store. So the bot does **not**
  replace the pull and does **not** co-locate with the relay; the dashboard and `orion comments` are
  unchanged. The C2 learning held: no new server, just a new machine endpoint + an outbound listener.
- **Report mapping — channel→project, attach-to-latest.** A reply attaches to the project's latest
  report; no message→report map (which would force the bot to *post* reports — out of scope). The
  endpoint already takes an optional `report_id`, so reply-targeting later is a **bot-only** change.
- **Dependency — `slack-bolt`, optional + lazy.** First new runtime dep, quarantined to an optional
  extra (`orion[slack-bot]`) imported only inside the bot shell; core install stays 3 deps. Bolt's
  sync App + `SocketModeHandler` run on a background thread, so **no asyncio** enters the codebase.

**Architecture:** a pure decision core (`src/orion/bot/core.py`, the threat model as ordered guards),
a sync relay client (`relay_client.py`), and a thin Bolt shell (`slack_bot.py`); the relay change is
one endpoint. **Inbound security:** no public surface; two-pronged loop prevention (bot/`bot_message`
— Orion's own reports arrive as webhooks); configured-channels-only; untrusted text never parsed as a
command; Bearer write; redaction stays outbound-only.

**Stage-bound:** the optional-extra/lazy shape is the smallest-slice choice, expected to **graduate to
a first-class integration** as the bot becomes load-bearing — the seams keep that additive (build the
interface, defer the implementation). **Dogfood (6/20–21)** remains a refinement input, not a gate.

## Meta-layer feature ideas reconciled + dogfood captured (2026-06-23)

The first real dogfood (6/20–21 hackathon, on `sar_hackathon`) and the seven feature ideas parked in
[`docs/feature-ideas-meta-session.md`](../docs/feature-ideas-meta-session.md), reconciled against
this roadmap. Full detail — per-idea verdicts, the config-write analysis, the focus-test — lives in
[`docs/feature-ideas-reconciliation.md`](../docs/feature-ideas-reconciliation.md); the summary:

**Dogfood:** used Orion for hackathon progress reporting (not as a to-do list). Value landed in **all
three surfaces** — the `orion-session` skill, the dashboard history, and delivery + the comment loop
(dad viewed reports and commented; replies pulled back via `orion comments`). #1 finding:
**setup/onboarding friction** — no way to register a project from its own directory (`orion project
<x>` needs it registered on the Orion side first), while the session skill "just works" from any
directory. Dad asked for a **real dashboard login** (the Basic-Auth view credential doesn't persist).
The **Slack bot was never exercised** (verification debt). Implication (recorded, not acted on):
points at **OSS-readiness / onboarding polish** next, reversing the lean in
[`docs/post-dogfood-kickoff.md`](../docs/archive/post-dogfood-kickoff.md).

**Reconciliation (verdict → placement):**

- **#1 incubator-as-5th-signal** — *new signal on a planned principle* (modular signals, L921–929 —
  the first real test of it). With #2, the strongest candidate.
- **#2 audience-typed routing** — *adjacent: the C3 routing future made concrete* (per-supervisor
  subscriptions, L71/L350–351; KI-11/17/1). Lands on the held-open "named recipients / participant
  model" seam.
- **#3 portfolio-aware `--all`** — *fold into the deferred `--due` / KI-13 filter*; not standalone.
- **#4 graduate-idea → register project + intake event** — *kept whole*; motivates reconsidering the
  config-write rule (below).
- **#5 dashboard as meta-layer surface** — *deferred dashboard-expansion + light-planning-layer
  (L821–828)*; the auth sub-piece now has a real external pull (dad).
- **#6 `orion status` backlog view** — *genuinely new, cheap, low-risk* (derivable from
  `report_history`, the gap named at L960); an OSS-readiness / QoL candidate.
- **#7 summaries inherit Writing Style** — *mostly already done* (the `summarize.py` prompt + the
  lean-directional preference); a one-line prompt-tuning todo, not an inheritance mechanism.

**Strongest = the #1+#2 pair, with #2 enabling #1** — an incubator signal is worth building only
because its updates target a *different audience* than git progress (the weekend pattern: supervisor
gets commits/tasks; family/mentors get ideas). Placement-deciding open question for a later plan-mode
pass: **can lightweight audience-typed routing ship onto today's named recipients (tag each with its
signal types) WITHOUT the full C3 participant graph?** Yes → an early additive slice on the existing
seam; needs authenticated identity → waits for C3.

**Config-write rule — reconsider, don't remove (revised #4; its own plan-mode pass).** Keep the
spirit (*no silent config mutation as a side effect of a `report`/`intake`/`collect` run*); relax the
letter to allow a deliberate, explicit, user-invoked `orion add-project`-style command that
**appends** a `[[projects]]` stanza (preview-before-write, dependency-free — stdlib `tomllib` is
read-only but an appended known-shape stanza needs no TOML *writer*). This is the **same knot as the
dogfood's onboarding friction**, so fixing it the right way serves both. Touches a "hard constraint,"
so it gets its own pass.

## D1 — Config-write invariant refined — `orion add-project` shipped (2026-06-23)

Acting on the reconciliation above (idea #4 + the dogfood's #1 onboarding friction): the
"Orion never writes config" rule is **refined, not removed**. The kept invariant is *config is
never written as a side effect of a `report`/`intake`/`collect` run*. The new exception is one
explicit, user-invoked writer — **`orion add-project`** — that previews before writing and is
**append-only** (it never rewrites existing content, so hand-written entries and comments survive).

- **Ergonomics:** run from inside a repo, it infers the project name (the directory) and
  `repo_path` (the git top level), so `cd myproj && orion add-project --like orion` registers a
  project in one line. Recipients come from `--like <project>` (copy an existing project's) and/or
  `--recipient "Name:channel:ENV_VAR"`. Modes: `--print` (show, write nothing), default
  preview+confirm, `--yes` (non-interactive — the entry point `graduate-idea` can call). Creates a
  minimal `orion.toml` when absent, else appends; re-loads after writing to prove validity.
- **Design:** a pure builder (`src/orion/scaffold.py`: `render_project_stanza`,
  `parse_recipient_spec`) reusing `config.py`'s validators/constants, plus `cli.cmd_add_project`
  for inference + I/O — mirroring the `hooks.build_hook_script` / `cmd_install_hook` split.
  Dependency-free: stdlib has no TOML *writer*, but appending a known-shape stanza needs none
  (values that would require escaping are rejected, not escaped). `auto_send` is always written
  `false` — enabling unattended send stays a deliberate manual edit.
- **Tests:** `tests/test_scaffold.py` (render round-trips through `load_config`) and
  `tests/test_add_project.py` (create/append, `--like`/`--recipient`, cwd inference, the three
  modes, every refuse-to-write error path). Suite: **391** (was 367). The `config.py` header now
  states the refined invariant.
- **Still hand-managed (out of scope):** secrets in `.env`; editing/removing existing projects;
  the `auto_send` opt-in. Calling `add-project` from the `graduate-idea` skill is an additive
  follow-up (the `--yes` entry point is in place).

## D2 — `orion status` shipped (2026-06-24)

The cross-project "what still needs reporting?" digest (idea #6) — a read-only command that lists,
for every project, whether any signal has **new unreported activity** (`new: git`) or is `up to
date`, plus how long since its last report. No new schema, no network, no LLM, nothing sent.

- **Design:** reuses the report flow's own detector (`_collect_for` + `CollectorResult.has_activity`)
  so status can never disagree with what a real `report` would find, and reads the last-report time
  from `report_history` via a new `state.get_last_report_time` (`MAX(sent_at)`). Per-collector
  fail-soft (a missing repo path shows `unreadable`, never crashes); exit 0 on success. Mirrors the
  `projects`/`check` read-only command shape.
- **Tests:** `tests/test_status.py` (never-reported → new; up-to-date after a report; new commit →
  new again; fail-soft missing repo; multi-project tally) + a `get_last_report_time` unit test. Suite:
  **397**. Verified by hand across the real config and a temp two-project workspace.
- **Next in Horizon D:** D3 (OSS-readiness polish), then the D4/D5 incubator-signal + audience-routing
  pair.

## D3 — OSS-readiness polish (2026-06-24)

Docs-only pass making the public-facing docs match the shipped reality and positioning Orion honestly
for an eventual open-source reader.

- **Onboarding/staleness:** README status line + blurb now say A–C shipped / Horizon D underway and
  surface `add-project` + `status`; `docs/new-project-setup.md` leads with `orion add-project` (hand-edit
  kept as the alternative) plus an activation note and a `status` mention; the "2 runtime deps" claim
  corrected to **3** (`anthropic`, `python-dotenv`, `tzdata`) across README / new-project-setup / CLAUDE.md.
- **Positioning:** a new README "How it compares" section names the incumbents (Gitmore, Gitrecap,
  dev-journal, async-standup bots) and states Orion's honest edge (four-signal incl. Claude Code
  sessions, the two-way loop, own-your-data), framed as personal infrastructure + portfolio, not a
  product launch. The broken `~/Developer/incubator/...` "Strategic assessment" pointer is removed
  (it 404s for any cloner); the new section is self-contained.
- **Verified:** ran the ≤10-min setup test for real — fresh venv → `pip install -e .` → `add-project`
  → `check` → `status` in seconds, matching the docs (stopped before a real send). Suite unaffected (397).
- **Deferred to a publish-prep pass:** scrubbing personal references (dad / family / a name /
  `sar_hackathon`) from committed planning docs — honest historical context for now; revisit closer to
  going public. **Next:** D4/D5 (incubator-signal + lightweight audience-typed routing).
  *(Update 2026-06-24: this scrub is now homed as **Horizon P, P1** — the decision-gated
  Publish / OSS-launch band — rather than a loose deferred note.)*

## D4/D5 — both shipped; Horizon D complete (2026-06-24)

- **D5 SHIPPED (2026-06-24, this PR).** Per-recipient audience-typed routing on today's
  named-recipient model with **no identity/C3 work**: a `signals` filter is config-level *content
  filtering*, orthogonal to the per-recipient state/identity KI-1/KI-11/KI-17 defer to C3.
  Backward-compatible — omitting `signals` = the recipient gets everything.
  What landed:
  - `config.Recipient` grew `signals: tuple[str, ...]`; `_parse_recipients` defaults it to the
    project's collectors and validates it as a non-empty subset (`_parse_recipient_signals`).
  - `_run_report` carries each section's collector name, groups recipients by
    `(channel, frozenset(signals))` (`_audience_groups`), composes one **filtered** blob per audience
    (reusing `merge_sections`/`build_report`/`compose` unchanged), skips audiences whose signals were
    idle, and previews/delivers per audience. `_deliver`/`_preview_and_confirm` were made
    key-agnostic so report (keyed by channel+signals) and intake (keyed by channel, **unfiltered** —
    a push has no per-signal sections) share one path.
  - The **relay push keeps the full, unfiltered blob** — D5 filters chat delivery only, not the
    dashboard's record.
  - Tests: 4 config (default-to-all, subset kept, unknown-signal rejected, empty-list rejected) +
    3 CLI (disjoint slices routed, idle-signal recipient gets nothing, relay gets the full report).
    Verified by a real CLI run: a notes-only and a tasks-only recipient received different filtered
    previews from one run.
- **D4 SHIPPED (2026-06-24, separate PR).** A new structured-lane collector
  `collectors/incubator.py` reads the incubator `index.md` table into a `{idea: status}` map.
  What landed:
  - The parser locates the **Idea / Status** columns by header (tolerates re-ordering, extra
    columns, a missing pitch column) and identifies an idea by its title — unwrapping a
    `[Title](path)` Markdown link. No table / no Idea+Status header is a valid empty pipeline, not
    an error; a missing file raises `IncubatorError`.
  - Delta logic mirrors `tasks.py`: `new_marker` = `json.dumps(current_map, sort_keys=True)`;
    `has_activity` = any **new idea** or **status change**; removals are silent (like an unchecked
    task). `raw_text` = "transitions + pitch" — `- New idea: Title (status)` with the one-line pitch
    indented beneath, and `- Title: old → new` for a status move.
  - Wired additively as the fifth collector across the enumerated slots: `SUPPORTED_COLLECTORS`,
    `COLLECTOR_FILE_KEYS`, `ProjectConfig.incubator_file`, `_parse_project`, and in cli.py the import,
    `_COLLECTOR_TITLES["incubator"] = "Idea pipeline"`, the `_collect_for` dispatch, and
    `IncubatorError` in all three collector-error `except` clauses (report, status, baseline).
  - Configured as a **dedicated `[projects.incubator]`** routed to mentors/family via D5 `signals`
    (example in `orion.toml.example`).
  - Tests: 10 collector unit (parse/delta/round-trip/link-identity/reordered-columns/removal/
    missing-file/no-table) + 2 config + 1 end-to-end CLI. Verified against the **real**
    `~/Developer/incubator/index.md`.
- **Horizon D is now complete** (D1–D5 shipped). Horizon E's **dashboard-visibility track is now
  building incrementally** (E2 Inc 1 — portfolio overview home — shipped 2026-06-25; see the Horizon E
  table and its ladder note). Its other tracks — chat/bots (E3) and the read-write inflection (E5) —
  stay recorded, not built.

## E2 Inc 2 + 2.5 shipped + a relay CSRF fix (2026-06-25)

Three changes landed on `main` and were deployed to the live Fly relay this session (detail record;
the Horizon E table + ladder above are canonical):

- **E2 Inc 2 — live checklist signal (PR #47).** The to-do/milestone checklist now reaches the
  dashboard: a full pipeline (`collectors/tasks.snapshot` → optional `checklist` blob field → a new
  project-level `relay_project_checklists` table, upserted → a portfolio-card "X/Y done" badge + a
  report-page "Current checklist" block). Project-level **live** model (one current row per project, not
  a per-report snapshot); opt-in per-project `checklist` toggle (requires the `tasks` collector);
  reframing-only; CSP-safe; each item redacted. The new table is created by `CREATE TABLE IF NOT EXISTS`
  so it needed **no migration** on the deployed DB.
- **E2 Inc 2.5 — near-real-time checklist push (PR #49).** The dedicated checklist-only push so a
  `tasks_file` edit reaches the dashboard between reports: a Bearer-authed `POST /checklist` endpoint
  (reuses the ingest token + `upsert_checklist`, no report row), a `delivery/relay.push_checklist`
  client, an `orion checklist-push <project>` command with a `--watch` poll loop (stdlib content-compare,
  pushes only on change), and the live checklist now also on the **project page** (the watch surface).
  Decided poll over `watchdog` (stdlib/minimal-dep). Redaction shared with the report path via
  `_redacted_checklist`.
- **Relay CSRF comment-bug fix (PR #48).** A logged-in admin's comment got a 403 ("blocked by an origin
  (CSRF) check") because `_origin_error` treated a missing `Origin` header as a hard failure, and some
  browsers (Safari) omit `Origin` on same-origin form POSTs. Fix: a `Referer` fallback (OWASP "verify
  Origin OR Referer"; `SameSite=Lax` still covers the cross-site threat), plus documenting + setting
  `ORION_RELAY_PUBLIC_ORIGIN` (`fly.toml [env]` + `docs/deployment.md`) so the check is deterministic
  behind the Fly proxy. **Verify live by commenting as admin, ideally on Safari.**

**Honest status:** the checklist features are live but **no project has a `tasks_file` configured yet**,
so nothing is actually being tracked. The first real candidate — an **applications tracker** — was
scoped this session and found to use rich `Status` fields + deadline tables (not checkboxes), so it
needs a status-aware collector rather than a reformat. Deferred to **E2 Inc 2.6** with a kickoff
written ([`docs/archive/applications-tracker-kickoff.md`](../docs/archive/applications-tracker-kickoff.md)); see the
Inc 2.6 entry in the E2 ladder. Also recorded there: the **Alex/Sam recipients are on-hold
discord/slack placeholders** (chat paused), and family supervision is now **via the dashboard**.

## Open questions / to settle before/while building

- **(Resolved, Phase 2, 2026-06-15) Push mechanism:** a **CLI command** (`orion intake <project>`, body via `--message` or stdin). No local HTTP endpoint — no network surface,
no server process, no auth token needed now. An endpoint (with a token) remains a
documented future option only if shelling out proves awkward from inside a session.
- **(Resolved by the above)** Intake authentication: not applicable while intake is a local
CLI command. Revisit only if/when a local endpoint is added.
- **(Resolved, Phase 2, 2026-06-15) Merge semantics:** **one body, titled `##` sections**,
in config (collector) order, empty sections skipped — one preview, one delivered message.
Only the git section is LLM-summarized; structured sections pass through verbatim and are
never re-sent to Claude. Lives in `merge.py` (a pure function), called by the orchestrator
before the final redaction pass.

## Future direction & guiding principles (noted 2026-06-13, deliberately not built yet)

These are intentions to keep the door open for, not work for the MVP. The whole point is to
build up bit by bit and avoid overengineering, so none of this changes the early phases.
They are recorded so that early choices do not quietly close these doors.

- **Open-source friendly / simple setup (a guiding principle, active now).** Orion should
eventually be public and usable by a stranger, so setup must stay accessible: minimal
dependencies, a single clear "install and run" path, and good defaults. This *reinforces*
choices already in the plan — stdlib `sqlite3`, `subprocess` for git, webhooks instead of
an always-on bot — which are all easy to stand up. Treat "could a new user run this in ten
minutes?" as a check on any future dependency or config decision.
- **Cross-platform: Windows, macOS, Linux (a guiding principle, active now — confirmed
2026-06-15).** Every change is made with cross-compatibility in mind, not one OS at a time.
The core is already mostly portable (stdlib, `pathlib`, a platform-safe timestamp); keep it
so. Where platforms diverge — scheduling (cron / `launchd` / Task Scheduler), git hooks —
delegate to the OS's native tool and document per-OS rather than embedding one platform's
mechanism. A dedicated **Phase 3.5 portability pass** (audit + fixes + the scheduling stance)
precedes Phase 4, because Phases 4–5 are the most platform-divergent features. See
`[docs/phase-3.5-kickoff.md](../docs/archive/phase-3.5-kickoff.md)`.
- **Modular signals and channels (a direction, not an MVP feature).** The signals (git,
Claude Code sessions, to-dos, notes) and channels (Discord, Slack) should be **optional
units a user turns on per project**, so one person can run to-dos + supervisor reporting
with no git or sessions, while another runs coding-only. The current two-lane design and
per-project config already lean this way. What we are *not* doing now: building a formal
plugin/registry system. That is premature abstraction this early. Near-term, "modular"
just means each collector and each channel is independently toggleable in config and does
not assume the others exist. A real plugin interface is only worth it once there are more
signals than we can hardcode cleanly.
- **Beyond one-supervisor-one-user (a later, architecturally significant goal).** Eventually
a supervisor may track several users, or one large project may have sub-sections owned by
different people and watched by multiple supervisors — i.e. cross-project / multi-party
collaboration. This is the consideration with the most weight, because it strains the
local-first, single-user assumption: multiple people's data has to meet in a shared place,
which is exactly what the future **web dashboard** (Phase 7b) would provide. We do not
build for it now, but we keep two things clean so it is not painful later: (1) the data
model names a project and its participants/recipients explicitly rather than assuming a
single implicit "me," and (2) the report and intake formats stay portable (a summary +
metadata blob), so they could later be sent to a shared service instead of straight to a
webhook. No multi-tenant machinery now — just avoid hardwiring "one user, one supervisor."
- **(2026-06-18) Newer recorded directions** — *scale-invariance* (no scale a second-class citizen),
the *light planning/tracking layer* (governed by *reframing, not originating*), and the *long-range
coordination/visibility-hub* vision are recorded under "Horizon-C direction settled" above and in
detail in `[docs/orion-strategy.md](../docs/orion-strategy.md)`. Same discipline: seams kept clean,
not built. **Update (2026-06-25):** the *dashboard-visibility* slice of that hub has since been
validated (the founding family-visibility intent) and moved to **building incrementally** — Horizon E2;
the rest (chat/E3, read-write/E5) stays seam-only. **Update (2026-06-26):** the *forward-state
planning layer (E1)* has also moved to **building incrementally as E2 Inc 3** — gate settled
*observe + remember, never originate* (see [`docs/e2-inc3-kickoff.md`](../docs/e2-inc3-kickoff.md)).

### Cross-platform & future-direction rationale (recorded Phase 3.5, 2026-06-15)

Reasoned through while settling the Phase 3.5 scheduling stance; recorded so the *why*
survives. **None of this is built yet** — it shapes future phases and the seams to protect.

- **Why Orion delegates scheduling to the OS (no built-in scheduler).** Orion is a one-shot
CLI; to fire at time T something must be alive at T, and Orion can't wake itself. Building a
scheduler means either a long-running daemon (which *still* needs the OS service manager to
survive reboot/sleep — so it adds a daemon *on top of* the OS layer, a bigger always-on
surface, the same reason webhooks beat an always-on bot) or an in-process scheduler library
(a dependency that only runs while the process runs). The OS schedulers (cron / launchd /
Task Scheduler) already own reboot-persistence, missed-run policy, and run-as-user. So Orion
delegates and documents per-OS. Accepted cost: per-OS setup divergence, no unified
`orion status`, per-OS missed-run semantics, and minimal-environment gotchas (stripped PATH,
no venv) — all documentation-shaped, cheaper than cross-platform service management.
- **When a built-in scheduling *layer* becomes right.** The test is whether cadence needs
Orion's own state. While cadence = "run a command at T," the OS tool wins. Once it needs
activity-gating ("only send if something changed"), backoff, quiet hours, per-recipient
cadence, or a unified next-run/last-error view, that logic must live in Orion — likely a
**hybrid** (OS provides the wake-up; Orion owns the decision). Because `orion report` is
already a clean non-interactive entry point, that shift is **additive — no rewrite**.
- **Bidirectional interaction (supervisors acting back) moves this.** "Destination → origin"
forces an always-on **listener** into existence (a bot/gateway connection or a public
inbound endpoint). Once that process exists, an in-process scheduler becomes nearly free —
so the listener and the "cadence needs state" moments likely arrive together (reinforcing
"don't build a scheduler before the process that would host it"). Bidirectional is also what
tips **local-first → hosted/hybrid** (a NAT'd, sleeping laptop is a poor inbound host); the
architecture then **splits** — collection stays local (it must read local files), delivery +
interaction move to a hosted relay — along the existing **portable report/intake blob seam**
(built for exactly this). New disciplines it brings: inbound = *untrusted input +
authorization* (the security story, until now purely outbound redaction, gains an inbound
validate/authorize side); state grows from an append-only log into a correlated conversation
(per-platform thread/message IDs); platform coupling deepens (receiving needs
platform-specific frameworks/signatures), which argues for the platform-neutral **dashboard
(Phase 7b)** as the primary interaction surface, with native-thread replies (7a) as a richer
add-on.
- **Multi-OS at once + multi-user × multi-supervisor.** Two distinct guarantees:
*portability* (Orion runs on each OS — the active principle) and *interoperability* (a
Windows producer and a macOS consumer agree on shared formats). The supervisor's OS is a
non-issue today — the chat platform abstracts it — and only matters for **artifacts that
cross machines** (the blob, future relay payloads), where the discipline is UTF-8 /
UTC-ISO-8601 timestamps / canonical `\n` and, the one invariant to state explicitly, **no
machine-local filesystem paths or locale-dependent formatting in any cross-machine
artifact** (the blob carries names and IDs, not paths). Many-to-many **converges on the same
hosted component** as bidirectional (multiple producers' data must meet in a shared place),
and promotes identity/addressing (a participant graph, not an implicit "me"), authorization
(who may see/act on which project), and routing → *subscriptions* to first-class concerns.
Seeds already in this plan: explicit participants, the portable blob, per-subscription
routing.

## Verification (per phase)

- MVP: in a test repo, make commits, run `orion report`, confirm the preview reflects the
diff, confirm the Discord webhook receives the message, confirm the state store advances
the last-reported commit so a second run reports "no new activity."
- Later phases: each new signal appears in the report; Slack receives the same; a scheduled
run fires from cron; a git hook triggers a report; session activity is summarized without
leaking file contents.
- Redaction: seed a repo with a fake API key and confirm it never appears in the preview or
the delivered message.


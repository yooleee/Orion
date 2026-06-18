# Phase C1 Kickoff — Web dashboard (read) + hosted/hybrid relay *(opens Horizon C)*

> **Read this, then [`plans/orion-plan.md`](../plans/orion-plan.md) before doing anything** —
> especially the **Horizon C roadmap rows**, the **"Horizon B → C boundary review"** section, the
> **"Future direction & guiding principles"** + **"Cross-platform & future-direction rationale"**
> blocks, and **KI-11**. C1 is **not** a normal incremental phase: it **opens Horizon C, the
> architectural pivot** (local-first → hosted/hybrid). Expect a real **plan-mode pass** with
> several genuinely-open decisions to settle with Yousuf *before* any code — this doc **surfaces**
> them, it does not pre-settle them.

> **Thoroughness mandate (Yousuf, 2026-06-17).** These decisions — above all **hosting**
> (decision 1) and the **open-source-simplicity stance** (decision 5) — are **foundational**: they
> shape the entire remainder of the project and carry **long-term lock-in**. Treat them with
> **extra rigor** in the plan-mode pass: weigh durable consequences (vendor coupling, the "anyone
> can run it" promise, stack consistency, where the user's data rests) over first-build
> convenience. **Do not default — justify.** The "Appendix — Async hosting research" at the end is
> decision-support prepared toward exactly this.

## Where things stand (as of 2026-06-17)

- **Horizon A** shipped. **Horizon B complete:** B1–B4, B6 signed off; **B5 deferred into Horizon
  C** at its gate. `pytest`: **172/172**. The B4 live verification (default Anthropic, local
  Ollama, fail-closed) passed end to end.
- Doc map: roadmap + design → [`plans/orion-plan.md`](../plans/orion-plan.md); shipped →
  [`CHANGELOG.md`](../CHANGELOG.md); open concerns → [`known-issues.md`](known-issues.md).

## What C1 is (and is not)

**Is:** the first **hosted** step. **Collection stays local** (it must read local git repos);
what becomes hosted is **presentation**. Local Orion, in addition to its existing channel
delivery, **pushes the portable report/intake blob** to a **hosted relay** that **stores** it and
serves a **read-only web dashboard**. The pivot is taken deliberately along the **portable blob
seam** that already exists, so the local-side change is small and additive.

**Is not:**
- **Not bidirectional** — no supervisor replies (that is **C2**; it forces an always-on listener
  and is where bots enter — see the boundary review). C1's only inbound is **blob ingest**.
- **Not multi-party** — single user's own projects; the participant graph / authorization is
  **C3** (KI-11).
- **Not a replacement of webhook delivery.** C1 **supplements**: Discord/Slack keep working
  unchanged; the dashboard is an *additional* surface. *(Decision, 2026-06-17.)*
- **No always-on local process / no inbound surface on the laptop.** The laptop only makes
  **outbound** pushes — local-first is preserved on the collection side.

> **Noted for later (not C1):** making the relay the **central delivery hub** — local Orion pushes
> *only* to the relay, which then fans out to Discord/Slack *and* the dashboard, i.e. one
> centralized place all messages/data flow through. Yousuf flagged this (2026-06-17) as a logical
> later direction; it is **out of scope for C1** (it reworks a working delivery path) but the
> blob/relay seam built in C1 should leave the door open to it.

## Seam readiness (verified in code, 2026-06-17)

C1 is genuinely **additive on the local side** — the heavy lifting is the *new hosted half*.

- **The blob is genuinely portable** (`src/orion/report.py:21-66`): `project`, `participants`,
  `share_level`, `lane`, `body`, `sections` (ordered `(title, body)` pairs), `generated_at`
  (**UTC ISO-8601**), `orion_version`. **No machine-local paths, no absolute repo paths.** Only
  `source_marker` is vestigial (always `""`; KI-8). This is the cross-machine contract C1 rests
  on, and it is real today.
- **Delivery is decoupled, pure transport** (`delivery/…send(payload, url)` + `_sender_for`
  dispatch in `cli.py`): a hosted-relay push is a **new target added additively**, reusing the
  established stdlib-`urllib` JSON-POST pattern (`delivery/discord.py`, `delivery/slack.py`), with
  **zero** changes to collectors / redaction / summarizer.
- **Intake and report share the same blob + path**, so one push serves both.
- **One design nuance:** the relay should ingest the **portable blob itself** (serialized JSON),
  *not* a Discord/Slack-composed payload — the dashboard renders its own presentation from the
  structured blob. So the new target is "serialize-and-POST-the-blob," slightly different from a
  chat channel sender.

## The hybrid shape (target picture for C1)

```
            ┌─────────── LOCAL (unchanged collection) ───────────┐
 git/etc ─▶ │ collect → redact → (summarize) → compose → deliver ├─▶ Discord / Slack  (as today)
            │                          └ build portable blob ─────┼─▶ POST blob ─┐
            └────────────────────────────────────────────────────┘              │  (new, outbound only)
                                                                                 ▼
                                          ┌──────────── HOSTED relay ────────────┐
                                          │ ingest endpoint (authenticated)      │
                                          │   → store (blobs + history)          │
                                          │   → read-only dashboard (access-gated)│
                                          └───────────────────────────────────────┘
```

## Open decisions to settle in the C1 plan-mode pass (surface w/ recommendation)

1. **Hosting target — *survey, decide in the plan-mode pass* (Yousuf's choice 2026-06-17).**
   Compare **Cloudflare** (Pages = dashboard, Workers = ingest API, D1 = SQLite-compatible store
   mirroring the local `report_history` shape) — the **leading candidate** given the stated
   Cloudflare preference and a clean fit — against alternatives (a small VPS, Fly.io, Vercel),
   weighed on cost, ops burden, and especially the **open-source-simplicity test** below.
2. **What the relay ingests + transport.** *Recommendation:* POST the **serialized portable
   blob** to a single authenticated ingest endpoint, via a new `relay` delivery target reusing the
   `urllib` POST pattern; authenticate with a **shared token** (in local `.env`, sent as a header).
3. **Hosted storage & data model.** Where pushed blobs live and the retention/history model.
   *Recommendation:* a hosted store **mirroring the local `report_history` shape** (project,
   summary/sections, recipients, `generated_at`); exact engine follows decision 1. Details in the
   plan-mode pass.
4. **Dashboard scope & access.** Read-only: list projects → report history → one report's
   sections/body + metadata. *Recommendation:* minimal read-only; **access-gated from day one**
   (even single-user — a login or access token), HTTPS only. Frontend stack (static SPA vs.
   server-rendered) follows decision 1.
5. **Open-source-simplicity tension (cross-cutting — the defining C1 constraint).** A hosted
   component is the first thing that seriously strains *"could a new user clone this and run it in
   ten minutes?"*. Settle **how the hosted half is offered**: optional (Orion still fully usable
   webhook-only with **no** hosted piece)? a one-click deploy template? self-hostable with a
   documented path? *Recommendation:* the hosted relay is **opt-in** — Orion's core stays
   local-only and dependency-light; the dashboard is an additive layer a user can choose to deploy.
6. **Framework/library justifications.** The hosted half introduces a new stack (Workers/JS or a
   server framework, a frontend framework, a hosted DB). Per project rules, **each must be named
   and justified** in the plan-mode pass against the simplest-thing-that-works bar — not adopted
   by default.

## Security / safety must-holds (non-negotiable; the permanent guarantees ride through the pivot)

- **Redaction is unchanged and remains the local guarantee.** The blob is already **twice-redacted
  before it leaves the machine**; hosting changes **where** redacted content rests, not **whether**
  it is redacted.
- **The hosted store is sensitive, not public.** It now **persists** redacted-but-still-sensitive
  project summaries (data-at-rest). The dashboard must be **access-controlled from day one** —
  even redacted activity is the user's, not world-readable.
- **New inbound surface = authenticate + validate.** The ingest endpoint is the first inbound
  Orion surface; it must **authenticate the pusher** (shared token) and **validate the payload**
  shape/version. This is the plan's anticipated "inbound gains a validate/authorize side," in its
  smallest form (ingest only; replies are C2).
- **Secrets discipline holds:** ingest token + any dashboard secret live in `.env` (local) and the
  host's secret store (hosted) — **never committed**.
- **Preview-before-send (channel side) and local-first collection are unchanged.**

## Seams / files likely involved (confirm in the plan-mode pass)

- **Local (small, additive):** a new `src/orion/delivery/relay.py` (serialize + POST the blob) +
  `_sender_for` registration; a config surface for the relay (`[relay]` table: endpoint URL + token
  env var), validated like existing tables; the relay push is **opt-in** per the simplicity
  decision.
- **Hosted (new, separate from the Python package** — likely its own top-level subdir, e.g.
  `web/` or `relay/`, with its own toolchain): ingest API, store, read-only dashboard. Stack ← D1.
- **Docs:** a new hosted-setup doc; README "Supported platforms / setup" update; promote the
  **portable-blob contract** to a documented, `orion_version`-stamped interface.
- **Tests:** the local relay-push target (unit); the hosted half's own tests (its stack).

## Appendix — Async hosting research (2026-06-17; decision-support for decisions 1 & 5)

> Prepared as async prep so the C1 plan-mode pass opens **ready to decide**, not ready to start
> researching. This is **decision-support, not a settled choice** — the final call is the
> plan-mode pass's, under the thoroughness mandate above. Platform facts are **mid-2026 and should
> be re-verified at decision time** (free tiers move).

**What the hosted half must do (requirements):**

1. A public **HTTPS ingest endpoint** that authenticates a token and accepts a JSON blob POST.
2. **Persistent storage** for blobs + history (small; mirrors local `report_history`).
3. A **read-only, access-gated dashboard** (mostly static content).
4. **Secrets** management (ingest token, dashboard access).
5. **Trivial cost** at hobby scale (one user, infrequent pushes).
6. **Self-hostable / open-source-friendly** — the dominant criterion (decision 5).

**The vendor-neutral invariant (recommend settling as PERMANENT, either path).** Local Orion
should know *only* "POST the portable blob (JSON) + a token to a configured URL," versioned by
`orion_version`. That single seam **decouples the local core from any hosting choice** — and even
lets the hosting choice change later without touching local Orion. Build this seam regardless of
which path wins.

**The central fork (the real decision — not "which vendor"):**

- **Path A — all-Cloudflare serverless.** Ingest = Workers, store = D1, dashboard = Pages.
  *Pro:* comfortably **free** at hobby scale, **zero ops**, matches the stated Cloudflare
  preference. *Con:* Workers' first-class language is **JS/TS** (Python Workers exist but remain
  limited), so the hosted half is a **stack divergence** from Orion's Python core, it **couples a
  foundational layer to one vendor**, and the user's (redacted) data rests on a managed platform.
- **Path B — portable self-hostable Python relay.** Ingest+store = a small **Python app + SQLite**
  on any container host (Render / Fly / a small VPS / a Raspberry Pi / the user's own box);
  dashboard = static, hostable anywhere (incl. Cloudflare Pages). *Pro:* **stack-consistent** with
  the core, **vendor-neutral**, genuinely **"anyone can run it,"** and the user's data stays on
  **their own infrastructure** — an extension of Orion's local-first/privacy ethos. *Con:* a small
  **ongoing cost** (no serverless-free equivalent) and **some ops** (a process to keep alive).
- **Path C — serverless + external DB** (e.g. Vercel + Neon). Frontend-framework-centric; more
  moving parts than our simple needs warrant. Noted for completeness, not recommended.

**Options scan (mid-2026; re-verify at decision time):**

| Option | Fits reqs | Cost @ hobby | Ops | Stack | OSS / self-host alignment |
| --- | --- | --- | --- | --- | --- |
| **Cloudflare** (Workers + D1 + Pages) | Yes | **Free** (Workers ~100K req/day; D1 5 GB / 5M reads/day; Pages) | **None** | JS/TS | Weak — vendor account + JS toolchain; data on managed platform |
| **Fly.io** (container + SQLite volume) | Yes | ~**$2/mo** always-on (+vol $0.08/GB, 10 GB free); **no free tier since 2024** | Low–med | any (Python) | Medium — portable container, but a vendor |
| **Vercel + Neon** | Yes (frontend-led) | Hobby free; DB via Neon free | Low | JS/TS + external DB | Weak–med — two providers |
| **Small VPS** (Hetzner/DO/Linode) | Yes | ~**€4–6/mo** | **High** (patching, TLS, uptime) | any (Python) | **Strong** — full control, vendor-neutral |
| **Self-host** (user's own box/Pi) | Yes | hardware only | user's | any (Python) | **Strongest** — data never leaves their infra |

**Open-source-simplicity analysis (the defining constraint, decision 5):**

- **Opt-in protects the core promise.** The relay is additive — Orion stays fully usable
  webhook-only with **no** hosted piece, so *"clone and run in 10 min"* is unchanged for anyone who
  doesn't want a dashboard. This is the single most important mitigation.
- For users who *do* want the dashboard, **Path B is the most "anyone can run it"**: one Python app
  (same ecosystem as the core) + a static dashboard, on the cheapest host they like — versus Path
  A's vendor account + JS toolchain onboarding.
- **Data residency echoes local-first.** Path B keeps even the *redacted* summaries on the user's
  own infrastructure; Path A places them on a managed vendor. For a project whose identity is
  local-first + privacy + redaction, that is a meaningful, on-brand difference.

**Recommendation (to confirm/scrutinize in the plan-mode pass):**

1. **Settle the vendor-neutral ingest contract as a permanent invariant** — do this no matter which
   path wins; it is the seam that makes the rest reversible.
2. **Lean Path B** (portable Python relay + SQLite; static dashboard on any host, which *can* still
   be Cloudflare Pages) as the better fit for Orion's **stated open-source, minimal-dep, privacy,
   and stack-consistency** values — accepting a small cost/ops in exchange for vendor neutrality on
   a foundational layer.
3. **Acknowledge the tension honestly:** Path A (all-Cloudflare) is the **lowest-friction, free,
   zero-ops** route for *Yousuf's own* instance and matches his stated preference — and the neutral
   contract supports it too. So a defensible alternative is "Path A for the personal instance now,
   Path B as the documented self-host story later." The plan-mode pass should weigh **long-term
   lock-in vs. first-build convenience** deliberately — exactly the kind of foundational call not
   to default on.

**Sources (mid-2026):**
[Cloudflare Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/) ·
[D1 limits](https://developers.cloudflare.com/d1/platform/limits/) ·
[Pages limits](https://developers.cloudflare.com/pages/platform/limits/) ·
[Fly.io pricing](https://fly.io/pricing/) ·
[Vercel storage](https://vercel.com/docs/storage) ·
[Vercel→Neon transition](https://neon.com/docs/guides/vercel-postgres-transition-guide).

## How to work this phase (project rules)

- **Plan-mode pass first** — settle decisions 1–6 with Yousuf (start with **hosting**, decision 1,
  since it gates 3/4/5); only then code.
- **Smallest reviewable units**, checkpoint after each. The natural split: (a) the local relay-push
  target + config, (b) the hosted ingest + store, (c) the read-only dashboard, (d) docs.
- **Cross-platform-minded** (the local push must stay portable; the hosted half is OS-neutral by
  nature). **Living docs** kept current. **Sign-off pattern** as before (implement → "awaiting
  sign-off" → a separate sign-off commit). Commit/push only when Yousuf asks.

## First commands next session

```bash
# 1. Confirm the baseline is still green (expect 172 passing).
.venv/bin/python -m pytest -q

# 2. Then start the plan-mode pass with decision 1 (hosting survey), which gates the rest.
```

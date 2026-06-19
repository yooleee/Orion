<!-- =========================================================================
phase-c2-bots-kickoff.md
---------------------------------------------------------------------------
Responsible for: Scoping the next Horizon-C slice — native Discord/Slack bots
                 (genuine two-way interaction in the chat surface, beyond the
                 dashboard comments C2 shipped). A SCOPING artifact, not the
                 build plan: the actual build gets its own plan-mode pass per
                 CLAUDE.md (plan before code, every phase).
Role in project: Output of the post-C2 horizon-planning juncture (2026-06-19).
                 Supersedes the archived, pre-C2 docs/archive/horizon-planning-
                 kickoff.md for the post-C2 state. See plans/orion-plan.md
                 ("Horizon-C next slice decided + E2E confirmed (2026-06-19)")
                 and docs/orion-strategy.md (S5).
========================================================================= -->

# Native Discord/Slack bots — next-slice kickoff (scoping)

> This decides **what's next and why**, and pre-scopes the open questions. It does **not** design the
> build — that is a dedicated plan-mode pass (kickoff → plan → code), per `CLAUDE.md`.

## Context & decision

C1 (deployed relay + dashboard, Path B on Fly) and C2 (dashboard comments + the `orion comments`
pull-back) are done and live-dogfooded. The post-C2 horizon-planning juncture (2026-06-19) chose the
**next slice: native Discord/Slack bots** — moving the *delivery surface* (where supervisors already
are) from one-way push into genuine two-way interaction.

**Why native bots, among the candidates {OSS-readiness polish, light planning/tracking layer, native
bots}:**

- It is the **natural progression from C2**: C2 made the loop two-way *on the dashboard*; bots make it
  two-way *in chat*, where the supervisor already reads the report — no context switch to a dashboard.
- It fits the recorded **surface-plural** long-range vision (chat = discussion; dashboard = structured
  overview; Orion = connective tissue), pulling that future one concrete step closer.
- The other candidates remain valid **later** slices (OSS-readiness polish for shareability; the
  derived planning/tracking layer for more meeting-replacing reports), but neither deepens the core
  loop the way two-way-in-chat does right now.

**The cost, named honestly (it's why bots were deferred before):** a bot is an **always-on process per
platform** with its own connection model and a standing **build-and-maintain burden**, and it is a new
**untrusted inbound surface**. This slice accepts that cost deliberately — and the design must keep it
as small as possible (see "smallest first slice").

## The C2 architectural learning that frames this

C2 was predicted (in the original roadmap) to force an always-on **listener**. It did **not** —
C2 shipped via the *already-deployed relay's* ingest + comment POST + a **pull** (`orion comments`
with a local watermark). So the "tips local-first → hosted" pressure was absorbed by the deployed
relay, and **no local listener was needed**.

Native bots **break** that: a bot must hold a live connection to receive events (Discord Gateway /
Slack Socket Mode or Events API). So **"how does the always-on bot relate to the existing pull/relay
model?"** is the first-order design question — does the bot *replace* the pull, *feed* the relay's
comment store (so the dashboard and `orion comments` still work unchanged), or run alongside?

## Open design questions (to settle in the build's plan-mode pass)

- **Which platform first?** Discord vs Slack — pick one for the smallest first slice (the project
  supports both for delivery; the bot side need not launch both at once).
- **Connection model:** Discord **Gateway** vs Slack **Socket Mode** vs Slack **Events API (HTTP)** —
  each has different always-on / inbound-endpoint / hosting implications.
- **Listener ↔ pull/relay relationship** (per the learning above): where do inbound replies land —
  the existing relay `report_comments` store (so `orion comments` + the dashboard keep working), a new
  path, or both? Prefer reusing the C2 comment store if it fits.
- **Inbound security** (non-negotiable, S4): a bot is a new untrusted inbound surface — apply the C2
  discipline (authenticate/validate the event source; verify platform signatures; never trust payload
  shape; redaction stays an *outbound* control for the developer's own secrets). Define the threat
  model before code.
- **Hosting the bot process:** where the always-on process runs (alongside the Fly relay? a separate
  worker?) and how secrets/tokens are managed (gitignored `.env`, per the permanent rule).
- **Smallest first slice:** e.g. **one platform**, inbound replies routed into the **existing comment
  store**, **no slash commands / interactive components yet** — prove the two-way loop end to end
  before adding surface area. Structure (if any) comes from slash commands / interactive components,
  **not** parsed from free text (recorded constraint).
- **Dependency check:** a bot likely needs a real library (e.g. `discord.py` / Slack Bolt) — the first
  new runtime dep beyond the current minimal set, so justify it against the open-source-simplicity bar
  in the build's plan-mode pass (per the framework-recommendation rule).

## Dogfood as a refinement input (NOT a gate)

The 6/20–21 hackathon dogfood **refines** this slice's scope and priority; it does **not** decide
whether to build it (the direction is decided). It tests the *current* features; the realistic outcome
is expand/refine, not remove. Capture during/after the weekend, then feed it into the build's plan-mode
pass:

- Did I **reach for Orion** over the lightweight alternatives (`TODO.md` / `git log` / hand-written
  standups)? When, and why not when not?
- What felt **missing, annoying, or slow**?
- Did I ever want a supervisor to **see** or **reply to** something in the moment? (Directly informs
  the bots' value + smallest-first-slice.)
- **Where was the value — the delivery (Discord/Slack), the dashboard, or the session skill?** This
  most directly tunes how much to invest in the chat surface vs. elsewhere.

## Demand-gated / out of scope (record; do not pull in)

- **C3-proper multi-party identity** (authenticated per-person identity / participant graph / per-
  supervisor routing — KI-17, KI-11, KI-1, per-recipient delivery state). Committed only on real
  multi-party demand, separate from this slice. (Native bots may *surface* identity questions; the
  self-entered author remains the lightweight stand-in until C3.)
- **E2E encryption / managed hosting** — stays a documented bridge, not a committed goal (Path B
  self-host + plaintext holds). See `plans/orion-plan.md` hosting section.
- **Slash commands / interactive components / rich structure** — beyond the smallest first slice.

---

*Supersedes `docs/archive/horizon-planning-kickoff.md` (the pre-C2 juncture) for the post-C2 state.*

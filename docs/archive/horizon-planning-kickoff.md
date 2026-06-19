# Horizon-planning kickoff — the post-C1 direction-setting juncture

> **⚠ SUPERSEDED (2026-06-18) — archived record.** This pass has been executed; its outcomes live
> in [`docs/orion-strategy.md`](../orion-strategy.md) (north-star, differentiators, deferred
> directions) and the updated roadmap in [`plans/orion-plan.md`](../../plans/orion-plan.md). One
> correction to this doc's framing: it overstated the hackathon dogfood (6/20–21) as the *decisive
> input*. The hackathon is a **readiness test, not a driver** — "is Orion good enough that I reach
> for it?" — and was not treated as dictating direction. Kept as the as-launched record.

> **Read FIRST:** [`plans/orion-plan.md`](../../plans/orion-plan.md) — the **Horizon C roadmap rows**,
> the **"Hosting decision (settled 2026-06-18)"** section, the **"Phase C1 status"** + **"Horizon
> B → C boundary review"**, and the **"Future direction & guiding principles"** block — and
> [`docs/known-issues.md`](../known-issues.md) (KI-1, KI-8, KI-11, KI-13, KI-16, KI-17). This is **not
> a normal phase kickoff**: it is the deliberate **direction-setting pass before any new
> Horizon-C build**, the one Yousuf flagged (saved as the `plan-direction-before-building` working
> preference). Treat the foundational calls here with **extra rigor** (long-term lock-in): **do not
> default — justify**.

## Why this exists / what it is

Everything through **C1 (first slice)** + the **post-C1 OSS-readiness pass** (CI, contributor docs,
onboarding friction) is shipped and green (`pytest` 228; CI green across {Linux, macOS, Windows} ×
{3.11–3.13}). Per the project's discipline — *plan before code; build seams, not futures* — the
**next substantial work (C2 onward) needs a deliberate direction call before building**, not
tactical continuation. This doc tees up that pass so it opens ready, with the threads and open
questions assembled.

## Key input: the hackathon dogfood (2026-06-20/21)

Run this pass **informed by the weekend dogfood read.** Orion's readiness is judged **empirically
by real use vs everyday lightweight methods** (hand-written standups, `TODO.md`, scrolling
`git log`), not by checklist. Worth capturing during/after the hackathon:

- Did you actually **reach for Orion**, or the lightweight alternatives? When, and why?
- What felt **missing, annoying, or slow**?
- Did you ever want a **supervisor to SEE** something (→ the hosted-dashboard value question)?
- Where was the value — the **delivery** (Discord/Slack), the **dashboard**, or the **session skill**?

This read directly shapes the "what's the goal / what to build next" calls below.

## The threads converging here (all already on record)

- **C2 — bidirectional / replies:** supervisors comment back; forces an **always-on listener**; the
  point where **bots** enter. (Roadmap C2 row; the B→C boundary review.)
- **C3 — multi-party / identity / authorization:** a participant graph (not an implicit "me"),
  per-supervisor per-project/task routing ([[orion-routing-future-per-subscription]] memory),
  access control. C3 has become the **home for several deferred threads**: the **E2E-encryption
  bridge** (managed hosting without data-residency loss; see the Hosting-decision section),
  **KI-17** (submitter accountability), **KI-1's** per-recipient delivery state, and **KI-11**
  (recipient-as-destination vs person / dedupe).
- **Hosted deployment (Path B, settled):** the actual deploy *beyond loopback* is its own build
  (a possible "C1 second slice"). E2E is the documented bridge to a *managed* option later.
- **B5 scheduling layer (KI-13):** deferred into Horizon C; likely arrives **with** C2's always-on
  process (an in-process scheduler is nearly free once a listener exists).

## Open strategic questions to settle (the heart of the pass)

1. **North star — what is Orion FOR, and for whom?** A personal dogfood tool? A small OSS tool
   others self-host? A multi-party product? This frames all sequencing. *(Inform with the dogfood
   read.)*
2. **Sequencing — what's actually next?** Options: (a) **deploy** the C1 relay/dashboard beyond
   loopback (hosted Path B); (b) **C2** bidirectional; (c) **C3** multi-party foundations. The
   plan's dependency-order is C1 → C2 → C3, but a real hosted deployment of C1 may slot first.
   Decide deliberately, not by default.
3. **Does the dogfood change priorities?** E.g. if the dashboard isn't valued but Discord/Slack
   delivery is, deprioritize the hosted dashboard; if "a supervisor wanting to see/reply" is the
   real pull, accelerate C2.
4. **E2E-encryption commitment:** adopt managed-hosting-with-E2E as a real goal/horizon, or stay
   self-host-plaintext (Path B) for the foreseeable future? (Shapes C3 and any future Cloudflare
   path.)
5. **C2's inbound surface (if it's next):** the first always-on listener + reply ingestion is the
   biggest architectural + security shift since C1's ingest. What is the **smallest first slice**?

## How to run the pass

- **Plan-mode, foundational rigor** (the thoroughness mandate): weigh long-term lock-in over
  first-build convenience; surface each decision with a recommendation; don't default.
- **Likely outputs:** a settled north-star framing; a **sequenced Horizon-C plan** (the next slice
  + why); decisions on E2E + deployment timing; an updated `plans/orion-plan.md` roadmap and a
  concrete next-phase kickoff.

## First steps next session

1. **Confirm baseline:** `pytest` 228 green, CI green on `main`, tree clean.
2. **Debrief the hackathon dogfood** — the key empirical input.
3. Open **Q1 (north star)** then **Q2 (sequencing)** — they gate the rest.

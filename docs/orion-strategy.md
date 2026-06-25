<!-- =========================================================================
orion-strategy.md
---------------------------------------------------------------------------
Responsible for: The one-page STRATEGY overlay for Orion — an explicit statement
                 of what success looks like (Objective / Goals / Strategies /
                 Measures), crowning the execution roadmap.
Role in project: A thought-process overlay, NOT a framework or a replacement.
                 `plans/orion-plan.md` stays the execution layer (horizons /
                 phases); this doc names the Objective those phases serve and the
                 Goals that tell us whether they worked. The horizon phases ARE
                 this doc's "Actions." Born from the post-C1 direction-setting
                 pass (2026-06-18); see that pass's outcome in orion-plan.md.
Companion: OGSM (Objective/Goals/Strategies/Measures) is carried as a recurring
           success-articulation lens; Cagan's Product Operating Model is carried
           as a solo-scaled mindset — both as thought processes, never installed
           as frameworks.
========================================================================= -->

# Orion — Strategy (one page)

> **How to read this.** This is a *thought-process overlay*, not a framework retrofit. It does
> not replace the discipline in [`plans/orion-plan.md`](../plans/orion-plan.md) (build seams not
> futures, plan before build, security-first) — it *crowns* it with the one thing the roadmap
> didn't state explicitly: **what success looks like and how we'd know.** The horizon phases in
> the plan are this page's **Actions**.

## Objective (the qualitative north star)

An **excellent, open-source, local-first tool** that turns a **solo developer's** real project
activity into progress updates a **supervisor genuinely values** — runnable by a stranger in
minutes, and a **showcase of disciplined product engineering**.

**Scale-invariant by aspiration: no scale is a second-class citizen.** Orion should be as
first-class for an **independent developer** as for a **team of developers + supervisors** — the
solo user never pays a team tool's complexity tax; the team never hits a solo tool's ceiling. The
engineering form of this aspiration: **every multi-party feature is zero-cost and invisible at one
participant.** Multi-party *machinery* isn't built now (a clean, additive seam); scale-invariance is
the **destination those seams serve** — the current build focus stays solo → supervisor.

## What distinguishes Orion (differentiators vs. enablers)

No single pillar is unique (self-hosted PM tools and git digesters exist) — the distinctiveness is
the **coherent combination**, held with discipline, and **earned by the architecture** rather than
bolted on as positioning.

| Differentiator (earned) | Enabler / quality bar (table-stakes) |
| ----------------------- | ------------------------------------ |
| **Data sovereignty** — own-your-data, local-first, independence (trust by ownership *or* by cryptography) | Ease of use (enables the solo end of scale-invariance) |
| **Derives from existing work** — reframes what you already produce; no data re-entry (*reframing, not originating*) | Redaction + preview-before-send (a trust property, not a differentiator alone) |
| **Agentic-execution-native** — sits alongside Claude Code; already ingests session summaries | |
| **Scale-invariant** — first-class from N=1 upward | |

## Goals (honest and behavioral — no vanity KPIs)

The measures of "excellent." All are qualitative/behavioral on purpose: with no user base yet,
invented quantitative KPIs (downloads, MAU) would be vanity metrics and premature optimization.

| # | Goal | How we'd know |
| - | ---- | ------------- |
| G1 — **Utility** | I reach for Orion over the lightweight alternatives (standup notes / `TODO.md` / `git log`) for **most** of my progress updates. | The dogfood read — did I actually reach for it, and when not, why? |
| G2 — **Outcome for the supervisor (informed *and* less overhead)** | An Orion report either (a) tells a **real supervisor** something they'd otherwise have missed, **and/or** (b) keeps them informed **asynchronously** so a status check-in/meeting is avoided — or freed up for a more important topic. | Recipient confirmation that a report surfaced something new *or* stood in for a sync they'd otherwise have needed (not "did it send" — did it *inform* and *save coordination time*). |
| G3 — **Setup simplicity** | A new user clones, configures, and sends their first report in **≤10 minutes**. | Re-run the 10-minute test on any onboarding-friction change. |
| G4 — **Showcase** | The code + planning artifacts stand as **portfolio-grade** evidence of disciplined product engineering. | Security-first, seams-not-futures, cross-platform, legible docs — judged on inspection. |

## Strategies (the key choices — already made, now stated)

The 3–5 decisions that define the route — and, by implication, what we're **not** doing now.

- **S1 — Solo-dev → supervisor focus.** Make that one loop genuinely great; multi-party (C3) stays
  a seam.
- **S2 — Local-first now, hosted/hybrid via the seam.** Collection stays local; delivery/presentation
  move hosted along the portable report/intake blob as the project grows. Security rides through
  unchanged.
- **S3 — Conditional, provider-agnostic, lightest-adequate LLM.** Summarize raw activity by default;
  never force-route structured/already-written content through a model.
- **S4 — Security & privacy are permanent invariants.** Redact + preview-before-send + fail-closed,
  gaining an inbound validate/authorize side as interaction goes two-way. (The one non-negotiable;
  everything else above is stage-appropriate and expected to evolve.)
- **S5 — Two-way (C2) shipped; next is OSS-readiness, then the local enhancements.** C2 is done —
  dashboard comments + the `orion comments` pull-back, plus a native Slack bot. The pre-dogfood plan
  was to deepen the chat surface next, but the **6/20–21 dogfood re-sequenced it**: onboarding
  friction was the binding constraint, so **Horizon D leads with OSS-readiness** (`orion add-project`
  shipped; `orion status` + setup polish next), then the reconciled local enhancements (incubator
  signal + lightweight audience-typed routing). **Native Slack/Discord bot deepening is a demand-gated
  chat-surface track** — a parallel surface to the dashboard, with channel features and a build/maintain
  cost (Horizon E), not the immediate next. **C3 (multi-party) is now being built incrementally** (a
  2026-06-25 re-sequencing): Increment 1 — dashboard-integrated identity and access (per-user login keys,
  roles, per-project scope, sessions, the `relay-user` admin CLI) — is built and in review, driven by real
  sharing/collaboration/showcase needs rather than a far-off product leap. The broader C3 product layer
  (subscriptions, routing) and E2E stay deferred behind the same seams.
  The **discrete go-public push** (license, personal-reference scrub, public CI, final sweep) is
  consolidated in the **decision-gated Horizon P (Publish / OSS-launch)** — distinct from Horizon D's
  OSS-readiness *polish*, and triggered by the publish decision rather than by dependency.
  *(Re-sequenced 2026-06-24 on the dogfood; see `plans/orion-plan.md` Horizons D–E and P.)*

## Measures (watch + act)

- **Dashboard (what we watch):** the dogfood read (G1) · the 10-minute setup test on any onboarding
  change (G3) · the supervisor-value signal (G2). Qualitative by design.
- **Actions (what we do):** the **horizon phases** in `plans/orion-plan.md`. Done = the
  deploy-beyond-loopback relay + dashboard hardening (C1) and the C2 bidirectional pass (dashboard
  comments + comment pull-back) and the native Slack bot; next = **OSS-readiness (Horizon D)** —
  `orion status` + setup polish (re-sequenced on the 2026-06-24 dogfood review; see `plans/orion-plan.md`).

## The lens we carry (thought processes, not installed frameworks)

- **OGSM — a recurring success-articulation step.** For any new direction, state the *Objective* it
  serves, the measurable *Goal*, the *Strategy*-choice it embodies, and the *Measure*. It slots into
  the existing "plan before building" pass; it is not a document to file and forget.
- **Cagan's Product Operating Model — a solo-scaled mindset.** Not an org transformation (there's no
  team to empower) but a guard against becoming an **order-taker to your own roadmap**. Three
  principles, carried as a habit of thought:
  - **Outcomes over output** — judge work by the outcome (an informed supervisor; a reached-for
    tool), not by phases shipped.
  - **Discovery before delivery** — before each horizon, weigh *value / usability / feasibility /
    viability*; chiefly, "will this actually be used?"
  - **Focus = saying no** — each new capability is also a decision about what *not* to build now
    (why C3 and E2E stay deferred).
- **Parallelization & coupling analysis — a recurring lens for brainstorming/analysis.** For any
  examination, direction-weighing, or multi-piece scoping, map what could proceed *in parallel* vs.
  what is *intertwined*. It serves three ends: **efficiency** (independent tracks can fan out to
  agents), **architectural understanding** (a live coupling-vs-separability map), and **verification**
  (coupling surfaces hidden dependencies and risk). A thought process carried into the "plan before
  building" pass, not a framework. The living map is `[docs/parallelization.md](parallelization.md)`,
  updated as the analysis shifts with the work.

## Direction under consideration (recorded, deferred — seams kept clean)

> **Governing principle — Orion reframes, it doesn't originate.** The planning layer is *derived*
> (as a report is): it **reorganizes / reframes / assesses** what the project has already produced
> (git, sessions, todos, notes), grounded entirely on the existing record as source of truth. Novel
> directions originate *outside* Orion (you · a chatbot · a Claude Code session); Orion grounds and
> reorganizes them — it does **not** invent them. So there is **no double-entry**: Orion stays
> *downstream* even for planning, which is exactly what preserves the "derives-from-existing-work"
> differentiator at every scale.

- **From reporter toward a *light planning/tracking layer*.** Today the to-do/milestone leg is
  **retrospective only** — the `tasks` collector reports what got checked off. The intended evolution:
  Orion gains a *light planning layer it tracks* — milestones, sprint/section grouping, due dates,
  progress-over-time, "at-risk"/cadence nudges — **short of a full task manager** (not Linear/Jira; the
  user still owns their task files). This **serves** the reporting north-star, it doesn't fork from it:
  forward-looking status ("milestone X on track for Friday, 1 blocked") makes reports far more
  meeting-replacing (**G2**).
- **Why deferred + where it lands.** It needs Orion to hold *forward-looking state of its own* — exactly
  the "cadence needs Orion's own state" threshold flagged for the deferred **scheduling layer
  (B5 / KI-13)**. So it most naturally arrives **with Horizon C's stateful, always-on process**, not
  before. Seams that keep it additive: the portable blob accepts extra fields without rejecting them
  (forward-looking metadata slots in), and the SQLite store can gain planning-state tables additively.
- **Discipline (focus = saying no):** keep it a *light, tracked* layer in service of better reports;
  resist drift into full task/calendar/sprint management. Decide the exact scope via discovery when it
  nears (would I actually use the planning side?).

## Long-range vision (Horizon E — aspirational, unvalidated; seams kept clean, *not* built)

> Food for thought, explicitly **not** on the active roadmap — recorded only so early choices don't
> quietly close the door. The seams it leans on (the portable blob, explicitly-named participants,
> swappable delivery transport, the multi-project registry) are the ones already protected. Its
> governing constraint is the same **reframing-not-originating** principle above.

- **Orion as a *coordination / visibility* layer — not an execution platform.** A place to discuss
  features/directions, reorganize work into milestones/sprints, and take multi-role input (supervisor,
  developer, …) — *upstream of and complementary to* agentic execution. It does **not** replace Claude
  Code; the model is *chatbot (ideate) → Orion (coordinate/track) → Claude Code (execute) → results
  flow back*. Already seeded by the `orion-session` ingest skill (Orion sits downstream of Claude-Code
  execution today).
- **Surface-plural, not dashboard-centric.** Coordination should also live **natively in Slack/Discord**
  (where people already are), with the **dashboard** as the structured, persistent, cross-project
  *visibility* surface chat can't provide. Division of labor: chat = discussion; dashboard = structured
  overview; Orion = the connective tissue + memory. *Costs:* native two-way needs **bots per platform**
  (the C2 build-and-maintain burden); structure is captured via slash commands / interactive
  components, **not** parsed from free text; multi-surface identity is a **C3-amplified** problem.
- **Multi-project / cross-project.** Working across projects in parallel (the registry already holds
  many), plus *some* cross-project collaboration discussion where projects share or integrate features
  (not merged into one — collaborating).
- **The inflection point to watch — read-only → read-*write* dashboard.** Everything through C3 keeps
  the invariant *collection stays local; Orion reads / redacts / reports; the dashboard presents.* A
  surface where work is *created/edited* is a different architecture (write paths, auth,
  hosting-as-primary). The surface-plural framing softens this: much of the *writing* can live in chat,
  leaving the dashboard as structured visibility.

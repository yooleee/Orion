<!-- =========================================================================
feature-ideas-reconciliation.md
---------------------------------------------------------------------------
Responsible for: A WORKING / STAGING doc that (1) captures the 6/20–21 hackathon
                 dogfood findings and (2) reconciles the seven meta-layer feature
                 ideas (docs/feature-ideas-meta-session.md) against the roadmap
                 (plans/orion-plan.md) — honest verdict per idea, a focus-test of
                 the strongest, and proposed roadmap placements.
Role in project: The detailed reconciliation record. Its outcomes were folded into
                 plans/orion-plan.md (the dated note "Meta-layer feature ideas
                 reconciled + dogfood captured", 2026-06-23) the same day; this doc
                 holds the full per-idea verdicts and the config-write analysis that
                 the roadmap note summarizes. docs/feature-ideas-meta-session.md
                 (the parked ideas) points here.
Status: Reconciled into the roadmap 2026-06-23. Candidates to place, not work to
        start. No code.
========================================================================= -->

# Meta-layer feature ideas — reconciliation + dogfood capture

> Working doc. Source inputs: `docs/feature-ideas-meta-session.md` (the parked ideas),
> `docs/archive/post-dogfood-kickoff.md` and `docs/archive/phase-c2-bots-kickoff.md` (the scoping/questions),
> and `plans/orion-plan.md` (the roadmap / source of truth). Line refs below are to
> `plans/orion-plan.md` as of 2026-06-23.

---

## 1. Dogfood capture (6/20–21 hackathon)

First real dogfood: Orion used on the `sar_hackathon` project over the weekend. Answers to the
four kickoff questions (`docs/archive/post-dogfood-kickoff.md`):

- **Did I reach for Orion?** Yes — for progress reporting on the hackathon project. Not as a
  to-do list (no `TODO.md` existed for it). A standout positive: the **`orion-session` skill
  worked from inside `sar_hackathon` with no extra setup** — unexpected, and a good signal that
  the skill path is portable across projects.

- **What felt missing / annoying / slow?** **Setup/onboarding friction — the #1 finding.** I
  could not just run `orion project sar_hackathon` from the hackathon directory. I had to register
  the project from the Orion-repo side and import it, and ended up using Claude Code *inside the
  Orion repo* to do the setup. The terminal commands would likely have worked, but the flow was
  too tedious. The crux is an **asymmetry**: the session skill "just works" from any project
  directory, while the CLI requires per-project registration on the Orion side. Understanding and
  closing that gap is the real lesson. (See idea #4 and section 3 — this is the same knot as the
  config-write rule.)

- **Did I want a supervisor to see/reply in the moment?** The value loop ran through the
  **dashboard comments + `orion comments` pull**, and it worked smoothly: I gave view credentials
  to my dad, he read the reports (he valued the technical *and* non-technical summaries), left
  comments, and after each report I had Claude Code pull the comments back. He didn't critique the
  dashboard design — he understands it's early and intentionally simple. **He did request a real
  login/auth system**, because he occasionally had to re-enter the view credentials instead of
  staying logged in. This is the known "lazy" Basic-Auth placeholder; the ask now has a concrete
  pull behind it (see idea #5). **The native Slack bot / `slack-bolt` was never exercised** — so
  there is outstanding verification debt on the last-built slice (this is fine, just unfinished).

- **Where was the value — delivery, dashboard, or session skill?** **All three.** The session
  skill produced the summaries; the dashboard let me reference past reports and let my dad read
  them; and the comment loop made delivery genuinely two-way. Overall: **positive, with expected
  limitations** — nothing surprising or alarming.

- **Next-slice implication (record, do not act on here):** the dogfood points at **OSS-readiness /
  onboarding polish** as the most valuable next direction, *not* bot-deepening — the bot's
  reply-in-chat value is unproven (untested), not disproven. This **reverses the default lean**
  listed in `docs/archive/post-dogfood-kickoff.md`. The actual next-slice decision is a separate session.

---

## 2. Reconciliation verdicts

Each idea from `docs/feature-ideas-meta-session.md`, with an honest verdict (already planned /
adjacent / genuinely new), the roadmap evidence, and a confidence note.

### #1 — Incubator as a fifth signal
**Verdict: genuinely new signal, but lands on a planned *principle*. (Confidence: high.)**
The roadmap locks in exactly four signals (git, sessions, tasks, notes) and has **no** plugin/
registry system — that's deliberately deferred as premature abstraction. But "modular signals" is
an explicitly recorded *direction*: signals should be "optional units a user turns on per project,"
with a real plugin interface worth it only "once there are more signals than we can hardcode
cleanly" (L921–929). An incubator/idea-pipeline collector would be the **first real test** of that
principle. Mechanically it's additive (a new collector + orchestrator wiring, the way tasks/notes
were added), not a rewrite.

### #2 — Audience-typed routing (signal types → audience types)
**Verdict: adjacent — this is the C3 routing future made concrete. (Confidence: high.)**
Today's routing is **channel-only** (same content per recipient, routed by channel; L350–351).
Per-supervisor / per-subscription routing is the explicit **C3** deferred goal — "a participant
graph (not an implicit 'me'), per-supervisor per-project/task/todo subscriptions (the routing
future)" (L71), entangled with KI-11 (a recipient is a destination, not a person), KI-17 (no
author identity), and KI-1. The seam is already held open: "named recipients / participant model
— recipients are named destinations today; the per-supervisor participant graph (C3) is additive
on top." Audience-typed routing is a specific flavor of that. (See the focus-test for the question
that decides *how early* it can land.)

### #3 — Portfolio-aware `report --all`
**Verdict: adjacent — fold into the deferred `--due` filter, not standalone. (Confidence: high.)**
`--all` is shipped (A4). Filtering it by project-status metadata (active/parked/archived) is a
small addition to the already-deferred cadence-aware **`--due` filter (KI-13)**, which the plan
says "`report --all` is the layering point" for. Not its own feature.

### #4 — `graduate-idea` registers a project + emits an intake event
**Verdict: kept whole; it motivates revisiting the config-write rule. (Confidence: high.)**
The intake path exists (Phase 2: `orion intake <project>`), so the "emit a started-project event"
half is feasible today. The "auto-register the new repo in `orion.toml`" half runs into a *settled
invariant*: Orion never writes config (`config set` was deliberately excluded; README: "Orion
never *writes* the config — you change it by editing `orion.toml`"). Rather than halve the idea,
**reconsider the rule** — see section 3. This is the strongest practical idea for fixing the
weekend's onboarding friction.

### #5 — Dashboard as the shareable meta-layer surface
**Verdict: adjacent — already-deferred dashboard expansion; auth has a new pull. (Confidence: high.)**
The read-only dashboard is shipped (C1, deployed on Fly, Path B). Hosting the **portfolio map +
idea pipeline** on it is the already-recorded **deferred dashboard-expansion** + **light planning/
tracking layer** (L821–828) and the long-range surface-plural vision. New this session: the
**dashboard auth sub-piece has a concrete external pull** (dad's login request). Real multi-user
auth sits under C3 multi-party identity (deferred); a simpler "stay-logged-in" session improvement
on the existing Basic-Auth may be separable and cheaper — worth scoping when the dashboard is next
touched.

### #6 — `orion status` backlog view (what's unreported across projects)
**Verdict: genuinely new, cheap, low-risk. (Confidence: high.)**
The data already exists — last-reported state per project is derivable from the `report_history`
table (`SELECT MAX(sent_at) ... WHERE project = ?`), no new schema. The plan even names the gap
("no unified `orion status`", L960) as an accepted cost of OS-delegated scheduling. A read-only
pre-report digest is additive and stands alone. Good **OSS-readiness / quality-of-life** candidate.

### #7 — Summaries inherit the global Writing & Documentation Style
**Verdict: mostly already done — do NOT build an inheritance mechanism. (Confidence: high.)**
The lean, outcome-focused, no-preamble style is already encoded in `summarize.py`'s system prompt,
and the `session-openers-lean-directional` memory note already records the "no padded metric
recaps" preference. Formalizing a doc-driven "inheritance" mechanism would be over-engineering
(the global style is *Claude's* authoring guidance, not something Orion's code should import). Keep
as a one-line prompt-tuning todo at most: sanity-check the summarizer prompt against the style.

---

## 3. The config-write rule — reconsider, don't remove (revised #4)

> **DONE (2026-06-23): shipped as `orion add-project`.** The middle-ground below was implemented —
> an explicit, preview-gated, append-only writer (creates a minimal config when absent, infers
> name/repo from cwd, recipients via `--like`/`--recipient`). See the dated note "Config-write
> invariant refined" in `plans/orion-plan.md`. The original analysis is kept below for the record.

The current invariant: **Orion never writes `orion.toml`** (human-edited, read-only via stdlib
`tomllib`; a `config set` command was deliberately excluded). The weekend dogfood showed this is
*also* the root of the #1 onboarding friction — there is no register-a-project path, so adding
`sar_hackathon` was tedious. So idea #4 and the dogfood point at the same knot. Proposed
middle-ground (to settle properly in a dedicated plan-mode pass, since it touches a hard
constraint):

- **Keep the spirit (the part worth preserving):** *no silent config mutation as a side effect of
  a `report` / `intake` / `collect` run.* Predictability and a human-owned config stay intact.
- **Relax the letter:** allow a **deliberate, explicit, user-invoked scaffolding command** — the
  entry point `graduate-idea` would call, e.g. `orion add-project <path> [--name ...]` — to write
  config. The intent is categorically different from a silent `config set`: the human is *asking*
  Orion to set up a project. (Preview-before-write keeps the human gate, mirroring preview-before-
  send.)
- **Safe implementation seam:** **append** a new `[[projects]]` / `[projects.X]` stanza rather than
  rewriting the file. Appending never touches existing content or comments, and stays
  **dependency-free** — stdlib `tomllib` is read-only, but emitting a known-shape stanza needs no
  TOML *writer* (no `tomli-w` / `tomlkit`). Optionally scaffold with commented defaults for the
  human to review before first use.
- **Why it matters:** this directly fixes the onboarding pain *and* keeps idea #4 whole, instead of
  dropping its register-the-project half. It also serves the "clone and run in 10 minutes" /
  OSS-readiness bar.
- **Open sub-questions for the dedicated pass:** does the command live in Orion (`orion add-project`)
  with `graduate-idea` shelling out to it, or does the skill write the stanza directly? How does it
  interact with the CLI-vs-skill asymmetry (could the same mechanism give the CLI the skill's
  "works from any directory" feel)? What's the minimal validation (path exists, name unique)?

---

## 4. Focus-test + proposed placements

### Focus-test (the strongest candidates)
The parking doc guessed #1 (incubator signal) and #2 (audience-typed routing) as strongest. They
are — but **the real finding is they're a complementary pair, and #2 enables #1.** An incubator
signal is valuable *because* its updates target a **different audience** than git progress (the
exact pattern from the weekend: a supervisor wants commits/tasks; family/mentors want the
idea-and-design discussion). Without audience-typed routing, a fifth signal just dumps idea updates
into the same supervisor report — not what's wanted. So #2 is the enabler that makes #1 worth
building.

**The placement-deciding open question** (to settle in a later plan-mode pass, not now): can a
*lightweight* audience-typed routing ship onto **today's named recipients** — tag each recipient
with which signal types it receives — **without** the full C3 participant graph (authenticated
identity, dedupe-by-person)?
- **If yes:** the #1+#2 pair is an **earlier, additive slice** landing on the seam the roadmap
  already protects — well before the C3 multi-party leap.
- **If it genuinely needs identity:** it waits for C3.

### Proposed placements (candidates, not committed work)
| Idea | Placement |
|------|-----------|
| #1 + #2 (pair) | **Strongest.** Candidate Horizon-C slice "lightweight audience-typed routing (+ incubator signal)", gated on the seam question above. |
| #3 | Fold into the deferred `--due` / KI-13 scheduling-layer note. |
| #4 | Config-write reconsideration (section 3); tie to onboarding-friction fix. Its own plan-mode pass. |
| #5 | Fold into the deferred dashboard-expansion; flag the auth sub-piece (external pull); scope a cheaper "stay-logged-in" improvement separately. |
| #6 | OSS-readiness / quality-of-life candidate (cheap, standalone). |
| #7 | Already-substantially-done; prompt-tuning todo only. |

---

## Follow-ups (do NOT do now — recorded so they aren't lost)

- **Proper reconciling — DONE (2026-06-23).** Outcomes folded into `plans/orion-plan.md` (the dated
  note "Meta-layer feature ideas reconciled + dogfood captured"), and
  `docs/feature-ideas-meta-session.md`'s status flipped to reconciled (pointing here).
- **Config-write reconsideration — DONE (2026-06-23):** shipped as `orion add-project` (section 3).
- **Next-slice decision:** separate session; dogfood points at OSS-readiness / onboarding polish.
- **Bot verification debt:** the Slack Socket Mode bot / `slack-bolt` slice still needs a live
  exercise.

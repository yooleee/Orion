<!-- =========================================================================
post-dogfood-kickoff.md
---------------------------------------------------------------------------
Responsible for: Orienting the NEXT session — after the native-bots slice (C2-bots)
                 and the 6/20–21 dogfood. A SCOPING artifact: it says what to read
                 first, captures the dogfood findings, and pre-scopes the next-slice
                 decision. It is NOT a build plan — the chosen slice gets its own
                 plan-mode pass (kickoff → plan → code), per CLAUDE.md.
Role in project: Output of the post-C2-bots state. Follows (does not supersede)
                 docs/phase-c2-bots-kickoff.md, which scoped the bots slice now built.
                 See plans/orion-plan.md ("C2-bots status (2026-06-19)").
========================================================================= -->

# Next session — post-bots + post-dogfood kickoff (scoping)

> **Read first:** `plans/orion-plan.md` (source of truth — start at "C2-bots status (2026-06-19)" and
> "Horizon-C next slice decided"). Then this doc. The bots how-to is `docs/slack-bot.md`; the command
> reference is `docs/commands.md`.

## Where things stand (one paragraph, no recap beyond this)

The native-bots **first slice is built and on `main`** (PRs #16–#19, `pytest` 367): a Slack Socket
Mode bot (`orion bot`) relays a supervisor's channel reply into the existing relay comment store via a
new Bearer-authed `POST /api/comments`. `slack-bolt` is an optional, lazily-imported extra; the core
install is unchanged. The **6/20–21 dogfood** is the immediate input — a *refinement* input, not a
gate (the direction was already decided).

## Step 1 — capture the dogfood (do this first)

Record findings against the four questions from `docs/phase-c2-bots-kickoff.md` (write the answers
into `plans/orion-plan.md` as a dated note, the living-doc rule):

- Did I **reach for Orion** over the lightweight alternatives (`TODO.md` / `git log` / hand-written
  standups)? When — and why not, when not?
- What felt **missing, annoying, or slow**?
- Did I ever want a supervisor to **see** or **reply to** something in the moment? (Directly tests the
  bot's value.)
- **Where was the value — delivery (Slack/Discord), the dashboard, or the session skill?** This most
  directly tunes where to invest next.

If the bot was exercised live, also note: did replies land correctly, was the author name readable,
did anything about Socket Mode setup or running `orion bot` feel heavy?

## Step 2 — pick the next slice (decide in the session, with the dogfood in hand)

Three live candidates. Recommendation depends on the Step-1 capture; default lean noted for each.

1. **Deepen the bot (second slice).** Carry the bots work forward with what the dogfood surfaced.
   Likely contents, smallest-first: **reply-targeting** (the relay endpoint already takes an optional
   `report_id` — make the bot send it, e.g. via Slack threads, so a reply hits a specific report not
   just the latest); a **second platform (Discord Gateway)** — additive via `SUPPORTED_BOT_PLATFORMS`
   + a new shell; or **hosting the bot as an always-on worker** (alongside the Fly relay) instead of a
   local process. *Lean: pick this only if the dogfood showed real reply-in-chat demand.*
2. **OSS-readiness polish.** The repo will be public — README/quickstart, a clean first-run path, the
   "clone and run in 10 minutes" test, trimming rough edges the dogfood exposed. *Lean: pick this if
   the dogfood mostly showed friction/setup pain rather than a missing feature.*
3. **Light planning/tracking layer.** The derived planning/tracking layer for more meeting-replacing
   reports (per the long-range surface-plural vision). *Lean: a later slice unless the dogfood made it
   feel urgent.*

> Apply the discipline: pause for this decision before building (plan direction before building); flag
> the strategic juncture; build seams, not futures.

## Carried-over follow-ups from the bots slice (record; pull in only when chosen)

- **Slack author display** (open micro-decision): currently resolves the display name via one
  `users.info` call (needs `users:read`), fail-soft to the user id. Alternative: store the raw id to
  minimize scopes. Author is a plain string either way — revisit if the dogfood showed it mattered.
- **Reply-targeting seam:** `POST /api/comments` accepts an optional `report_id` (unused by the
  current bot). Wiring the bot to send it is a **bot-only** change — no further relay edit.
- **Discord platform:** additive (new shell module + the `SUPPORTED_BOT_PLATFORMS` tuple); the config,
  the pure core, and the relay endpoint are already platform-neutral.
- **Bot dependency shape (stage-bound):** the optional-extra/lazy-import is the smallest-slice choice,
  expected to **graduate to a first-class integration** as the bot becomes load-bearing — keep that
  additive (see the memory note and `plans/build-the-native-bots-slice-squishy-key.md`).

## Demand-gated / out of scope (record; do not pull in)

- **C3-proper multi-party identity** (authenticated per-person identity / participant graph /
  per-supervisor routing — KI-17/KI-11/KI-1). Committed only on real multi-party demand.
- **E2E encryption / managed hosting** — a documented bridge, not a committed goal (Path B self-host +
  plaintext holds).
- **Slash commands / interactive components / rich structure** — beyond the smallest bot slice.

## Housekeeping (cheap, do whenever)

- **CI Actions are capped until 2026-07-01** — keep verifying via local `pytest` and merging on
  local-green; `main` stays PR-gated. (Re-enable CI after the reset; delete that memory note then.)

---

*Follows `docs/phase-c2-bots-kickoff.md` (which scoped the now-built bots slice).*

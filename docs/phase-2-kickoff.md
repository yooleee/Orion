# Phase 2 Kickoff — starting brief for the next session

> **Read this, then read [`plans/orion-plan.md`](../plans/orion-plan.md) in full before
> doing anything.** This file is a fast orientation, not a replacement for the plan. The
> plan is the source of truth for architecture and phasing; this just tells you where
> Phase 1 left things and how to open Phase 2.

## Where things stand (as of 2026-06-15)

- **Phase 1 is signed off.** `orion report <project>` runs the full pipeline end to end:
  `config → secrets → state → git collect → redact → summarize (Haiku) → redact →
  compose → preview/confirm → Discord (urllib) → advance state`. Live-tested with a real
  Anthropic key + Discord webhook; 52/52 tests pass.
- **Orion now tracks its own repo**: `orion.toml` has `[projects.orion]`
  (`repo_path = /home/yoolee/orion`, `share_level = high_level`). Run
  `.venv/bin/orion report orion` to post a progress update (preview-gated, default-no).
- **Backward/forward/open docs:** what shipped → [`CHANGELOG.md`](../CHANGELOG.md);
  design + phases → [`plans/orion-plan.md`](../plans/orion-plan.md); open cross-phase
  concerns → [`docs/known-issues.md`](known-issues.md); delivered messages →
  [`docs/test-messages.md`](test-messages.md).

## What Phase 2 is

The **structured lane**: signals that are already report-ready and therefore **skip the
LLM entirely**. Concretely, Phase 2 adds (per the plan's build order, item 2):

1. **A task/milestone list** signal — newly completed items.
2. **Manual notes** — a hand-written update.
3. **`intake`** — a CLI command that accepts a pushed update (`project` + body). This is
   the same entry point the Phase 6 Claude session skill will POST into. Building it now
   is what unblocks that later.

All three ride the **structured lane** and must **not** be routed through Claude (project
hard constraint: only raw git activity hits the LLM).

## Seams Phase 1 already built (Phase 2 is additive, not a rewrite)

- **The lane branch is real, not hardcoded.** `cli.py` already does
  `if result.lane == "raw": summarize_raw(...) else: body = redacted` — the `else` is
  dead in Phase 1 because nothing yet returns `lane="structured"`. **Phase 2 fills that
  `else`** by adding collectors that return `lane="structured"`. Do not change the
  signature of `summarize_raw`, `redact`, `build_report`, `compose`, or `delivery.send`.
- **`collectors/` is a directory with one file (`git.py`).** New structured collectors
  drop in as sibling files implementing the same `collect(...) -> CollectorResult`
  contract. **No plugin/registry system** — that's premature abstraction (explicit
  project constraint). "Modular" = each collector toggleable via the `collectors` list in
  config.
- **Redaction still runs on the structured lane** as a safety net (project rule:
  structured lane is redacted too, even though it's lower-risk).

## What changes in Phase 2 (and only this)

`cli.py` — the orchestrator — grows a loop over the *enabled* collectors plus a **merge
step** (combine a possible raw git summary with structured items into one report). This
is expected of an orchestrator and is contained to one file. Leaf modules stay stable.

## Open decisions to surface in plan mode (do not pick silently)

These are flagged in the plan's "Open questions" — bring a recommendation for each at the
start of the Phase 2 plan-mode pass:

1. **Merge semantics** — when a run produces *both* a raw git summary and structured items
   (e.g. completed to-dos), how do they combine into one report body? (Plan deliberately
   deferred this to Phase 2 — it was the thing most likely to be rewritten if guessed
   early.)
2. **Task/milestone source format** — where does the to-do/milestone list live and in
   what format? (A local file Orion reads — Markdown checklist? TOML? — favor stdlib and
   the 10-minute-setup test.)
3. **`intake` push mechanism** — CLI shell-out vs. a tiny local HTTP endpoint. Plan's
   lean: start with the CLI command; only add an endpoint if shelling out from inside a
   session turns out awkward. If an endpoint is ever added, it needs an auth token even on
   localhost.

## How to work this phase (project rules, do not skip)

- **Plan before code.** Open Phase 2 in plan mode (no edits): a file-by-file breakdown
  plus the three open decisions above, each with a recommendation. Only write code after
  the plan is acknowledged.
- **Smallest reviewable unit.** Propose a sub-unit breakdown and checkpoint after each
  (likely: structured collector contract + one signal → intake command → merge step →
  wire into `cli.py`). More small checkpoints over one big diff.
- **Keep the docs living.** Update `plans/orion-plan.md` (status table + Phase 2 status),
  `CHANGELOG.md` (`[Unreleased]`), and `docs/known-issues.md` in the same session as the
  work, so nothing drifts.
- **Security still overrides everything.** Redact on both lanes; preview-before-send stays
  default-on; secrets only in gitignored `.env`.

## First commands to run next session

```bash
# 1. Confirm the test baseline is still green.
.venv/bin/python -m pytest -q

# 2. Confirm live Discord delivery still works (a green suite does NOT prove this —
#    Phase 1 shipped a real 403 bug the tests didn't catch). Sends one throwaway
#    message through the real delivery path, so it also exercises the User-Agent fix.
#    Posts to the ORION_DISCORD_WEBHOOK_ALEX webhook from .env; expects no error.
.venv/bin/python -c "
from orion import secrets
from orion.delivery import discord
secrets.load_secrets()
url = secrets.get_required('ORION_DISCORD_WEBHOOK_ALEX')
discord.send('Orion Phase 2 kickoff — connectivity check', url)
print('Discord delivery OK')
"
```

If step 2 prints `Discord delivery OK`, the live path is healthy. A `DeliveryError`
(e.g. another 403) means delivery regressed and must be fixed before relying on it —
do not assume the green unit tests cover it.

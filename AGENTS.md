# Orion — Project Instructions for Codex

> Drop this file in at the **root of the Orion repo** as `AGENTS.md`. It is loaded
> automatically every session. It builds on the global `~/.Codex/AGENTS.md` (explain
> before building, incremental checkpoints, no overengineering, full code annotation) —
> those rules still apply and are not repeated here. This file adds only what is specific
> to Orion.

## What Orion is

A tool that turns a developer's project activity (git, to-do/milestone checklists, manual
notes, Codex session summaries, plus structured signals like a status-aware tracker
and an idea incubator) into readable progress updates for designated "supervisors."
Collection runs locally; the primary delivery surface is the **hosted dashboard** — a relay
plus React SPA at `project-orion.fly.dev`, with per-user logins, per-project scopes, and a
two-way per-project discussion thread. Discord/Slack webhooks remain supported channels but
the chat surface is currently on hold. The full design lives in `plans/orion-plan.md` — the
source of truth for architecture, phasing, and settled decisions. **For execution sessions**
(where the user points you to a kickoff doc), start there; the kickoff cross-references the
plan. **For planning or scoping sessions** without a kickoff doc, read `plans/orion-plan.md`
first.

## How to work in this repo

- **Build slice-by-slice off the roadmap in `plans/orion-plan.md`.** Work proceeds one
  scoped slice at a time (a phase, increment, or unit ladder), chosen at a planning
  juncture and recorded in the plan. Stop at each slice boundary and wait for review
  before starting the next. Do not pull work forward from a later slice because it is
  convenient.
- **Model split: plan on Fable, implement on Opus.** Planning / scoping / reconciliation
  sessions run on Fable and end by committing a kickoff doc under `docs/`; implementation
  sessions run on Opus, start from that kickoff, and build strictly unit-by-unit. Don't
  write feature code in a planning session.
- **Plan before code, every slice.** At the start of a slice, use plan mode (no edits):
  give a file-by-file breakdown and surface that slice's open decisions with a
  recommendation for each. Only write code after the plan is acknowledged.
- **Smallest reviewable unit.** If a slice is large, propose a breakdown into sub-units
  and checkpoint after each. The realized idiom is **one PR per unit**: code always lands
  on `main` via branch + PR; docs are tiered (consequential or public-facing docs go via
  PR, trivial private notes may commit direct).
- **Keep `plans/orion-plan.md` a living document — the roadmap table is the canonical map.** When a
  decision is made or a design detail changes, update the plan in the same session so it never drifts
  from the code. In particular, the **"Roadmap (horizons & phases)" table** at the top is the
  canonical map: re-sync it (update statuses, add rows for shipped/decided work, sketch forward bands
  coarsely) **as part of finishing each slice** — a dated prose note below is the *detail record*,
  **not** a substitute for updating the table. A stale map has coincided with slower progress; a
  current map keeps the next step unambiguous.
- **Run a parallelization & coupling analysis for brainstorming / planning / analysis tasks.** When
  the work is examining the project, weighing directions, or scoping multiple pieces (not a single
  well-defined change), also map **what could proceed in parallel vs. what is intertwined**, as part
  of the plan-before-building pass. It serves three ends: **efficiency** (independent tracks can fan
  out to agents), **architectural understanding** (a live map of coupling vs. separability), and
  **verification** (the coupling view surfaces hidden dependencies and risk). This is a carried
  *thought-process*, not a framework — keep it lightweight. The living map lives in
  `docs/parallelization.md`; update it when the analysis shifts as work lands.
- **Record every departure from an accepted plan in `docs/plan-deviations.md`.** Once a plan is
  acknowledged — a plan-mode plan, a kickoff doc, an approved act, a settled decision — any
  departure from it gets an entry: doing something different, doing *less* (an approved step
  skipped), or doing *more* (unplanned work added). Write it in the same session, while the
  reason is still exact. Ordinary implementation choices *inside* the plan do not belong there,
  and neither do factual errors — a deviation is a change of course, an error is a change of
  belief. The field that matters most is **authorization**: whether the deviation was approved
  before acting or taken unilaterally. Be honest in that field even when the answer is
  unflattering — a doc that only records the defensible deviations measures nothing.

## Hard constraints (specific to Orion)

> These are firm at the **current stage**, but several are *stage-appropriate choices, not
> permanent principles* — the local/hosted split, modular-by-toggle, and the single-LLM/Haiku
> default are all expected to be revisited as the project grows in depth and complexity. The
> **Privacy & safety** rules in the next section are the exception: those are permanent and
> non-negotiable, regardless of how the architecture evolves.

- **Straightforward to set up; minimize complexity.** (Reframed 2026-06-26.) The earlier goal
  was "stdlib-minimal, clone-and-run-in-ten-minutes." As the project grew (the E2 Inc 4 dashboard
  rebuild added a React/Vite frontend + build step), that hard gate **relaxed** to a softer aim:
  setup and use should be **easy and straightforward across usability levels** (single developer to
  multi-party). Still favor the standard library where it fits, justify every dependency, and keep
  install to a clear path with good defaults — "minimize complexity" stays a live discipline — but a
  richer stack is allowed when the task warrants it. **Open-source is now secondary** to the
  developer's own goals (it remains a possible later direction, not the current priority). The
  privacy/safety rules below and observe-not-originate are untouched by this reframe.
  **Dependency refusals carry revisit triggers (added 2026-07-29, from AU1).** When a dependency
  is declined in favor of stdlib or hand-rolled code, record the refusal together with a named
  trigger for re-litigation (a stage, a feature, or a scale point). A refusal that was right at
  one stage fossilizes silently as the project grows. The AU1 example: `http.server`, chosen when
  the relay was a loopback toy, was never re-examined as the relay became an internet-facing,
  credential-holding service, and the hand-rolled layers around it paid the discovery costs (the
  CSRF Origin bug, the ALTER race, the Argon2-OOM semaphore, KI-44) that mature serving
  infrastructure pre-pays. Standing triggers now recorded: the serving layer at stage-3/oracle
  scoping; `click` at the cli.py-split slice.
- **Cross-platform (Windows, macOS, Linux).** Orion must run on all three. Make **every**
  change with cross-compatibility in mind — not one OS at a time. The core is mostly portable
  already (stdlib, `pathlib`); keep it that way: no OS-specific path/shell assumptions, prefer
  `pathlib` over string path-joins, no `shell=True`, and don't hardcode `.venv/bin/` in docs
  (native Windows uses `.venv\Scripts\`). Where platforms genuinely diverge (e.g. scheduling:
  cron / launchd / Task Scheduler), **delegate to the OS's native tool and document per-OS**
  rather than embedding one platform's mechanism. The dedicated portability pass (A3.5)
  shipped 2026-06-15; a WSL2/Linux re-verification pass is a recorded future candidate.
- **Local collection, hosted presentation (stage-appropriate; the hybrid is now real).**
  Collectors read local files and the producer runs locally; only delivery makes outbound
  calls. Since C1 the presentation/interaction layer is **hosted**: a relay + SPA dashboard
  on Fly.io receives the portable summary+metadata blob and serves supervisors — exactly the
  seam this constraint asked to keep clean, landed additively. Moving *collection or
  summarization* hosted would be a further deliberate decision to weigh when complexity
  warrants, not a default. (Privacy & safety guarantees ride through any such shift
  unchanged.)
- **The LLM summarizer is conditional, not always-on.** The LLM is an *optional* step, not
  imposed. Raw activity that needs narrating — git diffs, Codex sessions — is summarized
  **by default**; structured or already-written updates (to-dos, milestones, notes, tracker,
  pushed session summaries) are formatted and passed through with **no** LLM call **by
  default**. The invariant is that structured/already-written content is **not force-routed**
  through the model — *not* that git is the only thing the LLM may ever touch. Opt-in LLM use
  elsewhere is realized practice: the disciplines "Working agreements" extraction is an
  opt-in, cache-gated Haiku step; scheduled checklist pushes have no LLM stage anywhere.
- **Modular by toggle, not by framework.** Each signal (git, sessions, to-dos, notes, tracker,
  incubator, disciplines docs) and each destination (Discord, Slack, the relay/dashboard) must
  be independently enable-able per project in config, and must not assume the others are
  present. Do **not** build a plugin/registry system yet — that is premature abstraction.
  "Modular" here just means cleanly toggleable.
- **Multi-party identity is shipped and account-based; build on it, not around it.**
  (Refreshed 2026-08-20 — the previous version of this paragraph predated the revamp it
  announced.) The S2.0 auth revamp (shipped 2026-07-20) replaced the bare-key model with
  accounts that hold scopes and credentials: roles (admin / viewer / supervisor / member /
  contributor), multiple key credentials per account (`relay-user key add/list/revoke`,
  replacing `rotate`), optional passwords for interactive accounts, per-project grants plus
  org-level visibility, and agent accounts with an operating human (`set-operator`). Push
  identity is per-user contributor keys; the legacy shared ingest token is retired in
  practice (prod runs `--disable-legacy-ingest`) and its code path is slated for removal in
  the CS-O arc. The `relay-user` surface itself is under the command-surface overhaul
  (CS-O, kickoff `docs/command-surface-overhaul-build-kickoff.md`): ungrant lands there
  (grants were add-only, KI-40) and account-level `revoke` renames to `deactivate`. The
  original guidance stands: name a project's participants explicitly, and keep
  report/intake a portable summary+metadata blob.

## Privacy & safety (non-negotiable)

- Redact obvious secrets (API keys, `.env` contents, tokens) before any text reaches the LLM
  or a channel. Redaction runs on the raw lane and as a safety net on the structured lane.
- **Preview-before-send** is the default until trust is established. Never send a report
  without showing it first, unless explicitly configured otherwise.
- Secrets (webhook URLs, Anthropic API key) live in a gitignored `.env`, never committed.
- The summarizer is prompted to report outcomes and progress, not raw code or secrets.

> **Scope note (2026-07-17, recorded decision — clarifies, does not soften):**
> preview-before-send guards content the user has *not already seen* — the LLM-summarized
> raw lane. User-authored structured content (e.g. the checklist a scheduled
> `checklist-push --all --due` sends) was reviewed at authoring time and may push
> unattended. This relaxes *preview only, never redaction* — redaction runs on every
> outbound lane regardless.

## Model

- Summarizer uses **Codex Haiku 4.5** (`Codex-haiku-4-5`) — the lightest model adequate for
  summarization; the opt-in disciplines extraction also runs on Haiku. Step up to Sonnet for
  that step only if Haiku visibly misses nuance on real diffs (quality on varied diffs is
  still not empirically confirmed — KI-4). The summarizer seam is provider-agnostic (B4): an
  OpenAI-compatible local model can be swapped in via config.

## Tech baseline (from the plan — confirm in each slice's plan-mode pass)

- Python; `subprocess` + `git` for git access; stdlib `sqlite3` for the state store;
  Anthropic Python SDK for the summarizer; incoming webhooks via stdlib `urllib.request` for
  delivery; a **TOML** config (stdlib `tomllib`) for the project registry; `.env` +
  `python-dotenv` for secrets. Net runtime dependencies: **3** (`anthropic`, `python-dotenv`,
  and `tzdata` — data-only, for portable timezone rendering on platforms without a system tz
  database). Optional extras stay out of the core install: `dev` (pytest) and `slack-bot`
  (`slack-bolt`, lazily imported, for the parked bot). Prefer these before adding any new
  **backend** dependency, and justify any addition against the "minimize complexity"
  discipline above.
- The **frontend** is a React/Vite/TypeScript SPA in `web/`, built once and served
  single-host by the relay (`relay-serve --web-dir`), deployed on Fly.io — its own justified
  stack choice (E2 Inc 4). The lean-backend baseline above still governs the Python
  producer/relay/CLI.
- **Phase 1 decisions settled (2026-06-14):** config format is **TOML** (zero-dep, read-only
  is fine since Orion never writes it); delivery uses stdlib **`urllib.request`** (one JSON
  POST needs no `requests`); the git payload to the LLM is a **hybrid** (commit messages +
  diffstat + a capped, secret-filtered diff, the diff only at `share_level = "detailed"`).

## Instruction profile

Router hints per `~/.Codex/Codex-md-toolkit/rubric.md` §7 — never guaranteed loading.

- `tdd-design` branch: `general`
- `docs/technology-preferences.md` — the relay/SPA deploys on Fly.io, per its
  demo-deployment lean; consult before hosting or frontend-stack changes

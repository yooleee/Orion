# Orion — Project Instructions for Claude Code

> Drop this file in at the **root of the Orion repo** as `CLAUDE.md`. It is loaded
> automatically every session. It builds on the global `~/.claude/CLAUDE.md` (explain
> before building, incremental checkpoints, no overengineering, full code annotation) —
> those rules still apply and are not repeated here. This file adds only what is specific
> to Orion.

## What Orion is

A local-first tool that turns a developer's project activity (git, a to-do/milestone list,
manual notes, and Claude Code session summaries) into readable progress updates and delivers
them to designated "supervisors" over Discord or Slack. The full design lives in
`plans/orion-plan.md` — **read it before doing anything**. It is the source of truth for
architecture, phasing, and the decisions already settled.

## How to work in this repo

- **Build strictly phase-by-phase, in the order given in `plans/orion-plan.md`.** Do one phase at a
  time. Stop at each phase boundary and wait for review before starting the next. Do not
  pull work forward from a later phase because it is convenient.
- **Plan before code, every phase.** At the start of a phase, use plan mode (no edits): give
  a file-by-file breakdown and surface that phase's open decisions with a recommendation for
  each. Only write code after the plan is acknowledged.
- **Smallest reviewable unit.** If a phase is large, propose a breakdown into sub-units and
  checkpoint after each. Prefer more, smaller checkpoints over one big diff.
- **Keep `plans/orion-plan.md` a living document.** When a decision is made or a design detail
  changes, update the plan in the same session so it never drifts from the code.

## Hard constraints (specific to Orion)

- **Open-source friendly, simple to set up.** This will be public. Favor the standard
  library, keep dependencies minimal and justified, and keep install to one clear path with
  good defaults. Apply the test: *could a new user clone this and run it in ten minutes?*
- **Local-first.** Collectors read local files; only delivery makes outbound calls. Do not
  introduce a hosted/server component into the core without flagging it as a deliberate
  deviation (a web dashboard is a planned *future* phase, not part of the core).
- **The LLM summarizer is conditional, not always-on.** Only raw activity (git diffs) goes
  through Claude. Structured or already-written updates (to-dos, milestones, notes, pushed
  session summaries) are formatted and passed through with **no** LLM call. Do not route
  structured updates through the model.
- **Modular by toggle, not by framework.** Each signal (git, sessions, to-dos, notes) and
  each channel (Discord, Slack) must be independently enable-able per project in config, and
  must not assume the others are present. Do **not** build a plugin/registry system yet —
  that is premature abstraction. "Modular" here just means cleanly toggleable.
- **No multi-tenancy yet, but don't hardwire single-user.** Do not build multi-user or
  multi-supervisor machinery now. But avoid assuming one implicit "me": name a project's
  participants/recipients explicitly, and keep report/intake as a portable summary+metadata
  blob, so a shared service could be added later without a rewrite.

## Privacy & safety (non-negotiable)

- Redact obvious secrets (API keys, `.env` contents, tokens) before any text reaches the LLM
  or a channel. Redaction runs on the raw lane and as a safety net on the structured lane.
- **Preview-before-send** is the default until trust is established. Never send a report
  without showing it first, unless explicitly configured otherwise.
- Secrets (webhook URLs, Anthropic API key) live in a gitignored `.env`, never committed.
- The summarizer is prompted to report outcomes and progress, not raw code or secrets.

## Model

- Summarizer uses **Claude Haiku 4.5** (`claude-haiku-4-5`) — the lightest model adequate for
  summarization. Step up to Sonnet 4.6 only for that step, and only if Haiku visibly misses
  nuance on real diffs. Confirm quality empirically in Phase 1.

## Tech baseline (from the plan — confirm in each phase's plan-mode pass)

- Python; `subprocess` + `git` for git access; stdlib `sqlite3` for the state store;
  Anthropic Python SDK for the summarizer; incoming webhooks via stdlib `urllib.request` for
  delivery; a **TOML** config (stdlib `tomllib`) for the project registry; `.env` +
  `python-dotenv` for secrets. Prefer these before adding any new dependency, and justify any
  addition against the open-source-simplicity constraint.
- **Phase 1 decisions settled (2026-06-14):** config format is **TOML** (zero-dep, read-only
  is fine since Orion never writes it); delivery uses stdlib **`urllib.request`** (one JSON
  POST needs no `requests`); the git payload to the LLM is a **hybrid** (commit messages +
  diffstat + a capped, secret-filtered diff, the diff only at `share_level = "detailed"`).
  Net runtime dependencies: **2** (`anthropic`, `python-dotenv`).

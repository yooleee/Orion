# Project Orion — Progress Tracker & Reporter (Design Plan)

> **Status: Phase 1 implemented (2026-06-15), awaiting sign-off.** Architecture and phasing
> agreed; the Phase 1 MVP is built, live-tested, and documented. This is task #7 on the
> non-application to-do ("Build progress tracker (Project Orion)").
>
> This file looks **forward** (design + phase plan). For what actually shipped, see
> [`CHANGELOG.md`](../CHANGELOG.md); for open cross-phase concerns, see
> [`docs/known-issues.md`](../docs/known-issues.md).

## Phase status

| Phase | Scope | Status |
|---|---|---|
| 1 | `orion report`: git → redact → conditional Haiku summary → preview → Discord | ✅ Implemented, live-tested (awaiting sign-off) |
| 2 | Structured lane: intake, to-dos, notes (no-LLM passthrough) | ⏳ Not started |
| 3 | Slack delivery + recipient routing | ⏳ Not started |
| 4 | Scheduled digests (cadence) | ⏳ Not started |
| 5 | Event-driven triggers (git hooks) | ⏳ Not started |
| 6 | Claude Code session skill (pushes summaries to Orion) | ⏳ Not started |
| 7 | Supervisor replies / web dashboard | ⏳ Not started |

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
  change, a hand-written note) are passed through and formatted directly. The Claude
  summarizer is a *conditional* step that runs only for raw activity that needs
  narrating (git diffs, or a session when the skill is not used). This keeps cost and
  latency down and matches the "lightest adequate" principle at the level of the whole
  pipeline, not just the model.
- **Supervisor replies are a planned later phase**, with two possible paths: native
  Discord/Slack threads (works only if both user and supervisor are on that platform), or
  comments on a future web dashboard. Deferred, not designed in detail yet.

## The shape of the problem (two conclusions that drive the design)

1. **Local-first, not hosted.** The collectors must read local git repos and local Claude
   Code session files, which live on Yousuf's machine. A Cloudflare Worker (his usual
   hosting preference) cannot reach local files, so the **core runs locally**. Only the
   delivery step makes outbound HTTPS calls to Discord/Slack. This is a deliberate
   deviation from the Cloudflare preference, justified by where the data lives. A hosted
   dashboard could be added later, but it is not the core.
2. **Reports are LLM-summarized *when they need to be*, with privacy guardrails.** Raw
   diffs and session transcripts can contain secrets and read as noise to a supervisor.
   For those, Orion redacts obvious secrets, then has Claude summarize activity into an
   abstracted, audience-appropriate update. But structured or already-written updates (a
   to-do change, a milestone, a hand-written note, or a summary pushed in by the Claude
   skill) do **not** go through the LLM at all. Orion routes each update down one of two
   lanes (see Architecture), so the model only runs where it adds value. A
   preview-before-send step guards against leaks on the first runs.
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
- **Secrets via `.env` + `python-dotenv`** (gitignored) — webhook URLs and the Anthropic
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
- **Summarizer (conditional)** — Claude turns redacted *raw* activity into a concise
  progress narrative at the project's configured share level. Skipped entirely for
  structured/already-written updates.
- **Composer** — merges whatever this run produced (a summary and/or structured items) and
  formats it for each channel (Discord embed / Slack blocks, or plain markdown to start).
- **Delivery** — POSTs to the configured Discord and/or Slack webhooks per recipient.
- **State store** — records what was reported so the next run covers only the delta.

## Phasing (front-load value; defer the fragile parts)

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
  diffs in Phase 1, and remember the summarizer only runs on the raw lane.
- **Git payload to the LLM (Phase 1, 2026-06-14):** a **hybrid** — commit messages (intent)
  + diffstat (scope) + a line-capped, secret-filtered code diff (detail). The diff is sent
  only at `share_level = "detailed"`; `high_level` sends messages + diffstat only. Sensitive
  files are excluded from the diff at collection time (Python allowlist of literal paths
  passed to `git diff`), so secret file *contents* never reach the model — only redaction of
  inline secrets is left to the redactor.
- **State store (Phase 1, 2026-06-14):** stdlib `sqlite3` (atomic marker writes, clean
  history table, zero dependency) over a JSON file.

## Phase 1 status (2026-06-14)

Phase 1 is **implemented** in `src/orion/` with a 52-test suite (`tests/`). Pipeline:
`config → secrets → state → git collect → redact → summarize (Haiku) → redact → compose →
preview/confirm → Discord (urllib) → advance state`. The conditional-LLM lane seam is built
(only the raw lane is wired); leaf-module signatures are stable so Phase 2's structured lane
is additive. Awaiting user sign-off that Phase 1 is complete before starting Phase 2.

**Live verification (2026-06-15):** ran the full path against a throwaway repo with a
seeded fake key and a real Anthropic key + Discord webhook — preview clean, initial and
incremental sends delivered, state advanced, re-run a no-op, stored body redacted. Two bugs
were found and fixed during this run (redaction hit-count double-counting; Discord 403 from
a missing `User-Agent`) — see [`CHANGELOG.md`](../CHANGELOG.md) for the details and fixes.

## Open questions / to settle before/while building

- The skill/plugin's exact push mechanism into Orion (a CLI shell-out vs a tiny local HTTP
  endpoint Orion runs). Decide when Phase 2 intake is built; the endpoint is only needed if
  a shell-out is awkward from inside a session.
- Intake authentication if a local endpoint is used (even localhost-only deserves a token).
- How structured items and a raw summary are merged in one report when both exist in a run.

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

## Verification (per phase)

- MVP: in a test repo, make commits, run `orion report`, confirm the preview reflects the
  diff, confirm the Discord webhook receives the message, confirm the state store advances
  the last-reported commit so a second run reports "no new activity."
- Later phases: each new signal appears in the report; Slack receives the same; a scheduled
  run fires from cron; a git hook triggers a report; session activity is summarized without
  leaking file contents.
- Redaction: seed a repo with a fake API key and confirm it never appears in the preview or
  the delivered message.

# Project Orion — Progress Tracker & Reporter (Design Plan)

> **Status: Horizon A (the local single-user reporting core) is complete and signed off
> (2026-06-15)** — `report` over git + structured signals + `intake`, two-pass redaction,
> preview-before-send, dual-channel (Discord **and** Slack) delivery with routing,
> cross-platform support, and safe **unattended scheduled digests** (`report --all --yes`).
> **Horizon B (local automation, ingestion & polish) is underway — B1 (event-driven git-hook
> triggers) is signed off (2026-06-16); B2 (the Claude Code session skill) is next.** This is
> task #7 on the non-application to-do ("Build progress tracker (Project Orion)").
>
> The **Roadmap** below is organized into **horizons** (A shipped · B next · C the
> multi-party/hosted pivot, kept coarse). This file looks **forward** (design + phase plan).
> For what actually shipped, see `[CHANGELOG.md](../CHANGELOG.md)`; for open cross-phase
> concerns, see `[docs/known-issues.md](../docs/known-issues.md)`.

## Roadmap (horizons & phases)

> **Numbering.** Phases are grouped into **horizons** (A, B, C, … and future D, …); numbering
> is **horizon-scoped and restarts each horizon**, so a phase number never grows into unwieldy
> double digits as the project runs on. **Horizon A keeps the original phase numbers unchanged,
> just prefixed** (A1 = the former "Phase 1", … A3.5 = "Phase 3.5", A4 = "Phase 4"), so every
> existing reference in `[CHANGELOG.md](../CHANGELOG.md)`, commits, and the kickoff docs still
> maps by inspection. From Horizon B onward, numbering is fresh (B1, B2, …). The detailed
> per-phase "status" sections further down keep their legacy "Phase N" headings (N == A-N) as
> the historical shipping record.

**Horizon A — Local single-user reporting core** *(shipped ✅)*


| Phase          | Scope                                                                  | Status                    |
| -------------- | ---------------------------------------------------------------------- | ------------------------- |
| A1 (was 1)     | `report`: git → redact → conditional Haiku summary → preview → Discord | ✅ Signed off (2026-06-15) |
| A2 (was 2)     | Structured lane: intake, to-dos, notes (no-LLM passthrough)            | ✅ Signed off (2026-06-15) |
| A3 (was 3)     | Slack delivery + recipient routing                                     | ✅ Signed off (2026-06-15) |
| A3.5 (was 3.5) | Cross-platform portability pass (audit + fixes + scheduling stance)    | ✅ Signed off (2026-06-15) |
| A4 (was 4)     | Scheduled digests — unattended `report --all --yes`                    | ✅ Signed off (2026-06-15) |


**Horizon B — Local automation, ingestion & polish** *(next; local-first preserved)*


| Phase | Scope                                                                                                                                                                                                                                   | Status                    |
| ----- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------- |
| B1    | Event-driven triggers — git `post-commit` (and/or `pre-push`) hook delegating to `report` (fire-on-commit); opt-in, cross-platform. *(Note: git has no client-side `post-push` hook — `post-commit`/`pre-push` are the local options.)* | ✅ Signed off (2026-06-16) |
| B2    | Claude Code session skill — summarize a coding session and push it via `intake` (the session signal)                                                                                                                                    | ✅ Implemented (2026-06-16) — awaiting sign-off |
| B3    | Richer rendering — Slack Block Kit + Discord embeds, done together (KI-9); likely a small `ReportBlob`/`compose` change to carry structured sections                                                                                | ⏳ Planned                 |
| B4    | Summarizer flexibility — provider-agnostic summarizer seam + optional local model + per-step model choice (keeps "lightest adequate model")                                                                                             | ⏳ Planned                 |
| B5    | Scheduling *layer* — activity-gating, `report --all --due`, quiet hours, per-recipient cadence (KI-13). Built **only if** OS-delegation is outgrown; sits at the B→C boundary                                                           | ⏳ Conditional             |
| B6    | CLI ergonomics — **read-only** config-inspect commands (`projects`/`show`/`check`) for visibility/discoverability. Orion still never *writes* config (hand-edited TOML stays the way to change it). Small polish; KI-15                 | ⏳ Planned (small)         |


**Horizon C — Multi-party & hosted** *(the architectural pivot; coarse — sequenced by dependency, detail to firm up as it nears)*

These converge into one horizon: bidirectional interaction (supervisors acting back) forces an
always-on **listener**, which is what tips local-first → **hosted/hybrid**, which is where
**multi-party** data must meet. So they are dependency-ordered, not finely pre-phased:


| Phase | Scope                                                                                                                                                                                          | Status            |
| ----- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------- |
| C1    | Web dashboard (read) + hosted/hybrid relay — collection stays local; delivery/presentation move hosted along the portable report/intake blob seam                                              | 🔭 Later (coarse) |
| C2    | Bidirectional replies — supervisors comment back (dashboard first; native Discord/Slack threads as a richer add-on); brings inbound validation + authorization                                 | 🔭 Later (coarse) |
| C3    | Multi-party: identity, subscriptions & authorization — a participant graph (not an implicit "me"), per-supervisor per-project/task/todo subscriptions (the routing future), and access control | 🔭 Later (coarse) |


Beyond Horizon C (a Horizon D, …) is deliberately not sketched yet — the discipline is to keep
the **seams** clean (the portable summary+metadata blob, explicitly-named participants, the
provider-agnostic summarizer) so the next horizon stays additive rather than a rewrite.

**Cross-cutting through every horizon:** security & privacy (redaction + preview, gaining an
inbound validate/authorize side in C), open-source-friendly simple setup, cross-platform
portability, and cross-machine interoperability (UTF-8 / UTC-ISO-8601 / canonical `\n`, no
machine-local paths in any cross-machine artifact). The rationale behind Horizon C lives in
"Future direction & guiding principles" and "Cross-platform & future-direction rationale" below.

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
change, a hand-written note) are passed through and formatted directly by default. The
Claude summarizer is an *optional, conditional* step — not imposed — that by default runs
on raw activity that needs narrating (git diffs, or a session when the skill is not used)
and is skipped for already-written content. The point is that the LLM is available but not
forced: not every action is significant enough to warrant a summary, and some users would
rather write their own. This keeps cost and latency down and matches the "lightest
adequate" principle at the level of the whole pipeline, not just the model. (It is *not* a
rule that git is the only thing the LLM may ever touch — a user could opt in to LLM
summarization elsewhere.)
- **Supervisor replies are a planned later phase**, with two possible paths: native
Discord/Slack threads (works only if both user and supervisor are on that platform), or
comments on a future web dashboard. Deferred, not designed in detail yet.

## The shape of the problem (two conclusions that drive the design)

1. **Local-first, not hosted (at the current stage).** The collectors must read local git
  repos and local Claude Code session files, which live on Yousuf's machine. A Cloudflare
   Worker (his usual hosting preference) cannot reach local files, so the **core runs
   locally**. Only the delivery step makes outbound HTTPS calls to Discord/Slack. This is a
   deliberate deviation from the Cloudflare preference, justified by where the data lives.
   *Local-first is a stage-appropriate choice, not a permanent principle:* as the project
   grows (multi-party collaboration, a hosted dashboard, a shared service), the primary
   grounding may shift toward hosted/hybrid — a deliberate decision to weigh when complexity
   warrants, kept additive by the portable report/intake blob. A hosted dashboard could be
   added later, but it is not the core today. (The privacy/safety guarantees are the part that
   stays permanent through any such shift.)
2. **Reports are LLM-summarized *when they need to be*, with privacy guardrails.** Raw
  diffs and session transcripts can contain secrets and read as noise to a supervisor.
   For those, Orion redacts obvious secrets, then has Claude summarize activity into an
   abstracted, audience-appropriate update. Structured or already-written updates (a
   to-do change, a milestone, a hand-written note, or a summary pushed in by the Claude
   skill) are **not routed through the LLM by default** — they are already report-ready, so
   forcing them through the model adds cost without value. Orion routes each update down one
   of two lanes (see Architecture), so the model runs only where it adds value, and is opt-in
   rather than imposed. A preview-before-send step guards against leaks on the first runs.
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
- **Secrets via `.env` + `python-dotenv*`* (gitignored) — webhook URLs and the Anthropic
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
- **Summarizer (conditional, opt-in)** — Claude turns redacted *raw* activity into a concise
progress narrative at the project's configured share level. Skipped **by default** for
structured/already-written updates (the model is optional, not imposed — see the "Not every
report needs an LLM" decision above).
- **Composer** — merges whatever this run produced (a summary and/or structured items) and
formats it for each channel (Discord embed / Slack blocks, or plain markdown to start).
- **Delivery** — POSTs to the configured Discord and/or Slack webhooks per recipient.
- **State store** — records what was reported so the next run covers only the delta.

## Phasing (front-load value; defer the fragile parts)

> **Historical (the original MVP sequencing rationale).** The forward-looking phase plan now
> lives in the **Roadmap** section above (horizons A/B/C). This numbered list reflects the
> *original* 7-phase plan and is kept for *why* value was front-loaded the way it was — it has
> since been expanded and re-grouped into horizons. Where the two differ, the Roadmap wins:
> original Phases 1–4 are Horizon A (A1–A4); original 5 → B1; original 6 → B2; original 7
> (supervisor replies / dashboard) is expanded across Horizon C (C1–C3).

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
  - diffstat (scope) + a line-capped, secret-filtered code diff (detail). The diff is sent
  only at `share_level = "detailed"`; `high_level` sends messages + diffstat only. Sensitive
  files are excluded from the diff at collection time (Python allowlist of literal paths
  passed to `git diff`), so secret file *contents* never reach the model — only redaction of
  inline secrets is left to the redactor.
- **State store (Phase 1, 2026-06-14):** stdlib `sqlite3` (atomic marker writes, clean
history table, zero dependency) over a JSON file.

## Phase 1 status (2026-06-14)

Phase 1 is **implemented** in `src/orion/` with a 52-test suite (`tests/`). Pipeline:
`config → secrets → state → git collect → redact → summarize (Haiku) → redact → compose → preview/confirm → Discord (urllib) → advance state`. The conditional-LLM lane seam is built
(only the raw lane is wired); leaf-module signatures are stable so Phase 2's structured lane
is additive. **Signed off 2026-06-15** — all release-gate criteria verified (52/52 tests,
redaction corpus + denylist-path tests, seeded-fake-key clean end to end, share-level
behavior). Orion now also tracks its own repo (`[projects.orion]`, `high_level`). The
Phase 2 starting brief lives in `[docs/phase-2-kickoff.md](../docs/phase-2-kickoff.md)`.

**Live verification (2026-06-15):** ran the full path against a throwaway repo with a
seeded fake key and a real Anthropic key + Discord webhook — preview clean, initial and
incremental sends delivered, state advanced, re-run a no-op, stored body redacted. Two bugs
were found and fixed during this run (redaction hit-count double-counting; Discord 403 from
a missing `User-Agent`) — see `[CHANGELOG.md](../CHANGELOG.md)` for the details and fixes.

## Phase 2 status (2026-06-15)

Phase 2 — the **structured lane** — is **implemented** in `src/orion/` with a 90-test suite
(+38 over Phase 1). It was built in seven reviewed checkpoints: per-collector state markers
(+ non-destructive migration of the Phase-1 git marker) → config `tasks_file`/`notes_file`
→ tasks collector → notes collector → merge helper → multi-collector orchestrator rewrite →
`intake` command. The three open decisions were resolved with the user (see above): merge =
one sectioned body; task source = Markdown checklist; intake = CLI command.

What shipped (details in `[CHANGELOG.md](../CHANGELOG.md)`): the **tasks** and **notes**
structured collectors (no LLM), the `**orion intake`** push command (no collector / no LLM /
no marker), a `merge.py` step that combines signals into one sectioned message, and a generic
per-`(project, collector)` marker store so each signal tracks its own delta. The structured
lane never reaches Claude — a test proves a structured-only run does not call the summarizer
even when the summarizer is wired to raise. Frozen seams (`summarize_raw`, `redact`,
`build_report`, `compose`, `delivery.send`) were not changed; net new runtime dependencies: 0.
Two fields are now vestigial (`ReportBlob.source_marker`, `project_state.last_commit`) — see
`[docs/known-issues.md](../docs/known-issues.md)` KI-8.

**Signed off (2026-06-15).** Live check passed: the full structured-lane path ran against a
throwaway project (git + tasks + notes merged into one sectioned report) plus an `intake`
push to the real Discord webhook, with a seeded fake key redacted and an immediate re-run a
no-op. `pytest`: 90/90 at Phase 2 close.

## Phase 3 status (2026-06-15)

Phase 3 — **Slack delivery + recipient routing** — is **implemented** in `src/orion/` with a
105-test suite (+15 over Phase 2). Built in four reviewed checkpoints: Slack sender + channel
config → Slack compose rendering → orchestrator routing → docs + live verification. Three
decisions were settled with the user (see "Decisions settled" above-style notes in the session
plan): Slack format = plain mrkdwn (Block Kit deferred, to be paired with richer Discord
formatting later); preview = one block per channel + one combined confirm; routing scope =
channel routing only (same content per recipient, routed by channel).

What shipped (details in `[CHANGELOG.md](../CHANGELOG.md)`): `delivery/slack.py` (a `{"text": …}` mrkdwn POST mirroring Discord), a `slack` branch in `compose` with a structural
`_to_slack_mrkdwn` translator, and per-channel routing in the orchestrator — each run composes
once per distinct channel and delivers each recipient their channel's rendering via
`_sender_for(channel)`, with one labeled preview block per channel and a single combined
confirm. Both `report` and `intake` route. Frozen seams unchanged; net new runtime
dependencies: 0. A `Recipient` is a delivery *destination* (channel + webhook); the eventual
per-supervisor (per project/task/todo) routing model is deferred (see KI-11 and the session
plan), with today's explicit recipient naming keeping that door open.

**Signed off (2026-06-15).** Live dual-channel check passed: a project with a Discord and a
Slack recipient delivered one report (and one `intake` push) to the real Discord webhook and
the test Slack workspace, each correctly formatted (Discord `**`/`##`, Slack `*…*`), with a
seeded fake AWS key redacted in both. `pytest`: 105/105.

## Phase 3.5 status (2026-06-15)

Phase 3.5 — the **cross-platform portability pass** — is **implemented** in `src/orion/` and
the docs, with a 110-test suite (+5 over Phase 3). An audit (paths, the venv entry point,
`subprocess` git calls, console encoding, file I/O encoding, line endings) confirmed the core
was already highly portable — `pathlib` throughout, explicit `encoding="utf-8"` on every text
read, `splitlines()` for line endings, no `shell=True`, and a component-built timestamp that
already avoids `%-I`/`%#I`. This pass therefore made only targeted fixes, not a rewrite.

What shipped (details in `[CHANGELOG.md](../CHANGELOG.md)`): a `python -m orion` entry point
(`__main__.py`) as the OS-neutral canonical invocation; a console UTF-8 guard
(`cli._ensure_utf8_output`) that keeps the `⚠`/`✗` glyphs but prevents a `UnicodeEncodeError`
on redirected Windows output; a "Supported platforms" + per-OS setup rewrite of the README
(leading with `python -m orion`, OS-agnostic `python -m pip`/`python -m pytest`); and Windows
TOML-path guidance in the README and `orion.toml.example`. No collector/redaction/summarizer/
compose/delivery logic changed; net new runtime dependencies: 0.

Four decisions were settled with the user (rationale in "Cross-platform & future-direction
rationale" below): support matrix = **native Windows + macOS + Linux** (WSL counts as Linux);
canonical invocation = `**python -m orion`**; console Unicode = **keep glyphs + a guarded
`reconfigure("utf-8")`**; and the **scheduling stance** = Orion ships **no scheduler of its
own**, delegating cadence to each OS's native tool (cron / launchd / Task Scheduler),
documented per-OS — the key input to Phase 4 (the carried auto-send tension, then tracked as
KI-12, was resolved in Phase 4 — see the Phase 4 status above and `CHANGELOG.md`).

The test suite is catalogued (categories + why each matters + known gaps) in
`[docs/testing.md](../docs/testing.md)`; the manual cross-OS runbook is
`[docs/portability-smoke-test.md](../docs/portability-smoke-test.md)`. A post-3.5 audit
confirmed all test files are current (nothing stale/removable) and closed five additive
coverage gaps (Slack-token redaction, git noise-glob/diff-cap/subdir-sensitive, the encoding
guard's `OSError` arm).

**Signed off (2026-06-15).** `pytest`: 115/115.

## Phase 4 status (2026-06-15)

Phase 4 — **scheduled digests / unattended send** — is **implemented** in `src/orion/` and the
docs, with a 126-test suite (+11 over Phase 3.5). It was built in five reviewed checkpoints:
the `config.py` `auto_send` field → the `cli.py` `--yes` + `_run_report` refactor (with the
security-critical tests) → `report --all` (fail-soft + summary) → docs (`auto_send`/`--all`/
`--yes` in README + `orion.toml.example`, new `docs/scheduling.md`) → these living-doc updates.
The design was settled with the user up front (see `[docs/phase-4-kickoff.md](../docs/phase-4-kickoff.md)`);
this phase was a build, not a re-plan.

What shipped (details in `[CHANGELOG.md](../CHANGELOG.md)`): the `**auto_send`** per-project
opt-in, the `**--yes**` non-interactive flag, and `**--all**` for every-project runs. The
load-bearing rule is that the human preview is bypassed **only** when `--yes` **and**
`auto_send=true` are both present — `--yes` alone never sends (a non-opted project is skipped and
logged), and `auto_send` alone never sends (without `--yes` the preview always shows). Redaction
is untouched: both passes still run on every path, so unattended delivery relaxes no
secret-scrubbing — it bypasses only the *human* preview, for opted-in projects. `--all` is
fail-soft (one project's error doesn't stop the rest) and exits non-zero **only on a real
failure**, so a scheduler alerts on genuine problems, not on routine no-activity/skipped runs.
Orion still ships **no scheduler of its own**: cadence is delegated to the OS, documented per-OS
in `[docs/scheduling.md](../docs/scheduling.md)` (with the WSL2 caveat and minimal-environment
gotchas). Frozen seams (`redact`, `summarize_raw`, `build_report`, `compose`, `delivery.send`,
the state store) were not changed; net new runtime dependencies: 0.

The security contract is pinned by `tests/test_schedule.py`, including the load-bearing test
that `auto_send` **without** `--yes` still previews, and that a seeded fake key is still redacted
on the auto-send path. `**pytest`: 126/126.** Resolves KI-12; the deferred cadence-aware
`report --all --due` filter is recorded as KI-13.

**Signed off (2026-06-15).** Live verification passed: against a throwaway repo with a real
Anthropic key + the real Discord/Slack webhooks, an `auto_send` project run with `--yes`
auto-sent without a preview and the delivered body had a seeded fake key redacted; the same
project **without** `--yes` still previewed; a `--yes` run of a non-opted project was skipped;
and `report --all --yes` auto-sent the opted-in project, skipped the rest, and a no-activity
re-run sent nothing (all exit 0). A dual-channel run delivered to Discord **and** Slack together,
and the real Phase 4 commit was reported live to both channels via `report orion`. `pytest`:
126/126. A native Windows/macOS scheduler smoke (per `[docs/scheduling.md](../docs/scheduling.md)`

- `[docs/portability-smoke-test.md](../docs/portability-smoke-test.md)`) remains a hardware-gated
follow-up, not a blocker.

## Phase B1 status (2026-06-16) — opens Horizon B

Phase B1 — **event-driven triggers (git hooks)** — is **implemented** in `src/orion/` and the
docs, with a 137-test suite (+11 over A4). Built in four reviewed checkpoints: a pure
`hooks.py` (`build_hook_script` + `resolve_hooks_dir`) → the `cli.py` `install-hook` command →
docs (`docs/git-hooks.md` + README) → these living-doc updates. Decisions settled with the user
(2026-06-16): deliver as a **command + runbook**; default trigger **pre-push** (post-commit also
supported via `--hook`).

What shipped (details in `[CHANGELOG.md](../CHANGELOG.md)`): `orion install-hook <project>`
installs a portable `#!/bin/sh` hook that runs `report <project> --yes` in the background and
**always exits 0**, so it never delays or blocks a commit/push; it embeds absolute paths (the
venv's python, the config, a `<git-dir>/orion-hook.log`), uses forward-slash paths so it's valid
under the `sh` git uses on Windows, refuses to clobber an existing hook without `--force`, offers
`--print` to review, and warns when the project isn't `auto_send`-opted. **The report pipeline is
untouched** — the hook only calls the existing `report --yes`, so all Horizon-A safety guarantees
carry over. No new config field; net new runtime dependencies: 0. Git has no client-side
`post-push` hook, so the local options are `post-commit`/`pre-push` (documented). The
single-file-hook model's limits (hook-manager coexistence, one-project-per-hook) are recorded as
KI-14. One small supporting change landed in `secrets.py`: `load_secrets` now also reads the
`.env` **beside the `--config` file**, so a hook/scheduled run (which starts in another directory)
finds Orion's central secrets via the config path it already passes — fixing secret discovery for
both unattended paths, with unchanged `override=False` precedence.

**Signed off (2026-06-16).** Live verification passed: on a throwaway repo, an installed
**pre-push** hook fired on `git push` (push returned in ~19 ms — never blocked), ran
`report --yes` in the background, and delivered to Discord; the hook log showed
`Auto-sending … / Sent to: …`. The **secrets fix** was confirmed separately: from a foreign
working directory with no exported env, `load_secrets(.../orion.toml)` loaded the central `.env`
purely via `--config`, and a `report orion` run from `/tmp` (no sourced env) delivered to **both
Discord and Slack** — proving unattended secret discovery and dual-channel delivery together.
`pytest`: 139/139.

## Phase B2 status (2026-06-16)

Phase B2 — the **Claude Code session skill** — is **implemented** (the fourth and final
ingestion signal). Built in four reviewed checkpoints: `intake --yes` (cli.py) + its
security tests → the skill artifact `skills/orion-session/SKILL.md` (+ `skills/README.md`) →
docs (README) → these living-doc updates. Decisions settled with the user (2026-06-16):
confirmation = **in-session review + `intake --yes`**; skill location = `skills/orion-session/`.

What shipped (details in [`CHANGELOG.md`](../CHANGELOG.md)): a `Claude Code skill` that drafts an
outcome-focused, secret-free session summary, **shows it for in-session approval**, then pushes it
via `intake … --yes`. Orion does **not** re-summarize — the skill's summary is delivered (after
redaction) on the existing structured lane. The only Orion-side change is `intake --yes`, which
skips the terminal preview for the (non-interactive) skill send; unlike `report --yes` it needs
**no `auto_send` gate** (intake is always an explicit push, never unattended), and redaction runs
unchanged. The skill is a separable artifact outside the Python package (no packaging change);
net new runtime dependencies: 0.

> **Awaiting sign-off.** Implementation and the automated suite (`pytest`: 141/141, +2 for
> `intake --yes`) are complete, plus a live check of the skill's send mechanism (a sample summary
> piped through `intake --yes` from a foreign CWD delivered to Discord + Slack with a seeded key
> redacted). The fully end-to-end check — invoking the skill *inside* a real Claude Code session
> so it summarizes that session — is a natural user-run confirmation.

## Open questions / to settle before/while building

- **(Resolved, Phase 2, 2026-06-15) Push mechanism:** a **CLI command** (`orion intake <project>`, body via `--message` or stdin). No local HTTP endpoint — no network surface,
no server process, no auth token needed now. An endpoint (with a token) remains a
documented future option only if shelling out proves awkward from inside a session.
- **(Resolved by the above)** Intake authentication: not applicable while intake is a local
CLI command. Revisit only if/when a local endpoint is added.
- **(Resolved, Phase 2, 2026-06-15) Merge semantics:** **one body, titled `##` sections**,
in config (collector) order, empty sections skipped — one preview, one delivered message.
Only the git section is LLM-summarized; structured sections pass through verbatim and are
never re-sent to Claude. Lives in `merge.py` (a pure function), called by the orchestrator
before the final redaction pass.

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
- **Cross-platform: Windows, macOS, Linux (a guiding principle, active now — confirmed
2026-06-15).** Every change is made with cross-compatibility in mind, not one OS at a time.
The core is already mostly portable (stdlib, `pathlib`, a platform-safe timestamp); keep it
so. Where platforms diverge — scheduling (cron / `launchd` / Task Scheduler), git hooks —
delegate to the OS's native tool and document per-OS rather than embedding one platform's
mechanism. A dedicated **Phase 3.5 portability pass** (audit + fixes + the scheduling stance)
precedes Phase 4, because Phases 4–5 are the most platform-divergent features. See
`[docs/phase-3.5-kickoff.md](../docs/phase-3.5-kickoff.md)`.
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

### Cross-platform & future-direction rationale (recorded Phase 3.5, 2026-06-15)

Reasoned through while settling the Phase 3.5 scheduling stance; recorded so the *why*
survives. **None of this is built yet** — it shapes future phases and the seams to protect.

- **Why Orion delegates scheduling to the OS (no built-in scheduler).** Orion is a one-shot
CLI; to fire at time T something must be alive at T, and Orion can't wake itself. Building a
scheduler means either a long-running daemon (which *still* needs the OS service manager to
survive reboot/sleep — so it adds a daemon *on top of* the OS layer, a bigger always-on
surface, the same reason webhooks beat an always-on bot) or an in-process scheduler library
(a dependency that only runs while the process runs). The OS schedulers (cron / launchd /
Task Scheduler) already own reboot-persistence, missed-run policy, and run-as-user. So Orion
delegates and documents per-OS. Accepted cost: per-OS setup divergence, no unified
`orion status`, per-OS missed-run semantics, and minimal-environment gotchas (stripped PATH,
no venv) — all documentation-shaped, cheaper than cross-platform service management.
- **When a built-in scheduling *layer* becomes right.** The test is whether cadence needs
Orion's own state. While cadence = "run a command at T," the OS tool wins. Once it needs
activity-gating ("only send if something changed"), backoff, quiet hours, per-recipient
cadence, or a unified next-run/last-error view, that logic must live in Orion — likely a
**hybrid** (OS provides the wake-up; Orion owns the decision). Because `orion report` is
already a clean non-interactive entry point, that shift is **additive — no rewrite**.
- **Bidirectional interaction (supervisors acting back) moves this.** "Destination → origin"
forces an always-on **listener** into existence (a bot/gateway connection or a public
inbound endpoint). Once that process exists, an in-process scheduler becomes nearly free —
so the listener and the "cadence needs state" moments likely arrive together (reinforcing
"don't build a scheduler before the process that would host it"). Bidirectional is also what
tips **local-first → hosted/hybrid** (a NAT'd, sleeping laptop is a poor inbound host); the
architecture then **splits** — collection stays local (it must read local files), delivery +
interaction move to a hosted relay — along the existing **portable report/intake blob seam**
(built for exactly this). New disciplines it brings: inbound = *untrusted input +
authorization* (the security story, until now purely outbound redaction, gains an inbound
validate/authorize side); state grows from an append-only log into a correlated conversation
(per-platform thread/message IDs); platform coupling deepens (receiving needs
platform-specific frameworks/signatures), which argues for the platform-neutral **dashboard
(Phase 7b)** as the primary interaction surface, with native-thread replies (7a) as a richer
add-on.
- **Multi-OS at once + multi-user × multi-supervisor.** Two distinct guarantees:
*portability* (Orion runs on each OS — the active principle) and *interoperability* (a
Windows producer and a macOS consumer agree on shared formats). The supervisor's OS is a
non-issue today — the chat platform abstracts it — and only matters for **artifacts that
cross machines** (the blob, future relay payloads), where the discipline is UTF-8 /
UTC-ISO-8601 timestamps / canonical `\n` and, the one invariant to state explicitly, **no
machine-local filesystem paths or locale-dependent formatting in any cross-machine
artifact** (the blob carries names and IDs, not paths). Many-to-many **converges on the same
hosted component** as bidirectional (multiple producers' data must meet in a shared place),
and promotes identity/addressing (a participant graph, not an implicit "me"), authorization
(who may see/act on which project), and routing → *subscriptions* to first-class concerns.
Seeds already in this plan: explicit participants, the portable blob, per-subscription
routing.

## Verification (per phase)

- MVP: in a test repo, make commits, run `orion report`, confirm the preview reflects the
diff, confirm the Discord webhook receives the message, confirm the state store advances
the last-reported commit so a second run reports "no new activity."
- Later phases: each new signal appears in the report; Slack receives the same; a scheduled
run fires from cron; a git hook triggers a report; session activity is summarized without
leaking file contents.
- Redaction: seed a repo with a fake API key and confirm it never appears in the preview or
the delivered message.


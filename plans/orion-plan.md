# Project Orion — Progress Tracker & Reporter (Design Plan)

> **Status: Horizon A (the local single-user reporting core) is complete and signed off
> (2026-06-15)** — `report` over git + structured signals + `intake`, two-pass redaction,
> preview-before-send, dual-channel (Discord **and** Slack) delivery with routing,
> cross-platform support, and safe **unattended scheduled digests** (`report --all --yes`).
> **Horizon B (local automation, ingestion & polish) is complete — B1 (git-hook triggers),
> B2 (the Claude Code session skill), B3 (richer rendering), B4 (summarizer flexibility), and
> B6 (read-only config-inspect commands) are all signed off; B5 (a scheduling *layer*) was
> evaluated at its gate and deliberately deferred into Horizon C (2026-06-17).** All four
> ingestion signals (git, tasks, notes, sessions) now feed Orion. This is task #7 on the non-application to-do ("Build progress tracker
> (Project Orion)").
>
> The **Roadmap** below is organized into **horizons** (A shipped · B next · C the
> multi-party/hosted pivot, kept coarse). This file looks **forward** (design + phase plan).
> For what actually shipped, see `[CHANGELOG.md](../CHANGELOG.md)`; for open cross-phase
> concerns, see `[docs/known-issues.md](../docs/known-issues.md)`.
>
> **Strategy overlay:** the *why/what-success* layer above this roadmap — Objective, Goals,
> differentiators, and deferred long-range directions — lives in
> `[docs/orion-strategy.md](../docs/orion-strategy.md)` (this roadmap is its "Actions"). Settled in
> the post-C1 direction-setting pass (2026-06-18); see "Horizon-C direction settled" below.

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
| B2    | Claude Code session skill — summarize a coding session and push it via `intake` (the session signal)                                                                                                                                    | ✅ Signed off (2026-06-16) |
| B3    | Richer rendering — Slack Block Kit + Discord embeds, done together (KI-9); likely a small `ReportBlob`/`compose` change to carry structured sections                                                                                | ✅ Signed off (2026-06-17) |
| B4    | Summarizer flexibility — provider-agnostic summarizer seam + optional local model (OpenAI-compatible). Per-step model choice **deferred** (one LLM step today; seam keeps it additive). Keeps the "lightest adequate model" default (Haiku) | ✅ Signed off (2026-06-17) |
| B5    | Scheduling *layer* — activity-gating, `report --all --due`, quiet hours, per-recipient cadence (KI-13). Built **only if** OS-delegation is outgrown; sits at the B→C boundary. **Gate evaluated 2026-06-17 → defer.**                       | ⏭️ Deferred → Horizon C    |
| B6    | CLI ergonomics — **read-only** config-inspect commands (`projects`/`show`/`check`) for visibility/discoverability. Orion still never *writes* config (hand-edited TOML stays the way to change it). Closes KI-15                 | ✅ Signed off (2026-06-16) |


**Horizon C — Multi-party & hosted** *(the architectural pivot; coarse — sequenced by dependency, detail to firm up as it nears)*

These converge into one horizon: bidirectional interaction (supervisors acting back) forces an
always-on **listener**, which is what tips local-first → **hosted/hybrid**, which is where
**multi-party** data must meet. So they are dependency-ordered, not finely pre-phased:


| Phase | Scope                                                                                                                                                                                          | Status            |
| ----- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------- |
| C1    | Web dashboard (read) + hosted/hybrid relay — collection stays local; delivery/presentation move hosted along the portable report/intake blob seam                                              | 🚧 First slice shipped (vendor-neutral relay + Path-B reference; loopback-only). Hosting **settled: Path B (self-host)**; managed/Cloudflare deferred behind the seam, E2E as the privacy bridge. **Second slice (2026-06-18): deploy *beyond loopback* (Basic-Auth + fail-closed guard) + dashboard hardening.** |
| C2    | Bidirectional replies — supervisors comment back (dashboard first; native Discord/Slack threads as a richer add-on); brings inbound validation + authorization                                 | 🎯 **Next destination** (direction settled 2026-06-18). The C1 second slice stands up the always-on host C2's listener will live on. "Smallest first slice" (dashboard-comments vs bot Socket-Mode/Gateway; which surface first) settled in its own plan-mode pass. |
| C3    | Multi-party: identity, subscriptions & authorization — a participant graph (not an implicit "me"), per-supervisor per-project/task/todo subscriptions (the routing future), and access control | 🔭 Deferred (a clean seam, not a destination now — committed only on real demand; the multi-party *product* leap). Home of E2E + KI-17 + per-recipient state. |


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
- **Summarizer (conditional, opt-in)** — an LLM (Claude Haiku by default; config-selectable
behind a provider-agnostic seam since B4, including a local model) turns redacted *raw* activity
into a concise progress narrative at the project's configured share level. Skipped **by default** for
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
diffs in Phase 1, and remember the summarizer only runs on the raw lane. *(B4, 2026-06-17:
the model/provider is now config-selectable behind a provider-agnostic `Summarizer` seam —
Anthropic by default, or a local OpenAI-compatible model — with Haiku still the default;
see the Phase B4 status section.)*
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
Phase 2 starting brief lives in `[docs/phase-2-kickoff.md](../docs/archive/phase-2-kickoff.md)`.

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
The design was settled with the user up front (see `[docs/phase-4-kickoff.md](../docs/archive/phase-4-kickoff.md)`);
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

**Signed off (2026-06-16).** Verified two ways: the send mechanism (a sample summary piped
through `intake --yes` from a foreign CWD delivered to Discord + Slack with a seeded key redacted
to `[REDACTED_AWS_KEY]`), and then the **skill run fully end-to-end** — the `orion-session` skill
was installed into `~/.claude/skills/`, invoked in a real session, drafted a summary, showed it
for in-session approval, and on approval delivered it to both channels via `intake --yes`.
`pytest`: 141/141 (+2 for `intake --yes`).

## Phase B6 status (2026-06-16)

Phase B6 — **read-only config-inspect commands** — is **implemented**, closing the
visibility/discoverability gap surfaced while using `install-hook` (KI-15). Built in three
reviewed checkpoints: `projects` + `show` → `check` (validity + readiness) → docs + living docs.
Decisions settled with the user (2026-06-16): build **all three** commands; `check` does
**validity + readiness**; plain-text output.

What shipped (details in [`CHANGELOG.md`](../CHANGELOG.md)): `orion projects` (list every project
with `auto_send`/share level/collectors/channels), `orion show <project>` (one project's resolved
config), and `orion check` (validate the config, then report per-project send-readiness — repo
path exists, each webhook secret present, Anthropic key present for the git lane — by NAME as
set/MISSING, and a non-zero exit if anything required is missing, so it works as a pre-flight
gate). The load-bearing constraint holds: these commands **only read** config — Orion still never
writes it, and a `config set` style command was deliberately excluded (it would break that
decision and need a comment-preserving TOML writer dependency). No secret value is ever printed.
No new runtime dependencies; `config.py`/`secrets.py` reused unchanged. **Resolves KI-15.**

**Signed off (2026-06-16).** Implementation and the automated suite (`pytest`: 148/148, +7 for the
inspect commands) are complete, plus a live check against the real config: `projects`/`show`/
`check` all correct, `check` even caught a genuine missing repo path (a cleaned-up throwaway) and
exited non-zero, and zero secret values were printed by any command.

## Phase B3 status (2026-06-17)

Phase B3 — **richer rendering (Slack Block Kit + Discord embeds)** — is **implemented** in
`src/orion/` with a 154-test suite (+6 net over B6). Built in four reviewed checkpoints: carry
structured sections on `ReportBlob` + per-section redaction (CP1) → the `ComposedMessage`
payload seam with delivery as pure transport, no look change (CP2) → the rich per-channel
renderers + faithful preview + overflow fallback (CP3) → docs + living docs (CP4). The five open
decisions from `[docs/phase-b3-kickoff.md](../docs/archive/phase-b3-kickoff.md)` were settled with the
user (2026-06-17): **(D1)** carry `(title, body)` sections on the blob, `body` stays canonical +
fallback; **(D3)** redaction pass 2 runs per section before the blob is built; **(D2)** the
preview is rendered from the actual payload's text fields; **(D5)** `compose` returns a
`ComposedMessage(payload, preview)` and `send` takes the payload dict; **(D4)** rich rendering is
always-on with a built-in plain fallback (no config toggle; deferred as a clean seam) and a
graceful fallback to the plain message on size overflow.

What shipped (details in `[CHANGELOG.md](../CHANGELOG.md)`): Discord embeds (one field per
signal) and Slack Block Kit (header / context / per-signal section blocks + `text` fallback),
both built from the blob's sections; `compose` now owns rendering + truncation while delivery is
pure transport (`send(payload, url)`); the second redaction pass runs per section so no
block/embed field can bypass it; and a faithful preview rendered from the payload. Frozen seams
held except the deliberate, KI-9-sanctioned change to `compose`'s return type and `send`'s input
(the "message is one string" model). Net new runtime dependencies: 0. **Resolves KI-9**; **KI-10
updated** (the `_to_slack_mrkdwn` translator is still scoped to the two constructs Orion emits,
now applied to each section body rather than the flattened report).

**Live verification (2026-06-17):** the real B3 commit was reported via `report orion` to the
real Discord + Slack channels — Discord rendered the embed card, Slack rendered Block Kit (the
single-section `orion` case differs only subtly from plain, confirmed against a Block Kit probe);
a separate throwaway multi-signal project then delivered an unmistakable multi-section embed /
Block Kit (stacked section blocks + dividers) to both channels, with state advancing and the
re-run a no-op.

**Signed off (2026-06-17).** `pytest`: 154/154. Future rendering polish (e.g. splitting an
oversized report across multiple embeds/messages instead of falling back to plain) is deferred to
a later horizon (Horizon C or beyond), per the user.

## Phase B4 status (2026-06-17)

Phase B4 — **summarizer flexibility (provider-agnostic seam + optional local model)** — is
**implemented** in `src/orion/` with a 172-test suite (+18 net over B3). Built in four reviewed
checkpoints: the global `[summarizer]` config + validation (CP1) → the
`Summarizer` Protocol with the Anthropic backend refactored behind it, default behavior unchanged
(CP2) → the `LocalSummarizer` (OpenAI-compatible endpoint over stdlib `urllib`) + backend-aware
`check` (CP3) → docs + living docs (CP4). The open decisions from
`[docs/phase-b4-kickoff.md](../docs/archive/phase-b4-kickoff.md)` were settled with the user (2026-06-17):
**(1)** the seam is a one-method `Summarizer` Protocol, with `cli._build_summarizer` constructing
the configured backend via explicit provider dispatch (not a registry); **(2)** config is a
single **global** `[summarizer]` table (per-project override left as a clean, additive seam);
**(3)** ship the seam + the Anthropic refactor **and** one real local backend (a single
implementation would be a "fake seam"); **(4)** per-step model choice **deferred** — one LLM step
exists today, so the seam keeps it additive; **(5)** Anthropic keeps `ANTHROPIC_API_KEY`, a local
endpoint needs no key unless `api_key_env` names one; **(6)** every backend fails closed into
`SummarizerError`, and the local backend adds **0** runtime dependencies.

What shipped (details in `[CHANGELOG.md](../CHANGELOG.md)`): the module-level `MODEL` constant and
the standalone `summarize_raw` are gone, replaced by `AnthropicSummarizer` (now using the
configured model) and `LocalSummarizer` behind the `Summarizer` Protocol; both share the one
security-relevant system prompt and the empty-result guard. The default — a config with no
`[summarizer]` table — still routes the git lane through Anthropic/Haiku unchanged. Redaction and
preview-before-send are identical for every backend; a local model is simply more private (no
outbound summarization call). The local backend targets the **OpenAI-compatible** `/chat/
completions` shape (one path for Ollama / llama.cpp / LM Studio / vLLM) rather than a runtime's
native API — a deliberate "engineered enough" call, tracked with its tradeoff and
change-conditions as **KI-16**.

**Signed off (2026-06-17)** on the strength of the 172-test suite. `pytest`: 172/172. The
optional **live/manual verification** carried over from this sign-off was **completed at the
start of the B5 session (2026-06-17)**, against a throwaway git repo delivering to the real
Discord + Slack channels: (1) the **default Anthropic/Haiku** path (no `[summarizer]` table)
rendered a summary and delivered end to end, with a seeded fake AWS key redacted from the
detailed diff; (2) a **local** backend (`provider = "local"`, Ollama at
`http://localhost:11434/v1`, `model = "qwen2.5:0.5b"` — a tiny model, because the check is of
backend *wiring*, not summary quality) rendered a local-model summary and delivered; and (3) the
local backend **failed closed** — an unreachable endpoint surfaced a clean `SummarizerError`
(exit 1), sent nothing, and did **not** advance state (the same delta re-reported on the next
successful run). No B4 follow-up fixes were needed.

## Phase B5 status (2026-06-17) — gate evaluated: deferred into Horizon C

Phase B5 (a scheduling *layer* inside Orion — `report --all --due`, activity-gating, quiet
hours, per-recipient cadence, a unified status view) was always marked **⏳ Conditional**: the
first question is *whether* to build it at all, not *how*. That gate was evaluated this session
and the decision (with Yousuf, 2026-06-17) is to **defer B5 and fold it into Horizon C** — no
B5 code now.

**Why defer:**

- **No concrete need yet.** Mixed per-project cadences are already expressible with multiple
  OS-scheduler entries (one per cadence group), and activity-gating already exists implicitly —
  every run is a no-op when there is no delta. The one named candidate, the cadence-aware
  `--due` filter (KI-13), is a convenience over the multiple-entries approach, not a need anyone
  has hit.
- **Sequencing.** The plan's own analysis ("When a built-in scheduling *layer* becomes right" /
  "Bidirectional interaction moves this") notes that the Horizon-C bidirectional **listener** and
  the "cadence needs Orion's *own* state" moment likely arrive together: once an always-on
  process exists to host a listener, an in-process scheduler is nearly free. Building B5
  standalone now would be "building the future" before the process that should host it exists.
- **The seam is already clean.** Picking B5 up in Horizon C is additive, not a rewrite: a
  per-project `schedule` field would mirror the existing `share_level` / `auto_send` validation
  in `config.py`, and "last successful report time per project" is **derivable from the existing
  `report_history` table** (`SELECT MAX(sent_at) ... WHERE project = ?`) — no new schema needed.
  `report --all` is the layering point for a future `--due` filter.

**What this means for the roadmap:** with B1–B4 and B6 signed off and B5 deferred, **Horizon B's
local-automation scope is complete.** The security invariants are untouched — the `--yes` +
`auto_send` preview-bypass gate and both redaction passes carry forward unchanged into whenever
B5's logic is eventually built. KI-13 records the deferral and the no-new-schema implementation
note for whoever builds it.

Also completed this session: the carried-over **B4 live/manual verification** (see the Phase B4
status block above) — the default Anthropic path, a local Ollama backend, and local fail-closed
all verified end to end against real channels.

## Horizon B → C boundary review (2026-06-17)

With Horizon B complete (B1–B4, B6 signed off; B5 deferred), this is a deliberate **consolidation
pass at the B→C boundary** — *not* detailed Horizon-C planning, which stays deferred until C
nears (designing C1–C3 in detail now would be "building the future"). Its job is to lock in two
strategic insights from this session and confirm the seams C will lean on are clean.

**Insight 1 — the summarizer has a capability floor (≈Haiku-4.5).** The B4 local-backend
verification ran a very small model (`qwen2.5:0.5b`) and it was noticeably worse — rough, partly
hallucinated summaries — confirming the long-suspected point that the summarizer needs roughly
Haiku-level capability to be *usable*. Implication for the B4 local-model option: "lightest
**adequate** model" means a mid-capability model, not the tiniest; sub-adequate models produce
poor output. A **model-tier comparison** (cloud vs local; where the cost/adequacy sweet spot
sits) would be useful but is **non-foundational** — a future experiment, not a phase. Tracked in
KI-4; the local-model docs (`README`, `orion.toml.example`) were reconciled to this framing.

**Insight 2 — webhooks are an inbound dead-end, so bidirectionality means bots (a build *and
maintain* cost).** An incoming webhook is outbound-only by construction; there is no upgrade path
to *receive*. Reading a supervisor's reply requires a registered **bot** per platform — Discord
(Gateway WebSocket + `MESSAGE_CONTENT` intent, or an Interactions endpoint) or Slack (Events API
public endpoint, or Socket Mode) — i.e. a new **always-on listener** that tips local-first →
hosted, plus ongoing maintenance (OAuth scopes/intents, request signing, reconnect/dedup, rate
limits, platform review). Two mitigations, already latent in the design and made explicit here:

- **Delivery and interaction are separable.** A bot for *inbound* need not replace webhooks for
  *outbound*; and because delivery is already behind a per-channel seam (`delivery.send(payload,
  url)` + `_sender_for(channel)` + the portable blob), a bot *sender* — or a hosted relay — can be
  added **additively**, without touching the report pipeline.
- **Dashboard-first defers bots.** C1 (a read-only web dashboard + hosted relay) provides a
  comment surface and the hosted shift **without any bot**; native in-platform replies become a
  *richer add-on* (C2), not a foundation.
- **Decision (with Yousuf, 2026-06-17):** native in-platform Discord/Slack discussion **is a
  wanted feature**, but it does **not** need to precede the existing Horizon C content (the web
  dashboard, etc.). Its exact slot is left to detailed Horizon-C planning. The C2 gate to settle
  then: is native in-platform reply a *must-have*, or is the dashboard sufficient as the primary
  interaction surface (bots as the add-on)?

**Seams Horizon C depends on — confirmed clean (invariants to protect):**

- **Delivery transport is swappable (webhook → bot → hosted relay).** Per-channel sender dispatch
  plus the payload/blob seam keep an outbound bot sender or a hosted relay additive. *(Stated
  explicitly here as an invariant for the first time.)*
- **Portable report/intake blob** (summary + metadata; no machine-local paths) — the seam for
  moving delivery/presentation hosted.
- **Named recipients / participant model** (KI-11) — recipients are named destinations today; the
  per-supervisor participant graph (C3) is additive on top.

**On-ramp (sketch only, not a design):** the gentlest first C step is a **read-only dashboard fed
by the portable blob** — collection stays local, presentation moves hosted — which aligns with
the Cloudflare hosting preference. Bidirectional + bots follow later (C2), behind the dashboard,
at a slot to be decided in detailed planning. Detailed C1–C3 design remains deferred — C1 is
framed (open decisions surfaced, not settled) in
[`docs/phase-c1-kickoff.md`](../docs/archive/phase-c1-kickoff.md).

## Phase C1 status (2026-06-18) — first slice: hosting-agnostic relay + read-only dashboard

**Opens Horizon C.** Built the part of C1 that needs **no** hosting decision: a **vendor-neutral
outbound relay seam** on the local side, and a **Path-B reference implementation** of the hosted
half (a small stdlib Python relay + read-only dashboard) in a new, separately-deployable top-level
`relay/` package. **Zero new dependencies, no core-pipeline changes.** Implemented across eight
checkpoints (CP1 `serialize_blob` → CP2 `[relay]` config → CP3 `delivery/relay.py` → CP4 fail-soft
wiring into `report`/`intake` + `check` readiness → CP5 `relay/store.py` → CP6 `relay/server.py`
ingest → CP7 `relay/render.py` + dashboard routes → CP8 `orion relay-serve` + docs). `pytest`:
**226** (172 at B-close). Verified end to end by a local dogfood: `relay-serve` + a real `intake` →
delivery → relay push → store → dashboard (index → history → report). **Awaiting sign-off.**

**The vendor-neutral invariant (treat as PERMANENT):** local Orion knows *only* "serialize the
portable blob (JSON) + a Bearer token → POST to a configured URL," `orion_version`-stamped. That
single seam decouples the local core from any hosting choice, so the **same** outbound push works
unchanged against a future Cloudflare ingress — the hosting choice stays genuinely open and is now
*informed* by a working Path-B reference.

**Accepted caveat (this slice):** the local relay is **loopback-only** (`127.0.0.1`), so the
dashboard is for the user's own machine; informal supervisors still see Discord/Slack delivery, not
the dashboard, until hosting lands.

**Deferred — with a near-term revisit point (NOT indefinite).** These were scoped out of *this
slice* and are slated for a **next-phase planning/decision juncture right after C1 sign-off**
(sooner than later, per Yousuf):

- **The hosting decision (Path A Cloudflare vs Path B self-host)** — **RESOLVED 2026-06-18 →
  Path B** (self-host). See the "Hosting decision (settled 2026-06-18)" section just below for the
  rationale and the E2E bridge to a future managed option. Receiver stays loopback-only until an
  actual hosted deployment is built.
- **OSS-readiness polish pass — alongside that next-phases discussion (Yousuf's explicit call):**
  CI, CONTRIBUTING, SECURITY, issue/PR templates, README/docs polish (WSL2-cron caveat, a "you're
  ready" checkpoint), **plus** the friction items — KI-1 dual-channel partial-failure policy, a
  new-repo blob baseline / `--init`, and the `orion-session` abs-path ergonomics.
- **Dashboard maturation (rides with the hosting decision):** the C1 dashboard is deliberately
  minimal (loopback-only, plain server-rendered HTML) — right for a "does it work" pass, not a
  finished surface. A proper **visual design** *and* richer **report content** (e.g. submitter/
  author accountability — **KI-17**) land when the dashboard becomes **supervisor-facing**, because
  those content features (multi-party identity, C3) reshape the design — so designing it now would
  be premature. (Recorded from the 2026-06-18 dashboard dogfood: drill-down navigation validated;
  content/design flagged as future.)
- **C2/C3 sequencing** (bidirectional, multi-party) — firmed up at the same juncture. The
  post-C1 OSS-readiness pass is now complete, so this **horizon-planning juncture is teed up** in
  [`docs/horizon-planning-kickoff.md`](../docs/archive/horizon-planning-kickoff.md) (north-star + C2/C3 +
  hosted-deploy + E2E sequencing), to be run **informed by the 2026-06-20/21 hackathon dogfood
  read**.

Detail on the slice's settled decisions (D1–D7) is in
[`docs/phase-c1-kickoff.md`](../docs/archive/phase-c1-kickoff.md) and the approved checkpoint plan.

## Hosting decision (settled 2026-06-18) — Path B (self-host), with E2E as the bridge to managed hosting

**Decision: Path B — a portable, self-hostable Python relay (the C1 reference). NOT all-Cloudflare
(Path A), NOT the hybrid.** Settled with Yousuf after re-verifying current (mid-2026) platform facts.

**Why Path B (verified, not assumed — checked 2026-06-18):**

- **Path A would be a stack-divergent rewrite, not "deploy what we built."** Cloudflare Python
  Workers are **still in open beta** and require Cloudflare's `WorkerEntrypoint`/`fetch` handler
  model, storage via **D1 bindings** (not the `sqlite3` stdlib), and the `pywrangler` toolchain. Our
  `http.server` relay + `sqlite3` store don't port — "it's Python too" doesn't save the rewrite.
- **D1 exposes no external connection** to the raw data, locking the redacted-but-sensitive data
  inside Cloudflare's ecosystem — against own-your-data.
- **Path B deploys the code we already wrote** (same language/ecosystem), is vendor-neutral, runs
  anywhere (Fly ~$2/mo, Render free-with-sleep or $7 warm, a VPS, or **free on a Pi/home box**), and
  keeps the data on infrastructure the user controls — the cleanest "anyone can run it" story. Its
  only cost vs A is a few $/mo (or a box) + keeping a process alive; **zero if self-hosted**.
- **The hybrid is rejected for now:** it means building AND maintaining *two* implementations (the
  Python relay + a Cloudflare rewrite) in lockstep — a standing maintenance tax for one upside (free
  personal hosting), unjustified at this stage.

**Path A is NOT foreclosed.** Because C1 locked the vendor-neutral contract, a Cloudflare ingress
(a Worker speaking the same blob+token contract, writing D1) can be added **later as an additional
deploy target** without touching local Orion — a documented future managed-deploy *option behind the
seam*, not a parallel build we maintain.

**The privacy bridge — using managed hosting WITHOUT degrading own-your-data: end-to-end (client-
side) encryption.** Own-your-data has two routes: (1) hold the bytes yourself (self-host = Path B),
or (2) hold the *keys* yourself — let a host store **ciphertext it cannot read** ("zero-knowledge"),
decrypting in the viewer's browser with a key the host never sees (how password managers use
untrusted cloud). Path A *naive* (plaintext in D1) fails because it does neither. The future path to
privacy-preserving managed hosting is therefore:

- an **encrypt-before-push** step — local, *additive* to the existing seam; authenticated symmetric
  crypto (adds a `cryptography`/`pynacl` dependency, the one real cost against minimal-deps);
- the dashboard shifts **server-rendered → client-decrypting** (static page + ciphertext + in-browser
  decrypt — where Cloudflare **Pages** would finally fit);
- **key management** — single-user is one key; multi-supervisor needs key distribution / envelope
  encryption, so this is **entangled with C3** (multi-party identity *is* key distribution);
- **metadata** decisions (encrypted bodies still leak cleartext project/timestamps unless those are
  encrypted too).

This is **a future horizon (intertwined with C3), not now** — but the C1 seam doesn't block it
(encryption slots in additively before the push; the blob is `orion_version`-stamped). Mental model
to carry: **self-host = trust by ownership; managed + E2E = trust by cryptography** — both satisfy
own-your-data; Path A naive satisfies neither. **Near-term: Path B, plaintext, self-host;** E2E is
the documented bridge to a managed option later.

## Horizon-C direction settled + C1 second slice (2026-06-18)

The deliberate **post-C1 direction-setting pass** (the `plan-direction-before-building` juncture).
Run with foundational rigor — *don't default, justify*. The hackathon (6/20–21) was reframed as a
**readiness test, not a driver** of direction. Strategy detail lives in
`[docs/orion-strategy.md](../docs/orion-strategy.md)`; the as-launched kickoff is archived at
`[docs/archive/horizon-planning-kickoff.md](../docs/archive/horizon-planning-kickoff.md)`.

**Settled:**

- **North star — an excellent OSS *solo-dev → supervisor* tool, *scale-invariant by aspiration*** (no
  scale a second-class citizen; every multi-party feature zero-cost at one participant). Current build
  focus stays solo→supervisor; multi-party is a clean seam. **Driver: showcase / learning** — which is
  *why* the next destination is C2, not polish-only.
- **Differentiators (earned, not bolted on):** data sovereignty (own-your-data/local-first) ·
  derives-from-existing-work (*reframing, not originating*) · agentic-execution-native (ingests Claude
  Code sessions) · scale-invariance. Ease-of-use and redaction/preview are *enablers/quality bars*, not
  differentiators alone.
- **Sequence — C2 next · C3 deferred · E2E deferred as the documented bridge.** C2 (bidirectional) is
  inside the north star (a supervisor replying to *your* reports — the loop getting richer, not
  multi-party) and the richest architecture on the board. C3 (multi-party product leap) and E2E
  (managed-hosting privacy bridge) stay seams.
- **Methodology overlay — OGSM + Cagan's Product Operating Model carried as *thought processes*, not
  installed frameworks** (augment the discipline, never replace it): OGSM = a recurring
  success-articulation step; POM = a solo-scaled "outcomes over output / discovery / focus" mindset
  guarding against becoming an order-taker to one's own roadmap. Filled the gap this pass exposed — an
  explicit definition of success (see the strategy doc's Goals).

**C1 second slice (the immediate build, Thu–Fri pre-hackathon):** deploy the relay **beyond loopback**
(Path B) + **harden** the dashboard, as one push (hardening *is* the deployed thing's quality).
Security gate first: the dashboard GET routes have **no read auth** today (loopback-only), so leaving
loopback requires **HTTP Basic-Auth on GET routes + a fail-closed guard** (refuse a non-loopback bind
without the view secret) before any deploy. The local push side is already deploy-ready (the `[relay]`
URL accepts any host; Bearer auth + fail-soft + tests exist).

**Recorded deferred directions** (detail in the strategy doc — *seams kept clean, not built*):

- **Light planning/tracking layer** — the to-do/milestone leg evolves from retrospective-only toward a
  *derived* forward-looking layer (milestones/sprints/due-dates/at-risk), governed by **reframing, not
  originating** (no data re-entry; Orion stays downstream even for planning). Converges with the
  deferred scheduling layer (**KI-13**) and Horizon C's stateful process.
- **Long-range vision (Horizon D+, aspirational/unvalidated):** Orion as a *coordination/visibility*
  layer (not an execution platform — complementary to Claude Code), **surface-plural** (native
  Slack/Discord *and* the dashboard), multi-project/cross-project. The inflection to watch is
  read-only → read-*write* dashboard.

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
`[docs/phase-3.5-kickoff.md](../docs/archive/phase-3.5-kickoff.md)`.
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
- **(2026-06-18) Newer recorded directions** — *scale-invariance* (no scale a second-class citizen),
the *light planning/tracking layer* (governed by *reframing, not originating*), and the *long-range
coordination/visibility-hub* vision are recorded under "Horizon-C direction settled" above and in
detail in `[docs/orion-strategy.md](../docs/orion-strategy.md)`. Same discipline: seams kept clean,
not built.

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


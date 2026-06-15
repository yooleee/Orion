# Changelog

All notable changes to Orion are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project aims to follow [Semantic Versioning](https://semver.org/). Entries are grouped
under **Added / Changed / Fixed / Removed**, newest version first. Each entry is brief
and, where it aids understanding, notes *why* the change was made.

This file looks **backward** (what actually shipped). For the forward-looking design and
phase plan, see [`plans/orion-plan.md`](plans/orion-plan.md); for open issues and
cross-phase concerns, see [`docs/known-issues.md`](docs/known-issues.md).

## [Unreleased]

Phase 3 — **Slack delivery + recipient routing**: a project can now report to Discord *and*
Slack in one run, with each recipient routed to their own channel and webhook.

### Added

- **Slack delivery** (`delivery/slack.py`) — a single `{"text": …}` mrkdwn POST via stdlib
  `urllib`, a sibling of the Discord sender with the same `send(message, webhook_url)` shape
  and shared `DeliveryError`. (Block Kit deferred — see `docs/known-issues.md` KI-9.)
- **Slack message rendering** (`compose.py`) — a `slack` branch plus `_to_slack_mrkdwn`,
  which translates the Markdown Orion emits to Slack's dialect (`## h` → `*h*` bold lines,
  `**b**` → `*b*`). Discord rendering is unchanged. (Structural-only — KI-10.)
- **Per-channel routing** (`cli.py`) — each run composes once per distinct recipient channel
  and delivers each recipient their channel's rendering via `_sender_for(channel)`. The
  preview shows one labeled block per channel with a single combined confirm; a single-channel
  run is unchanged. Applies to both `report` and `intake`.
- **Config/secrets** — `SUPPORTED_CHANNELS` now includes `"slack"`; a Slack recipient names a
  `webhook_env_var` pointing at a Slack incoming-webhook URL in `.env` (no new mechanism).
- **Tests** — 105 total (+15): the Slack sender, Slack compose rendering, and multi-channel
  routing (each recipient gets the right format via the right webhook; combined preview;
  decline aborts all; one channel failing doesn't block the other; intake fans out too).

### Changed

- **`_deliver` and `_preview_and_confirm` now take a per-channel `messages` dict** instead of
  a single string, so one run serves multiple channels. Channel→sender dispatch is a small
  `_sender_for` function (call-time name resolution, mirroring `_collect_for`), keeping the
  senders monkeypatchable and avoiding an import-time capture bug.

Phase 2 — the **structured lane**: report-ready signals that skip the LLM entirely (only
raw git activity is ever summarized by Claude). A run now collects from every enabled
signal, merges them into one sectioned message, and previews/sends it once.

### Added

- **Tasks collector** (`collectors/tasks.py`) — reports items newly checked off in a
  Markdown checklist (`- [x]`) at a per-project `tasks_file`. Structured lane (no LLM).
  Its marker is the full current completed set, so an already-reported item never repeats.
  (Items are identified by text — see `docs/known-issues.md` KI-6.)
- **Notes collector** (`collectors/notes.py`) — sends a hand-written `notes_file` when its
  content changes (a content-hash "replace model"; see KI-7). Structured lane (no LLM).
- **`orion intake <project>`** — sends a pushed/hand-written update (`--message` or stdin),
  the same entry point the Phase-6 Claude session skill will use. No collector, no LLM, no
  delta marker (a push, not a delta); still runs two-pass redaction + preview-before-send.
- **Merge step** (`merge.py`) — combines per-collector bodies into one report with titled
  `## ` sections, skipping empty ones. One preview, one delivered message per run.
- **Per-collector state markers** (`state.py`) — a generic `collector_markers`
  (project, collector) table replaces the single `last_commit`, so each signal tracks its
  own delta independently. Pre-Phase-2 git markers are migrated automatically on open.
- **Config** — `collectors` now accepts `"tasks"`/`"notes"`; each requires its file path
  (`tasks_file`/`notes_file`) only when enabled, resolved relative to the config file.
- **Tests** — 90 total (+38): the two collectors, the merge helper, per-collector markers
  and backfill, the multi-collector orchestrator (incl. a test proving the structured lane
  never calls the summarizer), and the `intake` command.

### Changed

- **`cmd_report` is now multi-collector** — it loops over the enabled signals, redacts each
  (pass 1), summarizes only the raw lane, merges, redacts the merged body (pass 2), then
  previews/sends once and advances each collector's marker only after a successful send.
  The Anthropic client is built lazily, so a structured-only run needs no API key.
- **Delivery extracted** into a shared `_deliver` helper used by both `report` and `intake`.
- **`ReportBlob.source_marker` is vestigial** (always `""`) now that markers are
  per-collector; the legacy `project_state.last_commit` column is kept only as the
  migration source (see KI-8).

## [0.1.0] — 2026-06-15

First end-to-end slice (Phase 1 MVP): `orion report <project>` reads git activity since
the last report, redacts secrets, conditionally summarizes with Claude, previews in the
terminal, and on confirmation posts to a Discord webhook — then advances per-project
state so the next run only covers what is new.

### Added

- **Report pipeline** wired end to end in `src/orion/`:
  `config → secrets → state → git collect → redact → summarize (Haiku) → redact →
  compose → preview/confirm → Discord (urllib) → advance state`.
- **TOML config** (`config.py`) — a per-project registry (repo path, share level,
  collectors, named recipients). Recipients name an env var holding their webhook URL,
  so the config itself never contains a secret.
- **sqlite3 state store** (`state.py`) — last-reported commit per project + a redacted
  report history. State advances *only* after a successful send.
- **Git collector** (`collectors/git.py`) — a hybrid payload (commit messages +
  diffstat + a capped, secret-filtered diff). Sensitive paths (`.env`, `*.pem`, keys,
  `credentials*`, …) are excluded from the diff at collection time.
- **Two-pass redaction** (`redact.py`) — scrubs API keys, tokens, JWTs, PEM blocks, and
  secret-ish assignments; runs once before the LLM and once before sending, and reports
  a hit count for the preview.
- **Conditional summarizer** (`summarize.py`) — one Claude Haiku 4.5 call on the raw
  lane only; the lane seam is built so Phase 2's structured lane is purely additive.
- **Preview-before-send gate** (`cli.py`) — default-no confirmation showing the
  redaction count; any pipeline error aborts before send (fail-closed).
- **Discord delivery** (`delivery/discord.py`) — a single JSON POST via stdlib
  `urllib.request`, with length truncation and uniform error handling.
- **Human-friendly date line** in composed messages — e.g. `June 15, 2026 · 1:32 AM
  UTC` — while the stored timestamp stays canonical ISO 8601.
- **Test suite** — 52 tests covering config, state, secrets, redaction (a secret-format
  corpus), the git collector, the summarizer (dependency-injected client), compose, and
  an end-to-end CLI run against a real temporary repo.
- **`docs/test-messages.md`** — a log of real messages delivered during testing,
  organized by channel.

### Fixed

- **Redaction hit count was double-counting** a single secret. A value caught by a
  specific pattern (e.g. `sk-…`) that also sat in a secret-ish-named assignment
  (`token = "sk-…"`) was re-matched by the generic `NAME=value` catch-all, which ate the
  just-inserted `[REDACTED_API_KEY]` token — inflating the count (2 for 1) and mangling
  the placeholder. *Why it mattered:* the "⚠ N secrets redacted" notice is part of the
  human-gate trust signal, so an inflated count is misleading. *Fix:* a negative
  lookahead (`(?!\[REDACTED_)`) so the catch-all skips placeholders an earlier pattern
  already inserted. No leak either way — this corrected the count and cosmetics only.
- **Discord delivery failed with HTTP 403** against real webhooks. The request sent no
  `User-Agent`, and Discord's edge (Cloudflare) blocks the default `Python-urllib/x.y`
  agent. *Why it mattered:* delivery is Phase 1's only outbound step, so this broke the
  one feature that has to work live. *Fix:* send a descriptive `User-Agent` header
  (Discord's API requires one); pinned by a delivery test asserting a non-default agent.

[Unreleased]: https://github.com/yooleee/Orion/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/yooleee/Orion/releases/tag/v0.1.0

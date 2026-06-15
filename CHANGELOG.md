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

_Nothing yet — Phase 2 (the structured, no-LLM lane) will land here._

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

[Unreleased]: https://example.com/orion/compare/v0.1.0...HEAD
[0.1.0]: https://example.com/orion/releases/tag/v0.1.0

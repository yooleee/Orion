# Phase B3 Kickoff — Richer message rendering (Slack Block Kit + Discord embeds)

> **Read this, then [`plans/orion-plan.md`](../../plans/orion-plan.md) in full, before doing
> anything.** Unlike the Phase 4 kickoff, **B3's design is NOT pre-settled** — only the *framing*
> is (from KI-9). Do a **full plan-mode pass**: surface the open decisions below with a
> recommendation for each, settle them with Yousuf, then build checkpoint by checkpoint, stopping
> at each boundary for review.

## Where things stand (as of 2026-06-16)

- **Horizon A** (A1–A4) shipped & signed off; **Horizon B**: B1 (git-hook triggers), B2 (Claude
  Code session skill), and B6 (read-only config-inspect commands) are signed off and pushed.
  `origin/main` is in sync; `pytest`: **148/148**.
- All four ingestion signals (git, tasks, notes, sessions) feed Orion. Delivery is dual-channel
  (Discord + Slack) with per-recipient routing. **B3 is next** in Horizon B; B4 (summarizer
  flexibility) and B5 (conditional scheduling layer) follow.
- Doc map: roadmap + design → [`plans/orion-plan.md`](../../plans/orion-plan.md); shipped →
  [`CHANGELOG.md`](../../CHANGELOG.md); open concerns → [`known-issues.md`](../known-issues.md)
  (esp. **KI-9** and **KI-10**); test catalog → [`testing.md`](../testing.md).

## What B3 is

Replace today's **plain-string** messages with **richer per-channel rendering** — **Slack Block
Kit** and **Discord embeds** — done **together** as one "structured rendering" upgrade. It is a
*presentation* change: no new signal, no new cadence. The goal is nicer-looking supervisor
updates without weakening any privacy guarantee.

## Settled framing — do NOT re-litigate (from KI-9 / KI-10)

- **Pair them.** Block Kit and richer Discord formatting (embeds) ship **together**, as one
  upgrade — not Slack-only.
- **Why it was deferred from Phase 3:** Block Kit breaks the current **"message is one string"**
  pipeline (`compose(blob, channel) -> str`, a string preview, a string send), it would
  *re-derive* structure that `merge.py` currently flattens into `##`-titled markdown, and it adds
  block-size limits. So B3 is exactly the deliberate change that earns that complexity.
- **Likely shape (KI-9's own hint):** a **small `ReportBlob`/`compose` change to carry structured
  sections** (title + body per signal) so blocks/embeds build naturally instead of being
  re-parsed out of flattened markdown.
- `compose._to_slack_mrkdwn` (KI-10) is a *structural* translator for the two constructs Orion
  emits (`#`/`##` → `*bold*`, `**b**` → `*b*`); a structured path may make parts of it moot.

## Open decisions to settle in plan mode (the heart of B3)

Surface each with a recommendation; settle with Yousuf before coding:

1. **Where structure lives.** Carry structured sections (`[(title, body), …]`) on `ReportBlob`
   alongside (or instead of) the flat `body` string? Recommended starting point: **add** the
   structured sections to the blob and keep the flat `body` as the canonical redacted text +
   fallback, so nothing downstream that expects a string breaks.
2. **The preview.** The preview must still show **what will actually be sent**. With blocks/embeds
   the sent payload is JSON, not the markdown string. Decide how to preview faithfully (render a
   readable text approximation? show the block structure?) — preview-before-send is a permanent
   rule, so this must stay trustworthy.
3. **Redaction over structured content.** Redaction currently runs on the flat string. It **must**
   still scrub every piece of text that ends up in any block/embed field. Decide whether to
   redact before composing into blocks (so structure is built from already-redacted text — likely
   cleanest) and how the two passes map onto the structured path.
4. **Opt-in vs always-on, and fallback.** A config toggle (e.g. per-project `rich` rendering) vs
   always rich? And the graceful fallback to plain `content`/`text` (block-size overflow, an
   unsupported case). Keep it simple — bias to a sensible default.
5. **Per-channel payloads.** Discord embeds (`{"embeds": […]}`) and Slack Block Kit
   (`{"blocks": […]}`) are different JSON shapes than today's `{"content": …}` / `{"text": …}`.
   Decide how `delivery/discord.py` and `delivery/slack.py` accept a structured payload while
   keeping their `send(...)` contract and `DeliveryError` handling. Mind block-size limits.

## Seams / files likely involved (confirm in plan mode)

- `src/orion/compose.py` — the main change (string → structured rendering per channel).
- `src/orion/report.py` (`ReportBlob`) — if structured sections are carried on the blob.
- `src/orion/merge.py` — currently flattens sections to one `##`-titled body; may instead carry
  structure through.
- `src/orion/delivery/discord.py` + `slack.py` — embed/Block-Kit JSON payloads + size limits.
- `src/orion/cli.py` — `_preview_and_confirm` (faithful preview of the new payloads).
- Tests + docs (`README`, `CHANGELOG`, `plans/orion-plan.md`, `docs/testing.md`, resolve **KI-9**
  and update **KI-10** as appropriate).

## Security / safety must-holds (non-negotiable)

- **Redaction on every path** — both passes still scrub all text that reaches any block/embed
  field. A structured payload must not become a redaction bypass.
- **Preview-before-send stays trustworthy** — the human must still see what actually leaves the
  machine (now a richer payload). `--yes`/`auto_send` and `intake --yes` gates are unchanged.
- **No secrets in payloads**; the portable report/intake blob stays portable (UTF-8, no
  machine-local paths) for the Horizon-C seam.

## How to work this phase (project rules)

- **Plan before code:** a real plan-mode pass (this phase has genuine design decisions). Surface
  the open decisions above with recommendations; settle them; then build.
- **Smallest reviewable unit**; checkpoint after each and wait for review.
- **Keep docs living**; every change made cross-platform-minded (Windows / macOS / Linux).
  Dev is on **Windows 11 + WSL2**; live-test delivery to the real Discord + Slack test channels.
- **Sign-off pattern:** implement (mark "awaiting sign-off"), then a separate "Sign off Phase B3"
  commit flips the markers. Commit/push only when Yousuf asks.

## First commands to run next session

```bash
# Confirm the baseline is still green (expect 148 passing).
.venv/bin/python -m pytest -q
```

Then read [`plans/orion-plan.md`](../../plans/orion-plan.md) (roadmap + the B3 row) and
[`known-issues.md`](../known-issues.md) KI-9/KI-10, and start the **plan-mode pass**.

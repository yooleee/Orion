# Orion — Test Message Log

A record of real messages Orion has delivered during testing, captured verbatim from
the send path. This is a reference for how reports actually render in each channel and
a log of what has been exercised end to end.

## Conventions

- **Grouped by delivery channel.** Discord today; Slack arrives in Phase 3.
- **Newest message first** within each channel.
- Each entry has a short **metadata** block followed by the **verbatim delivered
  message** in a code block — shown exactly as sent (Discord renders the Markdown).
- Metadata fields: *When* (UTC), *Project*, *Scenario*, *Share level*, *Secrets
  redacted*, *Result*.

---

## Discord

### 2026-06-15 · demo · Phase 2 intake push

- **When:** 2026-06-15 20:05 UTC
- **Project:** demo (throwaway Phase-2 test project)
- **Scenario:** `orion intake` — a pushed/hand-written update via `--message`. No collector,
  no LLM, no marker. Exercises the structured-lane intake path (the Phase-6 skill's entry point).
- **Share level:** — (intake has no share level)
- **Secrets redacted:** 0
- **Result:** Delivered (exit 0).

```text
**Progress update — demo**
_June 15, 2026 · 8:05 PM UTC_

Phase 2 sign-off: intake push working end to end.
```

### 2026-06-15 · demo · Phase 2 merged report (git + tasks + notes)

- **When:** 2026-06-15 20:05 UTC
- **Project:** demo (throwaway Phase-2 test project)
- **Scenario:** First `orion report` with all three collectors enabled. One delivered
  message with three titled sections: git (Haiku-summarized, raw lane) + completed tasks +
  notes (both passed through, no LLM). Exercises the multi-collector loop and the merge step.
- **Share level:** high_level
- **Secrets redacted:** 0
- **Result:** Delivered (exit 0). Immediate re-run reported "No new activity" (per-collector
  markers advanced correctly).

```text
**Progress update — demo**
_June 15, 2026 · 8:05 PM UTC_

## Code activity
The developer added a new feature module with initial implementation. This appears to be an early-stage addition that establishes the foundation for upcoming feature development.

## Completed tasks
- Build the structured-lane collector contract
- Add the intake command

## Notes
Phase 2 is feature-complete: tasks, notes, intake, and the merge step all land
this session. Haiku summaries still look good on real diffs — no step-up needed.
```

### 2026-06-15 · orion-live · Incremental update

- **When:** 2026-06-15 02:06 UTC
- **Project:** orion-live (throwaway test repo)
- **Scenario:** Second report — two new commits since the last report (added a
  `.gitignore` and a `summarize()` helper). Exercises the incremental delta (only new
  commits reported) and the friendly date format.
- **Share level:** detailed
- **Secrets redacted:** 1 — the removed `sk-` debug line still appears in the diff as a
  deletion and is scrubbed on the way out; the sent body is clean.
- **Result:** Delivered (exit 0).

```text
**Progress update — orion-live**
_June 15, 2026 · 2:06 AM UTC_

## Summary

Two commits addressed code cleanup and project hygiene:

**Environment and gitignore management:** Created a `.gitignore` file to prevent accidental tracking of sensitive files, including environment variables (`.env`), Python caches, temporary files, and IDE-specific directories. Removed the previously committed `.env` file from version control.

**Feature module refactoring:** Simplified the `feature()` function and added a new `summarize()` helper function. The summarize function takes a list of items and returns a formatted string reporting the count of items processed. Removed legacy debugging code from the feature module.

These changes improve project security by preventing credentials from being tracked and make the codebase cleaner by removing temporary debug code.
```

### 2026-06-15 · orion-live · Initial report

- **When:** 2026-06-15 01:32 UTC
- **Project:** orion-live (throwaway test repo)
- **Scenario:** First report for the repo (full history) — initial project structure.
- **Share level:** detailed
- **Secrets redacted:** 1
- **Result:** Delivered (exit 0).
- **Note:** This send predates the friendly-date change, so it shows the raw ISO 8601
  timestamp — kept here for historical accuracy.

```text
**Progress update — orion-live**
_2026-06-15T01:32:53+00:00_

Added initial project structure to the orion-live repository with documentation and a feature module. The commit includes a new README describing the repository's purpose as a test environment for the live report path, along with a basic Python feature module that returns a fixed value. Configuration files were also initialized.
```

### 2026-06-15 · Connectivity test (raw webhook)

- **When:** 2026-06-15, during delivery debugging.
- **Project:** — (not a pipeline run)
- **Scenario:** A raw webhook POST with a custom `User-Agent`, used to confirm the fix
  for Discord's Cloudflare 403 (the default `Python-urllib` UA is blocked). Not a full
  report — a one-off connectivity check.
- **Share level:** —
- **Secrets redacted:** —
- **Result:** Delivered (HTTP 204).

```text
Orion delivery test (custom UA)
```

---

## Slack

### 2026-06-15 · demo · Phase 3 intake push (redacted, Slack rendering)

- **When:** 2026-06-15 21:18 UTC
- **Project:** demo (throwaway Phase-3 test project)
- **Scenario:** `orion intake` with a seeded fake AWS key in the body, delivered to a Slack
  recipient (and a Discord recipient) in one run. Confirms the structured/intake lane is
  redacted before send on Slack too.
- **Secrets redacted:** 1 (the `AKIA…` key → `[REDACTED_AWS_KEY]`).
- **Result:** Delivered to both channels (exit 0).

```text
*Progress update — demo*
_June 15, 2026 · 9:18 PM UTC_

Phase 3 sign-off: dual-channel routing live. Oops [REDACTED_AWS_KEY] slipped in.
```

### 2026-06-15 · demo · Phase 3 dual-channel report (Slack rendering)

- **When:** 2026-06-15 21:17 UTC
- **Project:** demo (throwaway Phase-3 test project)
- **Scenario:** First `orion report` for a project with a Discord *and* a Slack recipient.
  Same report content routed to both; this is the **Slack mrkdwn** rendering — note `*bold*`
  header and `*Code activity*` section title (no literal `##`), vs the Discord version which
  uses `**bold**`/`## …`.
- **Share level:** high_level
- **Secrets redacted:** 0
- **Result:** Delivered to both channels in one send (`Sent to: Alex (discord), Sam (slack)`).

```text
*Progress update — demo*
_June 15, 2026 · 9:17 PM UTC_

*Code activity*
A new feature module was added with minimal initial implementation (2 lines of code), establishing the foundation for upcoming feature development.
```

### 2026-06-15 · Slack connectivity check (direct sender)

- **When:** 2026-06-15, during Phase 3 live verification.
- **Project:** — (not a pipeline run)
- **Scenario:** A direct `delivery.slack.send` call to confirm the test-workspace incoming
  webhook works, isolating connectivity from the full pipeline (mirrors the Phase-1 Discord
  connectivity check).
- **Result:** Delivered (HTTP 200 "ok").

```text
Orion Phase 3 — Slack connectivity check
```

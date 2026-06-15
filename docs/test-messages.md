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

_No messages yet — Slack delivery is planned for Phase 3._

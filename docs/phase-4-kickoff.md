# Phase 4 Kickoff — Scheduled digests (cadence)

> **Read this, then [`plans/orion-plan.md`](../plans/orion-plan.md), before doing anything.**
> Unlike the earlier kickoffs, **Phase 4 is already planned and its design decisions are
> SETTLED** (below, agreed with Yousuf 2026-06-15) — the heavy plan-mode work is done. Do a
> *brief* plan-mode re-confirmation pass (re-ground the plan against the current `cli.py` /
> `config.py`, surface anything that drifted), then build **checkpoint by checkpoint**. Do not
> re-open the settled decisions without a concrete reason.

## Where things stand (as of 2026-06-15)

- **Phases 1, 2, 3, 3.5 are signed off, committed, and pushed** to `origin/main`.
  `pytest`: **115/115**.
- The shipped product: `python -m orion report <project>` and `orion intake <project>` —
  collect from enabled signals (git + tasks + notes), redact (two passes), summarize only the
  raw git lane with Haiku, **preview-before-send**, then deliver to each recipient's Discord
  and/or Slack webhook with per-recipient routing. Runs **natively on Windows, macOS, Linux**
  (`python -m orion` is the canonical invocation; a console UTF-8 guard makes redirected output
  safe).
- **Versioning is pre-release / phase-tracked** — no SemVer numbers yet; phases are the
  progression markers (see `CHANGELOG.md`). Source version is the placeholder `0.0.0`.
- Doc map: shipped → [`CHANGELOG.md`](../CHANGELOG.md); design + phases →
  [`plans/orion-plan.md`](../plans/orion-plan.md); open concerns →
  [`known-issues.md`](known-issues.md); test catalog → [`testing.md`](testing.md); manual
  cross-OS runbook → [`portability-smoke-test.md`](portability-smoke-test.md).

## What Phase 4 is

Make Orion runnable **unattended** so an OS scheduler can produce digests **without weakening
preview-before-send**. It does **NOT** build a scheduler — Phase 3.5 settled that Orion
delegates cadence to each OS's native tool (cron / systemd timer / launchd / Task Scheduler)
and documents it per-OS. "Digest" needs no new aggregation concept: each run already reports
the **delta since the last report**, so a job that fires daily naturally yields a daily digest
(projects with no new activity send nothing). The relevant open question was recorded as
**KI-12** (unattended runs vs. the preview gate); Phase 4 resolves it.

## Settled decisions (do NOT re-litigate)

1. **Unattended send = `--yes` flag + per-project `auto_send` opt-in — BOTH required** to
   bypass the preview. A `--yes` run of a project **without** `auto_send` is **skipped +
   logged**, never sent. Without `--yes`, every run previews as today (`auto_send` ignored — a
   human is present). Defense in depth; matches the privacy rule and KI-12.
2. **Cadence lives in the OS scheduler ONLY** — no Orion cadence/`schedule` config field (it
   would be a no-op Orion can't enforce). A cadence-aware `report --all --due` filter is
   **deferred** as a future enhancement.
3. **Redaction is untouched.** Both passes still run in every path; the empty-after-redaction
   refusal still applies. Unattended send relaxes **no** redaction — it only bypasses the
   human preview, for opted-in projects.
4. **`intake` is unchanged** — a manual/skill push is inherently attended. `--yes` / `--all`
   apply to `report` only.

## The build — 5 checkpoints (smallest reviewable units; stop after each)

1. **`config.py` `auto_send` field.** Add `auto_send: bool = False` to `ProjectConfig`; parse
   + type-validate in `_parse_project` (mirrors `share_level`). Keep parsing pure (no I/O).
   Tests: parses true / false / default / invalid-type.
2. **`cli.py` `--yes` non-interactive single-project path.** Extract the per-project pipeline
   into `_run_report(project, conn, assume_yes) -> status`
   (`status ∈ {SENT, NO_ACTIVITY, SKIPPED_NOT_OPTED, ABORTED, FAILED}`). The preview step:
   `assume_yes and project.auto_send` → skip `_preview_and_confirm`, log "Auto-sending
   <project>"; `assume_yes and not auto_send` → `SKIPPED_NOT_OPTED` + note, no send; else →
   today's `_preview_and_confirm`. Add `--yes` to the `report` subparser. **Plus the
   security-critical tests (below).**
3. **`cli.py` `report --all`.** Make `project` optional (`nargs="?"`), add `--all`; validate
   exactly one of `{project, --all}`. Loop `config.projects.values()`, call `_run_report`
   per project, **fail-soft** (a per-project error → `FAILED`, continue) — the per-recipient
   fail-soft in `_deliver` is the model. Final summary ("N projects: X sent, Y no activity,
   Z skipped, W failed"). **Exit code non-zero only on a real `FAILED`** (skips / no-activity
   are exit 0 with a clear message, so cron alerts on real failure only). Tests.
4. **Docs.** Document `auto_send` (default `false`; preview stays on unless `auto_send=true`
   **and** `--yes`; recommend `share_level = "high_level"` for unattended projects), `--all`,
   and `--yes` in `README.md` + `orion.toml.example`. New **`docs/scheduling.md`** per-OS
   runbook (cron / systemd timer / launchd / Task Scheduler) for wiring
   `python -m orion report --all --yes`, with the **WSL2 caveat** (cron runs only while WSL is
   up → use Windows Task Scheduler to invoke the WSL command, or run native) and the
   minimal-environment gotchas (absolute paths, the venv's python, `git` on PATH). Short
   README "Scheduling" section points to it.
5. **Living docs + verification.** `plans/orion-plan.md` Phase 4 status + phase-table flip;
   `CHANGELOG.md` Phase 4 entry; `docs/known-issues.md` **resolve KI-12** (it moves to the
   changelog per that file's lifecycle) and record the deferred `--due` filter as a new KI;
   `docs/testing.md` note the new auto-send/scheduling coverage + link `scheduling.md`.

## Security-critical tests (the reason CP2 exists — must all hold)

- `--yes` + `auto_send=true` → delivers **without** calling `input()`, **and a seeded fake key
  is still redacted** in the sent body (redaction firm under auto-send).
- `--yes` + `auto_send=false` → **NOT sent** (skipped + noted, no delivery call).
- **No `--yes`, `auto_send=true` → still previews** (`input()` is called) — config alone never
  bypasses the preview. *This is the load-bearing safety test.*
- `report --all --yes` → fail-soft across projects; only `auto_send` projects deliver.

## Files to touch

- `src/orion/config.py` (the `auto_send` field) · `src/orion/cli.py` (the main change:
  `_run_report` refactor, `--yes`, `--all`).
- `orion.toml.example`, `README.md`, new `docs/scheduling.md`.
- Tests: `tests/test_config.py`, and `tests/test_cli.py` (or a new `tests/test_schedule.py`).
- Living docs: `plans/orion-plan.md`, `CHANGELOG.md`, `docs/known-issues.md`, `docs/testing.md`.

## Reuse (don't rebuild)

`load_config` / `config.projects` / `get_project` (`config.py`) for `--all`;
`_preview_and_confirm` / `_deliver` / `_channels` and the existing pipeline body (`cli.py`) —
the refactor **wraps** these, it does not replace them; `redact` (both passes) and
`open_state` / `set_marker` / `record_report` are unchanged.

## How to work this phase (project rules, do not skip)

- **Plan before code**, but lightly: the detailed plan is done — a *re-confirmation* plan-mode
  pass against current code, then build. Surface any drift; otherwise proceed to CP1.
- **Smallest reviewable unit**; checkpoint after each and wait for review.
- **Keep docs living**; every change made cross-compat-minded (Windows / macOS / Linux).
- **Security overrides everything**: redaction firm on every path; the preview is bypassed
  **only** with both `--yes` **and** `auto_send`; carry the data-leak/redaction test on the new
  unattended surface (above).

## Environment note

Dev is on **Windows 11 + WSL2**; a MacBook may now be in hand. Phase 4's `docs/scheduling.md`
and the unattended-send path are natural candidates for a cross-OS smoke pass (pair with
[`portability-smoke-test.md`](portability-smoke-test.md)). Target all three OSes by
reasoning/docs, not only the machine in hand.

## First commands to run next session

```bash
# Confirm the baseline is still green (expect 115 passing).
.venv/bin/python -m pytest -q
```

Then read `plans/orion-plan.md`, do the brief plan-mode re-confirmation, and start **CP1**
(the `config.py` `auto_send` field + its tests).

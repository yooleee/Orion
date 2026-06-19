# Phase 3.5 Kickoff — Cross-platform portability pass

> **Read this, then read [`plans/orion-plan.md`](../../plans/orion-plan.md) before doing
> anything.** This is a fast orientation; the plan is the source of truth for architecture and
> phasing. Phase 3.5 is a deliberate insert *before* Phase 4 (scheduled digests).

## Where things stand (as of 2026-06-15)

- **Phases 1–3 are signed off, committed, and pushed** to `origin/main`. `pytest`: **105/105**.
- The **on-demand reporting core is complete**: `orion report <project>` collects from enabled
  signals (git + tasks + notes), redacts, summarizes only the raw git lane with Haiku, merges,
  previews per channel, and delivers to each recipient's **Discord and/or Slack** webhook with
  per-recipient routing. `orion intake <project>` sends a pushed/hand-written update.
- Orion tracks **its own repo** (`[projects.orion]`, now with a Discord *and* a Slack recipient),
  and has live-reported on itself dual-channel.
- Backward/forward docs: shipped → [`CHANGELOG.md`](../../CHANGELOG.md); design + phases →
  [`plans/orion-plan.md`](../../plans/orion-plan.md); open concerns → [`known-issues.md`](../known-issues.md);
  delivered messages → [`test-messages.md`](../test-messages.md).

## Why Phase 3.5 comes before Phase 4

- Orion is going **open-source and must run on Windows, macOS, and Linux**.
- **Scheduling (Phase 4) and git-hooks (Phase 5) are the most platform-divergent features.**
  The WSL2 trigger made this concrete: cron only runs while WSL is up, so a Linux-cron-only
  scheduler would be unreliable and non-portable. Settling the cross-platform stance *first*
  de-risks Phase 4's design instead of forcing a redesign later.
- **Standing principle (new, 2026-06-15):** every change from here on is made with
  cross-compatibility in mind — not one OS at a time. Slower coding, far fewer future issues.
  Now recorded in `CLAUDE.md` and the plan's guiding principles.

## What Phase 3.5 is (an audit + targeted fixes + a stance — NOT a rewrite)

The core is **already mostly portable**: pure stdlib, `pathlib` for paths, and a deliberately
platform-safe timestamp in `compose.py` (built from components to avoid `%-I` vs `%#I`). This
pass keeps it that way and closes the concrete gaps:

1. **Audit** platform assumptions across the code (paths, the venv entry point, `subprocess`
   git calls, console encoding, line endings).
2. **Fix** the known gaps:
   - Docs hardcode `.venv/bin/orion`; native Windows venvs use `.venv\Scripts\orion.exe`.
     Consider adding `src/orion/__main__.py` so **`python -m orion …` works identically on
     every OS** (the cleanest portable invocation).
   - TOML path guidance for Windows (`\` is a TOML escape — use forward slashes or literal
     single-quoted strings) in `orion.toml.example` + README.
   - Console Unicode (`✓ ⚠ · — ✅`) on legacy Windows consoles — verify, and guard/fallback if
     it can raise `UnicodeEncodeError`.
   - Document per-OS requirements (Python 3.11+, `git` on PATH).
3. **Establish a support matrix** + a "tested on" statement in the README.
4. **Decide the cross-platform scheduling stance** — the key *input to Phase 4*. Recommended:
   Orion ships **no scheduler of its own**; it provides a clean non-interactive
   `orion report --all` and **documents** cron / `launchd` / Task Scheduler per OS. This keeps
   Orion itself platform-agnostic and sidesteps the WSL2-cron problem.

## Open decisions to surface in plan mode (bring a recommendation for each)

1. **Support-matrix scope:** native Windows (cmd/PowerShell) *or* Windows-via-WSL only? Native
   Windows is the most inclusive but has the most divergence (paths, `Scripts/`, console);
   WSL-only is much simpler (it's Linux). Decide explicitly — it scopes the whole pass.
2. **Canonical invocation:** add `python -m orion` (via `__main__.py`) as the portable entry
   point the docs lead with? (Lean: yes.)
3. **Console Unicode:** enforce UTF-8 / provide an ASCII fallback / document-only?
4. **Carried to Phase 4 (not 3.5):** the preview-before-send vs. auto-send tension for
   *unattended* scheduled runs — almost certainly an explicit per-project opt-in, with all
   redaction guarantees still firm. Flagged here so 3.5 leaves room for it but doesn't build it.

## How to work this phase (project rules, do not skip)

- **Plan before code** (plan mode, no edits): file-by-file breakdown + the open decisions above,
  each with a recommendation.
- **Smallest reviewable unit**; checkpoint after each.
- **Keep docs living**; make every change cross-compat-minded.
- **Security overrides everything** — redaction on both lanes, preview-before-send default-on.
  Each phase carries a data-leak/redaction test on any new surface.

## Environment note

Dev is on **Windows 11 + WSL2** now (likely for ~the next day), with a **MacBook expected in the
coming days**. The portability work plus a second-OS smoke test is the natural validation — but
the pass should target all three OSes by reasoning/docs, not only the machine in hand.

## First commands to run next session

```bash
# Confirm the baseline is still green (expect 105 passing).
.venv/bin/python -m pytest -q
```

(Optional live connectivity checks for Discord/Slack are in the Phase 1/2/3 notes; the suite
being green does not prove live delivery, but Phase 3.5 is about portability, not delivery.)

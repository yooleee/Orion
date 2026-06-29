<!-- =========================================================================
docs/oss-showcase-readiness-kickoff.md
---------------------------------------------------------------------------
Responsible for: The grounded kickoff for making the Orion repo presentable as a
                 PUBLIC SHOWCASE — clean enough that a recruiter / hiring manager who
                 clones or browses the GitHub repo gets a strong, private-data-free
                 impression. This is Horizon P (Publish / OSS-launch), reframed around
                 the near-term "showcase for job applications" goal.
Role in project: Read this at the START of the OSS-showcase session, THEN do the
                 plan-mode pass (a scrub inventory) before editing. Captures the
                 grounded state, the prioritized work, and the open decisions — NOT a
                 finished plan. Model: docs/supervisor-interaction-loop-kickoff.md.
========================================================================= -->

# Kickoff: OSS / public-showcase readiness (Horizon P, showcase-framed)

> **Opening prompt for the next session:**
> Read `docs/oss-showcase-readiness-kickoff.md` first. Start with a plan-mode pass that
> produces the **personal-reference scrub inventory** — a categorized `git grep` sweep
> (scrub vs. keep-as-attribution vs. decide). Settle the open decisions in the doc (above
> all: attribution-vs-anonymity, and whether to reframe the "supervisor/family" narrative
> to neutral "stakeholders/collaborators" on public surfaces). Then run the scrub slice by
> slice, polish the README for a cold professional reader, and finish with a
> secret-scanner pass to confirm the git history is clean. The OSS scaffolding
> (LICENSE / CONTRIBUTING / SECURITY / templates / CI) already exists — the real work is
> the scrub + the README first impression.

## Where we are (grounded 2026-06-29)

The goal is narrow and concrete: **someone reviewing your job application can open the
GitHub repo and come away impressed, with no private or family data visible.** That is a
showcase bar, not a "build a thriving OSS community" bar — so contributor-onboarding
polish ranks below a clean, professional, private-data-free repo and a strong README.

Good news from a scout of the actual repo: it is **further along than the roadmap's
Horizon P implies.** What already exists:

- **OSS scaffolding (P2) largely done:** `LICENSE`, `CONTRIBUTING.md`, `SECURITY.md`,
  `README.md` (with a CI badge and a "How it compares" positioning section), `CHANGELOG.md`,
  `.github/ISSUE_TEMPLATE/`, `.github/pull_request_template.md`. Only `CODE_OF_CONDUCT.md`
  is missing.
- **Config / secret hygiene solid:** `orion.toml` and `.env` are **not** tracked (only
  `orion.toml.example` and `.env.example` are). `.gitignore` covers `.env`, `*.sqlite3`,
  `.venv/`.
- **History appears secrets-clean:** `orion.toml` / `.env` were **never** committed. The
  only `discord.com/api/webhooks` references in history are placeholders in `.env.example`
  and `docs/new-project-setup.md`. No `sk-ant` (Anthropic key) strings in history. A proper
  scanner pass should still confirm this, but the obvious vectors are clean — **plan on NOT
  rewriting history.**
- **CI exists:** `.github/workflows/ci.yml` (the Actions quota cap is the known constraint,
  not a missing pipeline).

So the roadmap's Horizon P (P1 scrub / P2 scaffolding / P3 CI, all "decision-gated") is
**stale** — P2 and P3 are mostly shipped. Re-sync that table as part of this work.

## Goal

A repo a cold professional reader can browse and respect:
1. **No private / family / leaky data** in tracked files (the main job).
2. **A README that lands a strong first impression** in 30 seconds.
3. **Confirmed secrets-clean** (scanner pass, no history surgery expected).
4. *(Secondary)* fill the small scaffolding gaps (CoC) and tidy repo settings.

## The work, prioritized for a showcase

### 1. Personal-reference scrub (P1 — the dominant gap)

Tracked-file footprint (counts from `git grep -l`, 2026-06-29):

| Term | Tracked files | Nature | Lean |
| --- | --- | --- | --- |
| `Alex` / `Sam` | 35 / 37 | placeholder supervisor/recipient names (examples, tests, docs) | decide (keep as generic, or neutralize) |
| `Dad` | 9 (5 tests, 2 web, 1 plans, 1 docs) | **family reference** — mostly introduced THIS session as the discussion-loop example name | scrub → neutral |
| `sar_hackathon` | 9 (design, docs, plans, tests) | a **real private project name** used as an example | scrub → neutral |
| `/Users/...` | 9 (README, several docs, `orion.toml.example`, `src/orion/hooks.py`, `tests/test_hooks.py`) | **private absolute paths** (leak home dir + username) | scrub → generic (`/path/to/...`, `~/`) |
| `Yusuf` / `golding` / `yoolee` | 11 / 2 / 7 | **your identity** | **distinguish**: intentional authorship vs. incidental leakage |

**Honest note:** I introduced several `Dad` / `Yusuf` placeholders this session (discussion
tests, the SPA seed, the loop kickoffs). The scrub should treat those as in-scope, not assume
the repo was clean before.

### 2. README / first-impression polish

The README is already strong (positioning, incumbent comparison from D3). A showcase pass:
lead with what is impressive (the architecture, the multi-signal pipeline, the React/Vite
dashboard, the breadth), and reconsider the **"supervisor / family" origin framing** for a
professional audience (see open decision 3).

### 3. Secret-scan (must-do, likely clean)

Run a real scanner (e.g. `gitleaks` or `trufflehog`) over the working tree **and** history
to confirm. Expect clean. Only if it finds a real leak does history surgery enter scope.

### 4. Secondary / nice-to-have

`CODE_OF_CONDUCT.md`; a quick correctness pass on the existing scaffolding; repo settings
(branch protection, secret-scanning) — these are GitHub-settings, not code, and matter less
for a showcase than for an active OSS project.

## File / component map

- **Where refs live:** examples (`orion.toml.example`, `.env.example`,
  `docs/new-project-setup.md`), the `docs/` planning corpus (kickoffs, `known-issues.md`,
  `parallelization.md`, `feature-ideas-reconciliation.md`), `plans/orion-plan.md`, the
  `design/` HTML mockups, the test suites (`tests/`, `web/src/**/*.test.tsx`), and a couple
  of source files (`src/orion/hooks.py`, paths in docstrings).
- **The `docs/` corpus is double-edged for a showcase:** the depth of planning (this very
  doc included) reads as rigor and process maturity — a *plus* — but it also holds most of
  the personal refs. Keep it (it is a selling point) and scrub within it (see decision 5).

## First concrete steps

1. **Plan-mode pass → scrub inventory.** A full `git grep -n` sweep for the agreed terms,
   each hit bucketed: **scrub** (family/private/path), **keep** (intentional attribution),
   **decide** (placeholders, the narrative). No edits yet — produce the inventory + settle
   the open decisions first.
2. **Settle the open decisions** below (they shape every replacement).
3. **Execute the scrub slice by slice** (examples/docs, then tests, then source), running
   the suites after the test renames so nothing breaks.
4. **README first-impression polish.**
5. **Secret-scanner pass** + re-sync the roadmap Horizon P table.

## Open decisions (settle in the plan pass — recommendation each)

1. **Attribution vs. anonymity.** A showcase tied to YOUR applications wants your name on
   it. **Recommendation: keep intentional authorship** (your name, the `yooleee/Orion`
   GitHub URL, commit-author identity) and scrub only **incidental** identity leakage —
   the home-dir username inside absolute paths, email fragments in non-author content.
2. **Placeholder convention.** Pick ONE neutral example set and apply it repo-wide:
   supervisor/recipient names, and the example project name (`sar_hackathon` → e.g.
   `demo-project`). **Recommendation: a single consistent neutral convention** (it also
   makes the scrub verifiable with one final grep).
3. **The supervisor / family narrative.** Orion's origin is "send my dad progress updates
   so he can supervise my work." **Recommendation: neutralize to "stakeholders /
   collaborators / reviewers" on public-facing surfaces** (README, top-level docs) for a
   clean professional read, while a brief honest origin line is fine. Decide how far to
   carry this into internal docs (lower stakes there).
4. **Git history.** **Recommendation: leave as-is** — it is clean of secrets, and a rich,
   genuine commit history is a *plus* for a showcase (it shows real iterative engineering).
   Revisit only if the scanner finds a real leak.
5. **The `docs/` planning corpus.** **Recommendation: keep** (the rigor showcases well),
   scrub the personal refs within, and consider a one-line note at the top of the docs
   index explaining it is the project's internal planning record so a reader frames it
   right. Pruning is an option if any doc is purely private.

## Verification

- A final `git grep` for the agreed scrub terms returns **only intended hits** (e.g. your
  authorship, neutral placeholders).
- A **secret-scanner pass is clean** over tree + history.
- **Backend + web suites still green** after the placeholder renames in tests.
- A **cold read of the README** lands the value in ~30 seconds (read it fresh, or have
  someone unfamiliar skim it).
- `git log` spot-check reads as professional (no obviously private/embarrassing subjects).

**Gotchas:** renaming placeholders in tests must keep assertions consistent (the names are
often asserted on — change both sides). Run backend tests with `PYTHONPATH=src`. The live
config is untracked, so nothing to scrub there — the leak surface is examples/docs/tests,
not real config.

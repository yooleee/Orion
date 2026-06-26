<!-- =========================================================================
docs/consolidation-slice-kickoff.md
---------------------------------------------------------------------------
Responsible for: The kickoff for the post-Inc-2.6 consolidation slice — one
                 dashboard-visibility fix plus three deferred CLI cleanups.
Role in project: Read it at the START of the next session, THEN build unit by
                 unit (each is its own PR; stop for review at each boundary).
                 The forks are already settled here. Roadmap: plans/orion-plan.md
                 (Horizon E table + E2 ladder). Built on: E2 Inc 2.6 (PR #50) +
                 the CSRF fix (PR #51). Deploy: docs/deployment.md. Access model:
                 docs/dashboard-auth.md.
========================================================================= -->

# Kickoff: consolidation slice — dashboard-home visibility + add-project completeness + KI-8 cleanup

## Why

E2 Inc 2.6 (the status-aware `tracker` collector + `tasks_file` bootstrapping) and the CSRF comment fix
shipped to `main`; the `applications` tracker is wired and its checklist is pushed live. This slice
clears deferred debt and one gap surfaced while checking the live dashboard: **a checklist-only project
does not appear on the portfolio home**, so family has no link to the applications tracker — which
defeats the point of the work just shipped. Fix that first, then three CLI consolidation items.

Four independent units, **each its own reviewable PR**. Coupling map: **Unit 1** is relay-only (needs a
`fly deploy`); **Units 2 + 3** share the `add-project` code (`cli.py` / `scaffold.py`) so they pair into
one PR; **Unit 4** is state/report only. No two units fight over the same files, so they could be built
in parallel — but per the repo's sequential-review discipline, do them as separate PRs in the order at
the bottom. (Line numbers below are from `main` at kickoff time; treat them as starting points, they
will have drifted.)

---

## Unit 1 — Dashboard home shows checklist-only projects (relay; do FIRST)

**Problem (confirmed by reading the code):** `GET /` builds cards from `latest_report_per_project()`
(`relay/store.py:322`), whose query is `FROM relay_reports` — so a project with a live checklist but
**zero reports** (exactly `applications`: dashboard-only, no git, never `report`-ed) produces no row →
no card → no entry point. The data is stored (`relay_project_checklists`) and `/project/applications`
already renders it via `get_checklist` (`relay/server.py:580`); only the home overview misses it.

**Fix — make the home query project-driven, not report-driven:**
- `relay/store.py` `latest_report_per_project()` (the SQL ~lines 352–377): rewrite so the row set is the
  **union of projects that have a report OR a checklist**, LEFT-JOINing the latest-report fields (which
  become NULL for a checklist-only project). Also select the checklist's `pc.updated_at` (new) so the
  card has a "last activity" time when there's no report. Order by `COALESCE(latest_generated_at,
  checklist_updated_at) DESC, project ASC` (freshest first; both are ISO-8601 UTC, so lexical =
  chronological). Keep the existing done/total precompute. Return `report_count = 0` (via `COALESCE`)
  and a new `checklist_updated_at` field for the checklist-only case.
- `relay/render.py` `render_portfolio()` (~lines 477–506): tolerate a card with no report.
  `_headline(latest_body)` already omits the headline when the body is falsy (so pass `None` safely);
  the meta line must use `latest_generated_at` when present, else `checklist_updated_at`, and show
  "0 report(s)" without calling `_time_tag(None)` (today it passes `latest_generated_at`
  unconditionally). The checklist badge already renders from `checklist_done/total`.
- **No schema change** (both tables exist). Relay-only → after merge, **`fly deploy`** to take effect.

**Tests** (`tests/test_relay_store.py`, `tests/test_relay_server.py`, `tests/test_render.py`): a
checklist-only project (push a checklist, ingest no report) appears in `latest_report_per_project()` and
on the rendered `GET /` with its badge and a `/project/<name>` link; a report-only project and a
report+checklist project still render as before (no regression); ordering interleaves the two kinds by
recency.

---

## Unit 2 — `add-project` wires the tracker/incubator file args (CLI)

**Problem:** `add-project` only accepts `--tasks-file` / `--notes-file`;
`scaffold.render_project_stanza`'s `collector_files = {"tasks": ..., "notes": ...}`
(`src/orion/scaffold.py:213`) has no `incubator`/`tracker` key, so `add-project --collectors tracker`
would `KeyError`. Had this existed, `applications` wouldn't have needed hand-editing of `orion.toml`.

**Fix (mirror the existing `--tasks-file`/`--notes-file` pattern):**
- `src/orion/cli.py` add-project arg parser (near `--tasks-file`/`--notes-file`): add `--tracker-file`
  and `--incubator-file` (the latter already exists for `graduate-idea` — same dest names).
- `cmd_add_project` (signature ~line 1685) and the `args.command == "add-project"` dispatch (~line 729):
  thread `tracker_file` / `incubator_file` through.
- `src/orion/scaffold.py` `render_project_stanza` (signature ~line 158; `collector_files` ~line 213):
  add `tracker_file` / `incubator_file` params and dict entries. The COLLECTOR_FILE_KEYS-driven loop
  (~line 230) then emits them automatically.
- **No file creation** for tracker/incubator (unlike Inc 2.6 Unit B-i's tasks default): these point at
  rich user docs, so add-project only wires the config path.

**Tests** (`tests/test_add_project.py`, `tests/test_scaffold.py`): `add-project --collectors tracker
--tracker-file X` produces a loadable stanza with `tracker_file = X`; same for incubator; prior
tasks/notes behavior unchanged.

---

## Unit 3 — `add-project --seed-tasks-from <doc>` (the deferred Inc 2.6 Unit B-ii)

**What:** when `add-project` creates a defaulted `tasks_file` (the Inc 2.6 Unit B-i flow), optionally
**seed its content from a roadmap/doc's Markdown tables** instead of the empty starter checklist.
Structured parse, **no LLM** (the parse-vs-generate fork, already settled as parse).

**Fix:**
- New `--seed-tasks-from <path>` arg on `add-project` (threads through `cmd_add_project`).
- Reuse `src/orion/collectors/_markdown.py` `parse_tables` (shipped in Inc 2.6 — the DRY seam this was
  designed for). For each table, pick the **text column** by a documented preference list (first header
  matching, case-insensitive: `task`, `scope`, `sub-goal`, `item`, `milestone`, `name`) and an optional
  **status column** (`status`); emit `- [ ] <text>` per row, `- [x]` when the status cell contains a
  done-marker (`✅`, `done`, `shipped`, `signed off`, `complete`, `[x]`). A doc with no usable table →
  warn and fall back to the empty starter checklist (never fail the add).
  - **Done-marker robustness (decided 2026-06-25):** the seed source is an arbitrary user doc in a
    public tool, so the marker match is hardened past a literal substring test: `✅` and `[x]` match as
    substrings, but the word markers match on **word boundaries** (so `incomplete` ≠ `complete`) and a
    standalone `not` in the cell vetoes a done match (so `not done` stays open). Implemented in
    `cli._status_is_done`.
- Lives in `cmd_add_project`'s creation step (where Unit B-i writes `_starter_checklist`): when
  `--seed-tasks-from` is given, write the generated lines instead. Preview-gated and never-overwrite,
  same as Unit B-i.

**Tests** (`tests/test_add_project.py`, plus a focused seeding-helper test): a roadmap-shaped table seeds
checkbox lines with correct done/open mapping; a no-table doc falls back to the empty starter; the
generated file is a valid checklist the `tasks` snapshot reads back.

> Units 2 + 3 are the same code area → **one PR**, Unit 2 first (smaller, structural), then Unit 3.

---

## Unit 4 — KI-8: drop the vestigial state columns (state/report)

**Confirmed a clean drop:** `project_state.last_commit` / `last_reported` are read ONLY by the
idempotent one-time backfill `_backfill_git_markers` (`src/orion/state.py:118–142`); the live source of
truth is `collector_markers`. `ReportBlob.source_marker` is always `""` and the relay already treats it
as optional (not in `_REQUIRED_STR_FIELDS`, `relay/server.py:95`).

**Fix:**
- `src/orion/state.py`: remove the `project_state` table from the schema; delete `_backfill_git_markers`
  and its call in `open_state` (~line 114). **Deliberate decision to state in the PR:** this closes the
  Phase-1→Phase-2 migration window (Phase 2 shipped 2026-06-15; all live DBs have long since
  backfilled). Make the choice explicit, not silent.
- `src/orion/report.py`: remove the `source_marker` field from `ReportBlob` (~line 69), its
  `build_report` param (~line 85), and from `serialize_blob`'s payload (~line 161). The relay tolerates
  its absence, so the wire change is backward-compatible.
- **Tests:** delete the two backfill tests (`tests/test_state.py:111–154`); drop `source_marker=` args /
  `"source_marker"` keys from `test_report_compose.py`, `test_report_serialize.py`,
  `test_relay_store.py`, `test_relay_server.py`.

> Independent of Units 1–3 → its **own PR**.

---

## Verification

- Per unit: `PYTHONPATH=src pytest` green (run from the main checkout per the worktree gotcha).
- **Unit 1 eyes-on (the point of the slice):** after merge + `fly deploy`, open
  `https://orion-relay-horizon-c.fly.dev/` logged in as admin and confirm the **applications** card now
  appears (badge + link) alongside the report-bearing projects; click through to `/project/applications`.
  Confirm a checklist-only card shows a sensible "last activity" time (the checklist's `updated_at`).
- **Units 2/3:** run `add-project` against a scratch repo with `--collectors tracker --tracker-file …`
  and with `--seed-tasks-from plans/orion-plan.md`; inspect the written stanza and the generated
  checklist; `orion check` validates.
- **Unit 4:** full suite green; a fresh `open_state()` creates no `project_state` table; a report still
  serializes/ingests without `source_marker`.

## Sequencing

CI is quota-capped until 2026-07-01 → verify on local-green, merge, then deploy (Unit 1 only). Code via
branch + PR (`main` is PR-gated). Order: **Unit 1 (relay + deploy)** → **Units 2 + 3 (add-project, one
PR)** → **Unit 4 (KI-8, own PR)**. Stop for review at each PR boundary. Family-viewer provisioning stays
out of this slice (ops, awaiting the family members' names).

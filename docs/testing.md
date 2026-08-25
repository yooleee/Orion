<!-- =========================================================================
testing.md
---------------------------------------------------------------------------
Responsible for: Explaining Orion's automated test suite — what categories of
                 tests exist, WHY each is necessary, how to run them, and what
                 is deliberately not covered.
Role in project: The living map of the test suite. Update it in the same session
                 as any test change (new phase, new category, closed/opened gap)
                 so it never drifts from `tests/`.
Companions: the manual cross-OS checks live in `portability-smoke-test.md`, and the
harness for exercising real code paths against real projects lives in
`dogfood-harness.md`.
========================================================================= -->

# Testing Orion

Orion's safety story is "redact, then **preview before send**," and its delivery is the one
thing that has to work live. The test suite exists to protect exactly those load-bearing
guarantees — secrets never leak, the right model runs on the right lane, state advances only
after a real send, and the tool runs the same on every OS. This doc is the map: the
**categories** of tests, **why each is necessary**, and how to run them.

As of this writing (2026-07-18): **785 tests across 38 files**, all passing. The only test
dependency is **pytest** (the lone `[dev]` extra) — everything else is the standard library,
matching Orion's minimal-dependency principle. Shared end-to-end setup (the real-repo builder,
the config writer, the mock fixture, the scripted-`input` helper, and the isolation guards)
lives in `tests/conftest.py` and is reused across the CLI, schedule, intake, and relay-facing
end-to-end files.

## Running the suite

```bash
python -m pip install -e ".[dev]"   # once, in an activated venv — adds pytest
python -m pytest                    # run everything (OS-agnostic invocation)
python -m pytest tests/test_redact.py -q          # one file
python -m pytest -k redact                         # by keyword
```

`python -m pytest` (not a bare `pytest`) is used everywhere so the command is identical on
Linux, macOS, and Windows — see `portability-smoke-test.md`.

## Testing philosophy (what we mock, and why)

The end-to-end tests run **almost the whole real pipeline** — real config parsing, real SQLite
state, real git collection against real temporary repos, real redaction, real compose/merge.
Only three things are ever mocked, each for a specific reason:

- **The summarizer** (the B4 seam — `cli._build_summarizer`, via the `use_summary` helper in
  `conftest.py`) — so the suite needs no Anthropic API key and makes no network call, and so the
  test controls the exact "model output" (useful for the pass-2 redaction test, which seeds a
  leaked key *into* the model's reply).
- **The network POST** (`discord_send` / `slack_send`) — so no real webhook is hit; the fake
  records what *would* have been sent, which is what we assert on.
- **`input()`** — so the preview/confirm gate is scripted (`y` / `n`) without a human.

Everything else runs for real, because the bugs that matter (a secret surviving redaction, a
marker advancing on a failed send, a path that breaks on Windows) live in the real code paths,
not in mocks. The summarizer backends' own unit tests use **dependency injection** rather than
monkeypatching the seam: the Anthropic backend takes a fake client, and the local backend runs
against a patched `urllib.request.urlopen` (a fake HTTP response) — both mirroring how `cli.py`
builds the configured backend lazily, with no network and no key.

## Categories

| Category | Files | What it protects / why it's necessary |
|---|---|---|
| **Security gate** | `test_redact.py`; the `.env`/subdir/noise exclusion in `test_git_collector.py`; the pass-2 redaction cases in `test_cli.py` / `test_intake.py`; the redaction-under-auto-send case in `test_schedule.py` | The #1 principle: secrets never reach the LLM or a channel. If these fail, Orion is not shippable. |
| **Pure-logic units** | `test_merge.py`, `test_report_compose.py`, `test_secrets.py`, `test_summarize.py`, `test_console_encoding.py` | Leaf-module correctness with no I/O — fast, exact-output assertions. Includes the B3 rich rendering (`test_report_compose.py`: Discord embed / Slack Block Kit structure, the payload + faithful-preview pairing, per-channel bold dialect, and both overflow→plain fallbacks) and the B4 summarizer seam (`test_summarize.py`: both backends behind the `Summarizer` Protocol — the Anthropic backend uses the configured model and wraps API errors; the local backend POSTs the OpenAI-compatible shape with the shared security prompt, sends Bearer auth only when keyed, and fails closed on every transport/shape error). |
| **File-I/O collectors & store** | `test_tasks_collector.py`, `test_notes_collector.py`, `test_incubator_collector.py`, `test_config.py`, `test_state.py` | Parsing local files (TOML, checklists, notes, idea-pipeline tables) and persisting per-`(project, collector)` deltas, including the Phase-1→2 marker migration. |
| **Real-git integration** | `test_git_collector.py` | The actual `subprocess` git behavior against real temp repos — delta detection, share-level gating, and the collection-time secret/noise filters. |
| **Delivery** (network-mocked) | `test_delivery.py`, `test_slack_delivery.py` | The per-channel POST contract — Discord 204 vs Slack 200, the User-Agent that fixes Discord's 403, and that delivery is **pure transport**: it POSTs the compose-built payload dict as-is (rendering + truncation now live in `compose`, B3). |
| **End-to-end pipeline** | `test_cli.py`, `test_intake.py` | The orchestration: multi-collector loop, lane separation (structured signals never call the LLM), per-recipient routing, fail-closed state advancement. |
| **Unattended send** (Phase 4) | `test_schedule.py` | The scheduled-run safety contract: the preview is bypassed only with `--yes` **and** `auto_send` (config alone never sends); `--all` is fail-soft and exits non-zero only on a real failure; redaction still fires on the auto-send path. |
| **Event-driven hooks** (B1) | `test_hooks.py` | The generated hook's safety properties (delegates to `report --yes`, backgrounded, always `exit 0`, forward-slash paths) without executing a real hook; `resolve_hooks_dir` against a real repo; and the `install-hook` command (writes an executable hook, honors `--hook`, refuses to clobber without `--force`, `--print` writes nothing, warns when not opted in). |
| **Config inspect** (B6) | `test_inspect.py` | The read-only `projects`/`show`/`check` commands: they print the right facts, fail cleanly on a bad config / unknown project, and **never print a secret value** (`check` reports webhook/API vars by name as set/MISSING, with a non-zero exit when a required one is missing). B4 adds the backend-aware key check: a keyless local summarizer is ready with no Anthropic key, while a keyed local endpoint's named var is flagged MISSING when unset. |
| **Portability** (Phase 3.5) | `test_cli_entry.py`, `test_console_encoding.py` | The `python -m orion` entry point resolves on every OS, and the console UTF-8 guard never crashes on a redirected/odd stream. |
| **Later structured signals** (D, E2) | `test_tracker_collector.py`, `test_disciplines_collector.py`, `test_extract.py`, `test_markdown_extract.py` | The status-aware tracker's checklist parsing, and the disciplines pipeline: markdown extraction from a project's own docs plus the opt-in, cache-gated LLM step (the extraction call is injected/faked — no key, no network). |
| **Onboarding & visibility CLI** (Horizon D) | `test_add_project.py`, `test_scaffold.py`, `test_status.py` | `add-project`'s explicit, append-only config writing (Orion still never rewrites user TOML), project scaffolding, and the `status` backlog digest derived from `report_history`. |
| **Report blob contract** | `test_report_serialize.py` | `serialize_blob` is the portable wire format every hosted surface consumes — all fields and the tuple→array conversions pinned losslessly. |
| **Relay: store, auth, API, derive** (C/E2) | `test_relay_store.py`, `test_relay_server.py`, `test_relay_api.py`, `test_relay_derive.py` | The hosted half: the additive self-migrating SQLite store; per-user auth (cookie sessions + stateless revocation, Bearer principals, one generic 401, out-of-scope = 404 existence-hiding); the read-only JSON API serializers; and the derived views (effective checklist across producers, per-producer slippage, scheduling buckets, milestone slip counts). |
| **Relay push** | `test_relay_delivery.py` | The producer→relay outbound seam: verbatim blob body, Bearer header, and every failure translated to a non-fatal `DeliveryError` (network mocked, mirroring `test_delivery.py`). |
| **Retirement / migration tools** | `test_migrate_comments.py`, `test_drop_retired_tables.py` | The one-time ops tools stay safe to re-run: the idempotent comment→discussion migration with its lossless collapse guard, and the backup-first, allowlisted table drop. |
| **Test-isolation guards** | `test_isolation.py` | Pins the conftest guards themselves: no real network leaves the test process, and the real `.env` / secret env vars never bleed into a test — the structural reason the suite cannot leak a secret. |
| **Manual / hardware** | `portability-smoke-test.md` (not pytest) | Native Windows / macOS validation that can't run in CI on one machine. |
| **Manual / dogfood** | `dogfood-harness.md` (not pytest) | Running real commands against real projects with state, webhooks and the relay redirected to disposable local copies. Catches what fixtures cannot — see the coverage gap below. |

### Why the security gate is its own category

`test_redact.py` is the corpus from the plan's "security release gate": one test per real
secret shape (AWS, GitHub, Google, Slack, `sk-`/`sk-ant-`, JWT, PEM block, generic
`NAME=value`), plus the false-positive guard (ordinary prose with the word "secret" survives)
and the double-count regression. It is defense **layer one**; the human preview is the
guaranteeing layer (`docs/known-issues.md` KI-3). Because redaction is one regex list, an
untested pattern is a silent-regression risk — so every pattern in `redact._PATTERNS` has a
test. The git collector adds the *collection-time* half of the guarantee: a sensitive file's
**content is never read into the diff at all** (denylist), at the repo root or in a
subdirectory, even at `share_level = "detailed"`.

### Why end-to-end tests carry security assertions too

Defense in depth is only real if it's tested at the seams. `test_cli.py` seeds a fake key into
the *mocked model's reply* and asserts the **pass-2** redaction (the net before send) scrubs
it — proving a leak introduced *after* collection is still caught. `test_intake.py` does the
same for a pushed body — including on the **`intake --yes`** path (the session skill's
non-interactive send), proving that skipping the preview there does not skip redaction either.
`test_schedule.py` repeats it on the **auto-send** path. These pin that redaction runs on the
exact bytes that leave the machine, not just on collected input.

Both preview-skip flags also carry the *opposite* proof — that the human gate isn't silently
removed. `test_schedule.py` (report) and `test_intake.py` (intake) each use an `input()`
**tripwire** to show `--yes` runs never prompt, and a spy to show that **without** `--yes` the
preview *is* shown. The flag is the only bypass on either command.

The unattended path also needs the *opposite* proof — that the human gate is **not** silently
removed. `test_schedule.py` uses an `input()` **tripwire** (a fake that raises if called) to
prove `--yes`+`auto_send` runs never prompt, and a recording spy to prove that `auto_send`
**without** `--yes` *does* still prompt. The second is the load-bearing test: config alone must
never bypass the preview.

## Known coverage gaps & intentional non-targets

Tracked honestly so "green" doesn't read as "everything is covered":

- **`load_secrets`'s Orion-specific logic *is* tested; the dotenv plumbing is not.** Its
  config-relative `.env` discovery (find the `.env` beside `--config` from any working directory)
  and its override=False precedence (a real env var beats the file) are pinned in
  `test_secrets.py`, because that behavior is what makes git-hook/scheduled runs find secrets. We
  do not separately test that `dotenv.load_dotenv` reads a file — that would exercise the library,
  not Orion. The accessor it feeds (`get_required`) *is* fully tested.
- **Per-command CLI coverage is uneven, and the suite cannot see the gap.** The DF1 dogfood
  sweep (2026-07-21) found three bugs a green suite had no way to catch, and the structural
  reason is the same each time: coverage follows *whichever* command a behavior was first built
  for, not every command that shares the behavior. `report` had a test proving it skips push-only
  collectors; `status` and `baseline` walked the same list with no such test, and both crashed on
  the real config. `cmd_disciplines_push` had **no CLI-level test at all**, so nothing noticed it
  pushing an empty set over live data. When you fix a bug in one command, check whether a sibling
  command shares the code path — and prefer a test that walks a **constant** (as
  `test_every_report_collector_has_a_dispatch_branch` does) over one that names a case, so new
  entries are covered without anyone remembering to add a test.
- **`compose()`'s unknown-channel fall-through (KI-5) is not pinned in `test_report_compose.py`** —
  by design. Config validation (`test_config.py::test_unknown_channel_is_rejected`) makes that
  branch unreachable, so the guard lives upstream. If a third channel is ever added to config
  but not to `compose`, that test is where the gap would surface.
The KI-8 schema migration has since shipped: the legacy `last_commit` column, the one-time
git-marker backfill, and the vestigial `source_marker` field are gone, and the two
legacy-backfill tests were removed with them — `test_state.py` now pins that the drop left no
references behind.

## Keeping this living

When you add or change tests, update this doc in the same session:

- New signal/channel/phase → add its tests to the right category row (and add a row if it's a
  genuinely new category).
- Closed a gap → remove it from "Known coverage gaps"; opened one → add it there.
- New manual check → put the runbook in `portability-smoke-test.md` (cross-OS) or
  `dogfood-harness.md` (real-project exercise) and reference it here.

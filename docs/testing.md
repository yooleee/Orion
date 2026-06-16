<!-- =========================================================================
testing.md
---------------------------------------------------------------------------
Responsible for: Explaining Orion's automated test suite — what categories of
                 tests exist, WHY each is necessary, how to run them, and what
                 is deliberately not covered.
Role in project: The living map of the test suite. Update it in the same session
                 as any test change (new phase, new category, closed/opened gap)
                 so it never drifts from `tests/`.
Companion: the manual cross-OS checks live in `portability-smoke-test.md`.
========================================================================= -->

# Testing Orion

Orion's safety story is "redact, then **preview before send**," and its delivery is the one
thing that has to work live. The test suite exists to protect exactly those load-bearing
guarantees — secrets never leak, the right model runs on the right lane, state advances only
after a real send, and the tool runs the same on every OS. This doc is the map: the
**categories** of tests, **why each is necessary**, and how to run them.

As of this writing: **126 tests across 17 files**, all passing. The only test dependency is
**pytest** (the lone `[dev]` extra) — everything else is the standard library, matching
Orion's minimal-dependency principle. Shared end-to-end setup for the CLI tests (the real-repo
builder, the config writer, the mock fixture, and the scripted-`input` helper) lives in
`tests/conftest.py`, so `test_cli.py` and `test_schedule.py` reuse one copy.

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

- **The LLM call** (`summarize_raw`) — so the suite needs no Anthropic API key and makes no
  network call, and so the test controls the exact "model output" (useful for the pass-2
  redaction test, which seeds a leaked key *into* the model's reply).
- **The network POST** (`discord_send` / `slack_send`) — so no real webhook is hit; the fake
  records what *would* have been sent, which is what we assert on.
- **`input()`** — so the preview/confirm gate is scripted (`y` / `n`) without a human.

Everything else runs for real, because the bugs that matter (a secret surviving redaction, a
marker advancing on a failed send, a path that breaks on Windows) live in the real code paths,
not in mocks. The summarizer's own unit test uses **dependency injection** (a fake client
passed in) rather than monkeypatching, mirroring how `cli.py` builds the client lazily.

## Categories

| Category | Files | What it protects / why it's necessary |
|---|---|---|
| **Security gate** | `test_redact.py`; the `.env`/subdir/noise exclusion in `test_git_collector.py`; the pass-2 redaction cases in `test_cli.py` / `test_intake.py`; the redaction-under-auto-send case in `test_schedule.py` | The #1 principle: secrets never reach the LLM or a channel. If these fail, Orion is not shippable. |
| **Pure-logic units** | `test_merge.py`, `test_report_compose.py`, `test_secrets.py`, `test_summarize.py`, `test_console_encoding.py` | Leaf-module correctness with no I/O — fast, exact-output assertions. |
| **File-I/O collectors & store** | `test_tasks_collector.py`, `test_notes_collector.py`, `test_config.py`, `test_state.py` | Parsing local files (TOML, checklists, notes) and persisting per-`(project, collector)` deltas, including the Phase-1→2 marker migration. |
| **Real-git integration** | `test_git_collector.py` | The actual `subprocess` git behavior against real temp repos — delta detection, share-level gating, and the collection-time secret/noise filters. |
| **Delivery** (network-mocked) | `test_delivery.py`, `test_slack_delivery.py` | The per-channel POST contract — Discord `content`/204 vs Slack `text`/200, the User-Agent that fixes Discord's 403, and length truncation. |
| **End-to-end pipeline** | `test_cli.py`, `test_intake.py` | The orchestration: multi-collector loop, lane separation (structured signals never call the LLM), per-recipient routing, fail-closed state advancement. |
| **Unattended send** (Phase 4) | `test_schedule.py` | The scheduled-run safety contract: the preview is bypassed only with `--yes` **and** `auto_send` (config alone never sends); `--all` is fail-soft and exits non-zero only on a real failure; redaction still fires on the auto-send path. |
| **Portability** (Phase 3.5) | `test_cli_entry.py`, `test_console_encoding.py` | The `python -m orion` entry point resolves on every OS, and the console UTF-8 guard never crashes on a redirected/odd stream. |
| **Manual / hardware** | `portability-smoke-test.md` (not pytest) | Native Windows / macOS validation that can't run in CI on one machine. |

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
same for a pushed body. `test_schedule.py` repeats it on the **auto-send** path, proving that
skipping the *human* preview does not skip *redaction*. These pin that redaction runs on the
exact bytes that leave the machine, not just on collected input.

The unattended path also needs the *opposite* proof — that the human gate is **not** silently
removed. `test_schedule.py` uses an `input()` **tripwire** (a fake that raises if called) to
prove `--yes`+`auto_send` runs never prompt, and a recording spy to prove that `auto_send`
**without** `--yes` *does* still prompt. The second is the load-bearing test: config alone must
never bypass the preview.

## Known coverage gaps & intentional non-targets

Tracked honestly so "green" doesn't read as "everything is covered":

- **`load_secrets` is intentionally not unit-tested.** It is a one-line wrapper over
  `dotenv.load_dotenv`; a test would exercise the library, not Orion. The accessor it feeds
  (`get_required`) *is* fully tested.
- **`compose()`'s unknown-channel fall-through (KI-5) is not pinned in `test_report_compose.py`** —
  by design. Config validation (`test_config.py::test_unknown_channel_is_rejected`) makes that
  branch unreachable, so the guard lives upstream. If a third channel is ever added to config
  but not to `compose`, that test is where the gap would surface.
- **`test_report_compose.py` uses a non-empty `source_marker` fixture** while the field is
  vestigial in production (always `""`, KI-8). This is deliberate: a distinctive value tests
  that `build_report` *passes through* whatever marker it's handed; `""` would weaken that.

When the deferred KI-8 schema migration finally drops `project_state.last_commit`, the two
backfill tests in `test_state.py` become removable — revisit them *then*, not before.

## Keeping this living

When you add or change tests, update this doc in the same session:

- New signal/channel/phase → add its tests to the right category row (and add a row if it's a
  genuinely new category).
- Closed a gap → remove it from "Known coverage gaps"; opened one → add it there.
- New manual check → put the runbook in `portability-smoke-test.md` and reference it here.

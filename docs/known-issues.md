# Known Issues & Cross-Phase Concerns

A lightweight tracker for bugs, design questions, and tech debt that are **not tied to a
single phase** — the local stand-in for an issue tracker until the project is hosted
(then these can migrate to GitHub Issues).

**Lifecycle:** an item lives in the open list while it is open. When it is resolved, its
full write-up moves to the relevant slice's section of [`CHANGELOG.md`](../CHANGELOG.md)
(Added / Changed / Fixed / Removed) and the open entry is removed from here, leaving a
one-line pointer in the **[Resolved](#resolved)** index at the bottom so the id and its
fate stay traceable. Phase-specific work belongs in
`plans/orion-plan.md`, not here.

**Fields per entry:** a stable id, a one-line title, the detail, *why it matters*, a
severity (low / medium / high), and a status (Open / Needs decision / Monitored /
Deferred).

> **DF1 dogfood sweep, 2026-07-21.** This list was worked end to end by *running* each
> entry's affected path against real projects (see [`dogfood-harness.md`](dogfood-harness.md)
> for the harness). Entries carrying an **"Exercised (DF1, 2026-07-21)"** line were actually
> reproduced or checked; the line says what was run and what happened. Three new bugs were
> found and fixed ([`CHANGELOG.md`](../CHANGELOG.md) → *DF1 dogfood sweep*), and one new
> entry was filed ([[KI-41]]).
>
> **Not exercised in this sweep**, so their status is unchanged and unconfirmed:
> **KI-10** (the Slack mrkdwn translator — real Haiku output in these runs contained no
> links, tables or code fences, so nothing tested its limits), **KI-16** (the local
> summarizer backend — needs a running local model), **KI-17** (configurable anonymity — a
> design question, not a reproducible path), **KI-21** (forward-store item identity — needs a
> tracker item renamed across pushes, which was cut for time), **KI-33** (the
> anonymous→identified slippage split — needs a producer straddling that cutover), and
> **KI-34** (project-page density — an IA judgment that S2.1 Unit 3 is already scoped to
> address). Recorded so the absence of an "Exercised" line reads as *not yet done*, never as
> *checked and fine*.

---

## KI-1 — Multi-recipient partial-failure policy

- **Detail:** When a project has several recipients and some deliveries fail, the current
  policy advances state if **at least one** send succeeds. The conservative alternative
  is to advance only if **all** succeed.
- **Why it matters:** Advancing on partial success means a recipient who failed never
  receives the activity that was covered by that run — it silently falls into the gap
  between reports. Advancing only on full success risks re-sending to recipients who
  already got it.
- **Decision (2026-06-18):** **Keep advancing on ≥1 success.** All-or-nothing was rejected
  because a *permanently* broken recipient (e.g. a dead webhook) would block state
  advancement forever and re-send to the working recipients on every run — a worse failure
  mode than the bounded gap of one missed delta. The accepted gap's proper fix is
  **per-recipient delivery state** (each recipient advances independently), which is
  premature while each channel has a single destination (see KI-11) and belongs with the
  **C3** multi-party/identity model. The behavior is pinned by
  `tests/test_cli.py::test_one_channel_failure_does_not_block_the_other` and commented at the
  marker-advance step in `cli._run_report`.
- **D5 nuance (2026-06-24):** per-recipient `signals` routing slices this gap finer. All
  active collectors' markers still advance on ≥1 successful send *of the run* — so if the
  only subscriber to a given signal fails while a different audience succeeds, that signal's
  marker advances though no subscriber received it. This is the same bounded, by-design gap at
  audience granularity, with the same proper fix (per-recipient/per-audience delivery state in
  C3). Not a new severity.
- **Exercised (DF1, 2026-07-21):** reproduced deliberately — one project, four recipients,
  one of them pointed at a sink returning HTTP 500. The run named the failed recipient
  (`✗ Supervisor B (broken): Discord webhook returned HTTP 500`), sent to the other three,
  advanced state, and said why: *"1 recipient(s) failed; state advanced because at least one
  delivery succeeded."* The decided policy holds and reports itself honestly. No change.
- **Severity:** medium
- **Status:** Decided (by-design); per-recipient state deferred → **C3** (with KI-11).

## KI-2 — Discord message truncation is lossy

- **Detail:** Messages over Discord's 2000-character limit are truncated with a marker
  rather than split across multiple messages.
- **Why it matters:** A long (but already redacted) report loses its tail. Splitting into
  multiple messages would preserve everything; truncation was chosen as the simplest
  correct behavior for Phase 1.
- **Exercised (DF1, 2026-07-21) — fires on ordinary use, not an edge case:** the first real
  report of `instruction-debugger` (52 commits, `high_level`) hit it immediately. The Discord
  recipient received **2000 characters, cut mid-word** (`- Unit 1: \`ch` + `… [truncated]`);
  the Slack recipient on the same run received all **3066**. Two supervisors, materially
  different content, roughly a third lost on one of them. **Detail not previously recorded:**
  at that size Discord also silently drops the *embed* format and falls back to a plain
  `content` string, so nothing on the sending side signals the loss — the preview shows the
  full report, and only the delivered payload is short. That asymmetry (Slack whole, Discord
  truncated, no warning either way) is a better argument for splitting than the original
  entry made.
- **Severity:** low
- **Status:** Deferred (enhancement). The DF1 evidence raises its practical priority: the
  trigger is a normal first report of a real project, not a pathological input.

## KI-3 — Redaction pattern set is inherently incomplete

- **Detail:** `redact._PATTERNS` is a living list of known secret shapes. No regex set can
  match every possible secret, so false negatives are always possible.
- **Why it matters:** Redaction is one layer of defense in depth, not a guarantee. The
  guaranteeing layer is the human preview-before-send. This entry exists to keep the risk
  visible and the pattern list under periodic review, and to resist any future change that
  would treat redaction as a sufficient control on its own.
- **Exercised (DF1, 2026-07-21):** ran the real `detailed`-share diff of this repo (30306
  characters of actual code and docs) through the live path. No false negative was observed —
  nothing secret-shaped got through — but the pass produced **two false POSITIVES**, which is
  a distinct problem now tracked as [[KI-41]]. Worth stating plainly for this entry: the
  sweep did not test the pattern set against a corpus of *deliberately planted* secret
  shapes, so "no false negative observed" here means "none in one real diff", not
  "confirmed complete". The entry's premise stands.
- **Severity:** ongoing / by-design
- **Status:** Monitored. See [[KI-41]] for the over-matching sibling.

## KI-4 — Haiku summary quality not yet empirically confirmed on varied diffs

- **Detail:** The summarizer uses Claude Haiku 4.5 (the lightest adequate model). It has
  produced good summaries on the small test repo, but has not been evaluated on large or
  messy real-world diffs.
- **Why it matters:** The plan commits to stepping up to Sonnet 4.6 *only if* Haiku
  visibly misses nuance. That decision should be made from real evidence, not assumed.
- **Capability floor (observed 2026-06-17):** the B4 local-backend verification ran a very small
  model (`qwen2.5:0.5b`) and produced noticeably worse, partly hallucinated summaries — confirming
  the working assumption that the summarizer needs roughly **Haiku-4.5-level capability** to be
  usable. Implication for the B4 local-model option: "lightest **adequate**" means a mid-capability
  model, not the tiniest (the `README` / `orion.toml.example` local-model guidance was reconciled
  to this). A **model-tier comparison** — cloud vs local, finding the cost/adequacy sweet spot — is
  worthwhile but **non-foundational**: a future experiment, not a phase.
- **Exercised (DF1, 2026-07-21) — first real evidence, and it is positive:** three real runs
  on two projects, no hallucination and no missed nuance observed.
  1. `instruction-debugger`, **52 commits**, `high_level` (messages + diffstat): correctly
     identified the governance/detector work, the "no global instruction file" finding, and
     the handoff, with outcome-level framing rather than a commit list.
  2. `orion-detailed`, 8 commits, **`detailed`** (a real 30k-character diff): explained
     KI-39's root cause — that `collectors` conflated report collectors with push-only
     capability flags — and the taxonomy fix, *without being told any of it*. That is the
     hardest thing asked of the summarizer so far and it landed.
  3. A small scratch repo: accurate, no padding.
  Caveats, so this is not overclaimed: one model version, one week of history, two repos,
  both of them documentation-heavy Python; no adversarial or multi-language diffs; quality
  judged by reading, not against a rubric or a Sonnet baseline. So this is **evidence for
  keeping Haiku**, not a completed evaluation. The model-tier comparison remains the
  non-foundational future experiment the entry already describes.
- **Severity:** low
- **Status:** Open, but **substantially answered in Haiku's favour** by the DF1 runs above.
  Keep Haiku; revisit only if a real diff visibly defeats it.

## KI-5 — `compose()` silently falls through for unknown channels

- **Detail:** `compose.compose()` returns Markdown for any unrecognized `channel` value
  rather than raising. Config validation already restricts `channel` to supported values
  upstream, so this is currently unreachable.
- **Why it matters:** It is a small latent foot-gun: if a future channel is added to
  config validation but not to `compose`, it would silently get Discord-Markdown formatting
  instead of an error. As of Phase 3, `compose` handles `discord` and `slack`; the
  fall-through still returns Discord Markdown for anything else (config restricts the value,
  so it stays unreachable). The same shape exists in `cli._sender_for` (raises on an unknown
  channel) — the two should stay in sync as channels are added.
- **Exercised (DF1, 2026-07-21):** confirmed still unreachable — loading a config with
  `channel = "teams"` is rejected at validation (*"recipient #1 has invalid channel='teams'.
  Supported now: ('discord', 'slack')"*), so no unknown value can reach `compose`. No change
  needed; the entry is correctly parked until a third channel exists.
- **Severity:** low
- **Status:** Open (revisit when a third channel is added).

## KI-6 — Tasks collector identifies checklist items by their text

- **Detail:** The tasks collector (`collectors/tasks.py`) tracks completed items by their
  TEXT, not a stable id or line position. Consequences: (a) **renaming** a completed item
  makes the old text "disappear" and the new text count as a brand-new completion;
  (b) two identical completed lines are **deduped** to one; (c) re-checking an item that
  was un-checked *after* it was already reported will not re-report it (the stored set
  still lists it as done).
- **Why it matters:** It is the simplest stdlib-only identity model and is correct for the
  common case (check a box once, move on). The edge cases are minor and surfacing them
  here keeps the behavior intentional rather than surprising. A stable id (e.g. an inline
  `<!-- id -->` marker) would remove the ambiguity but adds syntax the user must maintain —
  not worth it for Phase 2.
- **Exercised (DF1, 2026-07-21) — all three arms reproduced exactly as written:**
  (a) renaming a completed item from *"Write the parser"* to *"Write the parser module"*
  re-reported it as a brand-new completion; (b) two identical `- [x] Ship it` lines were
  deduped to a single reported item; (c) unchecking an already-reported item and re-checking
  it reported **nothing** (no "Completed tasks" section at all). The documentation is
  accurate and the behavior is defensible for the common case. Left as-is.
- **Severity:** low
- **Status:** By-design (documented limitation; revisit only if it bites in real use).

## KI-7 — Notes collector uses a replace model (whole-file resend on any change)

- **Detail:** The notes collector (added in Phase 2) treats `notes_file` as *the current
  note*: its marker is a content hash, so **any** change re-sends the **whole** file, not
  just the newly-added lines.
- **Why it matters:** This matches the intended "hand-written current update / pushed
  summary" framing — notes is a replace, not an append log. If a user instead treats the
  file as an append-only log, already-reported lines would resend. The append-log
  alternative (track a byte offset / line count) is deliberately not built; flagged so the
  semantics are a conscious choice.
- **Exercised (DF1, 2026-07-21) — first real run ever:** `notes` is enabled in **no** live
  project, so this was its first exercise outside the test suite. It works. The replace model
  reproduced exactly: appending one line (*"Also fixed the flaky test."*) re-sent the **whole**
  file, already-reported paragraph included. Correct per the entry's framing; the resend is
  visible enough that a user treating the file as an append log would notice at once.
- **Severity:** low
- **Status:** By-design (revisit if an append-log notes workflow is actually wanted).

## KI-10 — `_to_slack_mrkdwn` is a structural translator, not a full converter

- **Detail:** `compose._to_slack_mrkdwn` converts only the two Markdown constructs Orion
  emits — `#`/`##` header lines → `*bold*` lines, and `**bold**` → `*bold*`. It does not
  handle arbitrary Markdown (links, nested emphasis, code fences, tables, etc.). Since B3 it is
  applied to each **section body** before it goes into a Slack Block Kit `section` block (and to
  the plain `text` fallback), rather than to one flattened report string — same scope, same two
  constructs, just per section.
- **Why it matters:** Orion controls what reaches it (a section body is an LLM summary that may
  contain `**bold**`, or already-structured passthrough text), so a full converter would be
  speculative complexity. If a future source introduces other Markdown that must render in
  Slack, extend the translator then. Flagged so the scope is a conscious choice, not an
  oversight.
- **Severity:** low
- **Status:** By-design (extend only when a real case needs it).

## KI-11 — A `Recipient` is a destination, not a person (no dedupe-by-webhook)

- **Detail:** A `Recipient` names a delivery destination (channel + webhook), so two people
  watching one Slack channel should be modeled as **one** recipient. If a user instead
  configures two recipients pointing at the *same* resolved webhook URL, Orion will POST the
  same message to that webhook twice (a double-post). There is no dedupe-by-resolved-webhook.
- **Why it matters:** The current one-destination-per-channel setup never hits this, so a
  dedupe pass would be premature. The eventual direction is per-supervisor routing (different
  supervisors per project / task / to-do); when individual people become first-class
  recipients, add dedupe-by-resolved-webhook then. Recorded so the modeling choice is explicit.
- **Exercised (DF1, 2026-07-21):** reproduced directly — two recipients configured against
  the same env var double-posted the identical message to the one destination (two POSTs,
  same path, same body). Exactly as described, with no dedupe. Still the right deferral: the
  configuration that triggers it is one a user has to construct on purpose.
- **Severity:** low
- **Status:** Deferred (add with the per-supervisor routing model).

## KI-13 — Cadence-aware `report --all --due` filter deferred

- **Detail:** Phase 4 delegates *timing* to the OS scheduler: `report --all --yes` reports
  **every** project whenever the scheduler fires. There is no Orion-side notion of per-project
  cadence (e.g. "this project weekly, that one daily"). A `--due` filter — Orion reading a
  per-project schedule plus the last-report time and reporting only the projects due *now* — was
  considered for Phase 4 and deliberately deferred.
- **Why it matters:** Today, mixed cadences are expressed with multiple scheduler entries (one
  per cadence group), which works and keeps Orion stateless about timing. A `--due` filter would
  let a single scheduler entry serve mixed cadences, but it is the first piece of "cadence needs
  Orion's *own* state" — the exact point at which a built-in scheduling *layer* becomes the right
  call (see the plan's "When a built-in scheduling layer becomes right"). It needs a `schedule`
  config field and last-report-time gating, which is scope beyond Phase 4's goal of making
  unattended runs *safe*. Recorded so the deferral is a conscious choice, not an omission.
- **Gate decision (B5, 2026-06-17):** the B5 "build a scheduling *layer*?" gate was explicitly
  evaluated and the decision is to **defer and fold this into Horizon C**. While cadence = "run a
  command at time T," the OS scheduler (plus one entry per cadence group) still wins; the real
  need for in-Orion scheduling state likely arrives *with* the Horizon-C bidirectional listener
  that would host an always-on process, at which point an in-process scheduler is nearly free.
  **Implementation note for whoever builds it:** "last successful report time per project" is
  **derivable from the existing `report_history` table** (`SELECT MAX(sent_at) FROM report_history
  WHERE project = ?`), so **no new schema is needed**; a `schedule` field mirrors the
  `share_level` / `auto_send` validation in `config.py`, and `report --all` is the layering point
  for `--due`.
- **Severity:** low
- **Convergence (2026-06-18):** the deferred **light planning/tracking layer** (milestones/sprints/
  due-dates/at-risk; see "Horizon-C direction settled" in `plans/orion-plan.md`
  and the strategy doc) lands at the *same* "cadence needs Orion's own state" threshold — so it most
  naturally arrives **with** this scheduling layer and the Horizon-C stateful process, not separately.
- **Status:** **RESOLVED (E1.2 Unit 2, 2026-07-17).** `report --all --due` now reads each
  project's optional `cadence` config field (`config.py`, validated like `share_level` / `kind`)
  plus the last-report time — `MAX(sent_at)` over `report_history`, **no new schema**, exactly as
  the implementation note above prescribed — and reports only the projects due now. The rest are
  marked `NOT_DUE`, counted in the `--all` tally (numbers reconcile), and skipped before any
  collection or LLM call. Interval carries slack (daily ~20h, weekly ~6d) and is compared in UTC,
  so scheduler jitter / DST never skips a run. One daily entry with `--all --due --yes` now serves
  mixed cadences (see `docs/scheduling.md`).
- **B5 scheduling-*layer* stance (unchanged by this):** `--due` is precisely the *stateless* subset
  carved out of B5 — it needs only the already-existing `report_history`, no always-on process.
  What remains of B5 (activity-gating, quiet hours, per-recipient cadence, an in-process scheduler)
  still fails the 2026-06-17 gate, and shipping `--due` *removes* the strongest remaining motivation
  for a built-in scheduling layer (mixed cadences from one entry). Re-evaluate that layer only if an
  always-on Orion-side process (e.g. the Horizon-C listener) actually appears.

## KI-14 — `install-hook` installs one standalone hook per project; no chaining

- **Detail:** `orion install-hook` (B1) writes a single standalone hook file (e.g.
  `.git/hooks/pre-push`) that reports **one** project. It refuses to overwrite an existing hook
  without `--force`. Consequences: (a) a repo already using a **hook manager** (husky, the
  `pre-commit` framework, a custom `core.hooksPath`) needs manual integration — let the manager
  call the one `report --yes` line (see `--print`), since Orion deliberately doesn't try to chain
  or wrap an existing hook; (b) a single repo mapped to **multiple** Orion projects needs the hook
  edited by hand to report each.
- **Why it matters:** The single-file model is the simplest thing that works and is safe (no
  fragile hook-chaining logic, no silent clobbering). Chaining/awareness of hook managers is
  speculative complexity until a real user needs it. Recorded so the limitation is a conscious
  choice, and documented for users in [`git-hooks.md`](git-hooks.md).
- **Exercised (DF1, 2026-07-21) — installed and fired for real:** `--print` renders a
  reviewable script; installing writes the single standalone hook; a second install refuses to
  clobber and points at `--force`. A real `git push` then fired it. With `auto_send = false`
  it **fail-closed** correctly — *"--yes was given but auto_send is not enabled, so a preview
  is required and no human is present. Nothing sent; state unchanged."* — and after enabling
  `auto_send` the same push produced a complete unattended run (collect → summarize → redact →
  deliver → relay). Worth noting as a quality signal: the install output proactively warns when
  `auto_send=false` would make the hook a no-op, which is the kind of thing this sweep exists to
  confirm actually happens. The single-file limitation is unchanged and still the right call.
- **Severity:** low
- **Status:** By-design (revisit if hook-manager coexistence or multi-project-per-repo is actually
  wanted).

## KI-16 — Local summarizer targets the OpenAI-compatible shape, not a runtime's native API

- **Detail:** The B4 local backend (`summarize.LocalSummarizer`) speaks the OpenAI-compatible
  `POST {base_url}/chat/completions` shape rather than any runtime's native protocol (e.g.
  Ollama's own `/api/chat`, or llama.cpp's bespoke endpoints). The user points `base_url` at the
  OpenAI-compatible endpoint their server exposes (Ollama: `http://localhost:11434/v1`).
- **Why it matters:** This was a deliberate "engineered enough" call. The OpenAI chat shape is
  the common denominator across the popular local runtimes (Ollama, llama.cpp server, LM Studio,
  vLLM), so **one** stdlib-`urllib` code path serves all of them with **zero** new dependencies —
  versus building and maintaining a per-runtime adapter set, which would be speculative complexity
  for a single LLM step. The tradeoffs accepted: (a) a user whose server speaks *only* a native
  API (no OpenAI-compatible endpoint) can't use it as-is; (b) features exposed only through a
  native API (model pull/management, runtime-specific options) aren't reachable; (c) Orion sends
  `max_tokens` and a system message and reads `choices[0].message.content` — fields a
  non-conforming "compatible" endpoint could omit (handled by failing closed with a clear
  `SummarizerError`, not by guessing).
- **When this might change:** add a native-API backend (a new `provider` value behind the same
  `Summarizer` seam — additive, no rewrite) **only if** a real user runs a server with no
  OpenAI-compatible endpoint, or needs a native-only capability for summarization. Until then the
  single shape is the simplest thing that works. Recorded so the scope is a conscious choice, not
  an oversight.
- **Severity:** low
- **Status:** By-design (extend with a native-API backend only when a real case needs it).

## KI-17 — Reports carry no author/submitter identity (anonymous in a multi-user setting)

- **Detail:** The portable blob names `participants` (who *receives* a report) explicitly, but has
  no field for who *authored/submitted* it — because the current design is single-user (one local
  "me" per machine). The C1 dashboard therefore shows *what* was reported and *to whom*, but not
  *by whom*. With multiple users feeding shared supervisors, reports would be effectively
  anonymous.
- **Why it matters:** Once there are multiple users and supervisors, "who submitted this update" is
  an accountability property a supervisor (or the submitter) will likely want — and sometimes
  explicitly *not* want (anonymity by preference), so it should be a **configurable** choice, not
  forced. The seam already keeps this additive: the blob is `orion_version`-stamped, the relay's
  ingest validates required fields **without rejecting extras**, and the codebase already follows
  "name participants explicitly, don't hardwire a single 'me'." So an `author`/`submitter` field
  (plus a config switch for optional anonymity) is a clean later add, not a rewrite. Surfaced from
  real dashboard feedback (2026-06-18) so it isn't lost.
- **Severity:** low
- **Status:** **Report-submitter identity resolved by C3 Increment 2 (2026-07-08); optional
  anonymity remains open.** The comment half was done in Increment 1 (a logged-in viewer's write is
  stamped with their authenticated identity). Increment 2 (the two-person shared base) closed the
  **report-submitter** half: a report pushed with a producer's own per-user `contributor` key is now
  attributed — `relay_reports` carries a server-derived `author_id`/`author_name`, surfaced as "pushed
  by <name>" on the dashboard. Identity is derived from the key, never self-asserted, so a producer
  cannot spoof another. **Still open — the *configurable anonymity* switch:** attribution is currently
  all-or-nothing by credential (an identified producer is always named; only the legacy shared-token
  path is anonymous). KI-17's original point that some users would *prefer* anonymity by choice is not
  yet a first-class per-report/per-project option. That remains a clean additive step (a config toggle
  + an optional null-author path), tracked here rather than closed.

## KI-21 — Forward-store item identity is the title, so a renamed item is a new item

- **Detail:** The observed-state store (`relay_observed_items`, E2 Inc 3 Unit 3) records each
  checklist item's deadline/done over time, keyed by a stable `item_key`. For tracker applications
  that key is the **bare title** (the producer emits it as `key`, because the item `text` embeds the
  status — "Title - In progress" — and so changes when the status does); for tasks/table items, which
  carry no status in their text, the key falls back to the **text**. This deliberately makes identity
  status-independent (the whole point — an application's history must survive Not-started → Submitted).
  Consequences, inherited from the same title/text identity model as KI-6: (a) **renaming** an item's
  title makes the old key's history stop and a brand-new key begin (the prior observations are
  orphaned, not migrated); (b) **two items with the same title** (e.g. in different tracker sections)
  collapse to one `item_key` and their observations interleave; (c) `group` (added in Unit 5 for
  milestone roll-up) is **deliberately not part of the key** — identity stayed status-/group-independent
  so a milestone re-grouping never orphans an item's history. So two same-titled rows in different
  sections still share one key (the limitation above), and group+title disambiguation remains the
  available, un-taken seam rather than a shipped change.
- **Why it matters:** Slippage and history (Units 4+) read this key to track one item across pushes.
  The title-based key is the simplest identity that meets the rung's needs and is correct for the
  common case (a stable title that advances through statuses). The edge cases are minor for the
  current single-tracker workload, and surfacing them keeps the behavior intentional. The seam stays
  additive: `group` now rides each item (Unit 5), so the key *could* become group+title without a
  migration (the store is an append-only **projection**, rebuildable from the pushes) — but that is held
  as a seam, not built, since folding group into the key would orphan history on a re-grouping for no
  current benefit. Complements **KI-6** (the tasks collector's text identity) — same model, applied forward.
- **Severity:** low
- **Status:** By-design (documented limitation; revisit if a rename/duplicate-title case bites in real
  use — group+title keying is the available seam if so).

## KI-22 — SPA renders some design fields the relay does not yet carry (E2 Inc 4 4a)

- **Detail:** The E2 Inc 4 dashboard SPA (`web/`) consumes the relay's read-only JSON API
  (`docs/dashboard-api-contract.md`). A few fields the design shows are not stored on the relay today,
  so the API ships a graceful fallback and the SPA degrades rather than inventing data: (3) **recipient
  roles** in a report's "SENT TO" — `participants` is a list of plain name strings, so `role` is `null`
  and the SPA shows names only; (4) **report/project `source_tags`** ("git history · checklist · session")
  — the originating collector set is not stored, so the API ships `[]` and the SPA omits the "BUILT FROM"
  card; (5) **project description** — none stored, so the header sub-line is omitted; **(7) comment author
  role is now CLOSED** — the free-text, `role: null` comment surface was retired outright in KI-28 Stage 2,
  and the one conversation surface (the discussion thread) carries a real server-derived role.
  Separately, the report **title** is the body headline (the report has no distinct
  title field) — a documented choice, not a gap. **(8) embedded item status is now CLOSED** — see below.
  Gaps 3, 4, 5 remain.
- **Why it matters:** Each is a producer/wire extension (richer `participants`, a `collectors` list, a
  `description` on the blob), deliberately out of the read-only 4a backend seam. They are
  flagged so the SPA's omissions read as intentional, and so the closing change is a known additive step.
- **Exercised (DF1, 2026-07-21):** all three remaining gaps confirmed still open against a
  live relay, read straight off `GET /api/projects/<name>`: **gap 5** — `description` is
  present as a key but empty; **gap 4** — `source_tags` ships `[]`; **gap 3** — `participants`
  came back `null` on the reports checked (so no roles, a fortiori). The SPA degrades rather
  than inventing data, as designed. No change; recorded so the next producer-wire extension
  knows these are still the open three.
- **Severity:** low
- **Status:** Partly closed. Gap 8 (item status) closed in the Tracker slice (see below); gaps 3/4/5/7
  remain by-design for 4a (graceful degradation), closed incrementally when the producer wire is next
  extended; tracked against the contract doc's "Data gaps" list.
- **Update (Tracker slice, E2 Inc 4):** gap 8 closed. The producer now ships a first-class semantic
  `status` field (`not_started|in_progress|submitted|closed`) additively (the text embed stays for legacy
  `render.py`/reports), the relay folds `in_progress` into `state` and passes raw `status` through, and the
  tracker page renders the circular in-progress arc + submitted/closed label from it. Chosen over a
  relay-side text parse so status is a clean observed property end-to-end rather than reverse-engineered
  across the process boundary — the foundation later supervisor-side features build on.

## KI-24 — Disciplines extraction is LLM-judged (scope, selection, dedupe) (E2 Inc 4 4b)

- **Detail:** The Disciplines collector (4b) reframes a doc's stated principles into cards via a Haiku
  step, which leaves three properties model-dependent rather than deterministic: (a) **scope** — each card
  is classified `global` vs `project` by the model, so the Global/project split is approximate (a
  cross-cutting principle may land under a project, or vice versa). **As of Unit 5 (2026-07-13) this arm is
  cosmetic:** the "Working agreements" section on the project page renders **all** of a project's cards
  regardless of scope, so an approximate split no longer gates whether a card is shown (the scope enum is
  dropped from the wire card entirely); only a possible future scope *badge* would surface it. (b)
  **selection/count** — which
  principles are extracted (and how many) depends on the doc and the prompt, so it is not stable across
  model or prompt versions, and a dense doc can yield many cards (a visual spec yields styling trivia,
  which is why `discipline_docs` should point at instruction docs, not `design/`); (c) **global dedupe is
  identity-by-normalized-title** — near-duplicate titles ("inert" vs "inert.") do not merge (mirrors
  KI-6's identity-by-text). The content-hash cache keys on doc text **plus** an `extract.CACHE_VERSION`,
  so a prompt change busts the cache only when that constant is bumped.
- **Why it matters:** observe-not-originate holds (the model reframes only stated principles, never
  invents, and the collector stamps `source`), but the *organization* of the cards is a model judgment.
  An outside reader sees a faithful-but-approximate grouping, not a guaranteed-canonical one.
- **Exercised (DF1, 2026-07-21):** ran real extraction against two different real instruction
  docs. `instruction-debugger`'s CLAUDE.md yielded **7 cards**, this repo's yielded **16** —
  every one traceable to a principle actually stated in the doc, so **observe-not-originate
  holds** on real input. Arm (c) produced no near-duplicate titles in this sample (not a
  refutation: one sample, and the arm is about a case that needs two similar docs to appear).
  Arm (b), selection/count, is visible in the 7-vs-16 spread — that is the doc's density
  showing through, which is the entry's point. The approximation reads as intentional. No
  change. Separately, this exercise is what surfaced the `disciplines-push` empty-clobber bug
  (CHANGELOG, DF1 sweep) — a different failure of the same command.
- **Severity:** low
- **Status:** Open by-design for 4b (stage-appropriate), **shrunk by Unit 5 (2026-07-13)** — the scope arm
  (a) is now cosmetic (all cards render on the project page regardless of scope), leaving only
  selection/count (b) and title-dedupe (c) as model-judged. A later move to deterministic scope-by-source
  or a richer dedupe is additive — the store still keeps `scope` on `{title, why, scope, source}`, so a
  future scope badge is additive. Tracked here so the approximation reads as intentional. Relates to [[KI-6]].

## KI-26 — Skills comb is per-project + component-flavored, and the comb visual is approximate (E2 Inc 4 4c)

- **Detail:** The shipped skills comb has three known limitations, surfaced on real data and to be
  addressed in a dedicated rework (see `docs/skills-comb-rework-kickoff.md`):
  (a) **per-project extraction, not global** — skills are extracted per project and merged by normalized
  title, so the same competency phrased differently across projects ("Python backends with CLI tooling" vs
  "Python backend development") does NOT collapse, producing near-duplicate teeth (mirrors KI-6/KI-24's
  identity-by-text); (b) **component-vs-skill** — the prompt sometimes emits a *system the developer built*
  ("multi-drone tracking") rather than a *skill* (the Bayesian-probability / geospatial competencies that
  system was built with); the bar to hit is resume-grade / job-description-grade competencies; (c) **comb
  visual fidelity** — the vertical-bars-by-depth rendering is not faithful to the actual Comb Method /
  Capability Comb visual (needs design research).
- **Why it matters:** the tab works and is observe-not-originate, but it does not yet read like a resume an
  outside reader (or a recruiter) would find legible — duplicates and component-entries dilute it, and the
  visual undersells the comb metaphor.
- **Severity:** low (cosmetic + content quality, not correctness or security)
- **Status:** **RESOLVED (rework CP1 + CP2 shipped).** Parts (a) and (b): `orion skills-sync` runs a
  **global two-pass** extraction (pass 1 builds one deduplicated, resume-grade canonical vocabulary across
  all projects on Sonnet; pass 2 attributes it per project, blind to the others) and writes every slice
  through an **atomic batch** endpoint (`POST /skills-batch`, prune + empty-clobber guard). The
  global-vs-scope tension is resolved structurally — pass 2 never sees another project, so existence-hiding
  holds without trusting the prompt. An "achieved, not demoed" anti-overclaim rule keeps the output honest
  (a demo of how something might work is not a competency). Depth boundaries re-tuned for the now-accurate
  breadth. Part (c): `Skills.tsx` + `skillsComb.ts` + `base.css` redesigned to the true comb-shaped-skills
  form — a horizontal **spine** (breadth) with **teeth hanging down**, length = depth ("broken comb"), in
  category-labelled segments, evidence cards kept; eyes-on-verified on real data across all 3 themes.
  Calibration-validated against a private resume oracle kept outside the repo. Residual: run-to-run name flicker (a more advanced
  persistent-identity "living skills" store is the proper long-term fix) and KI-27 (dead `tasks` signal).
  Relates to [[KI-25]] and the kickoff doc above.
- **Historical note (2026-07-13):** the skills comb was **retired at parity** in the living-resume
  retirement (Units 1-4) — the residual run-to-run flicker above would have needed a persistent-identity
  "living skills" design, which was judged the least-aligned part of the product to invest in. This entry
  is kept as a record of the rework that shipped; the feature it describes no longer exists. See CHANGELOG.

## KI-32 — Aggregate disciplines are last-writer-wins across producers; per-producer merge + display deferred (C3 Inc 2.5)

- **Detail:** C3 Inc 2.5 stores per-producer disciplines (`relay_producer_disciplines`, dual-written
  beside the aggregate on every write path). But nothing **reads** them yet — the aggregate
  `relay_project_disciplines` row still drives the dashboard's Disciplines section, and that aggregate is a
  single row **overwritten on every push**. So under multiple producers the disciplines roll-up reflects
  only whoever pushed most recently. (The skills half of this KI is **moot**: the skills comb was retired at
  parity in the living-resume retirement, and `relay_producer_skills` was dropped — see the Resolved index.)
- **Why it matters:** the same roll-up fidelity gap KI-30 had (now fixed for checklists), still present
  for disciplines. It is not a data-loss bug: per-producer provenance is captured now precisely because it
  **cannot be backfilled** (every push before this shipped would otherwise be lost). The
  `producer_disciplines_for` read seam exists; only the merge/display is deferred.
- **Exercised (DF1, 2026-07-21):** reproduced end to end with two real identified producers.
  Provisioned two `contributor` accounts, minted a key each, and pushed the same project's
  disciplines from both: `relay_producer_disciplines` gained **two rows** (`author_id` 2 and
  3), while `relay_project_disciplines` kept its **single** row, overwritten by the second
  push. Provenance captured, nothing reading it — exactly as the entry states.
- **Severity:** low (provenance captured, roll-up fidelity only; per-producer rows are on the store).
- **Status:** Open, deferred as **additive**. A later unit derives display from the `producer_disciplines_for`
  seam. Note: Unit 5 of the living-resume retirement (shipped 2026-07-13) moved disciplines onto the project
  page's "Working agreements" section and made the aggregate's Global/project split cosmetic (all of a
  project's cards render regardless of scope), which shrank [[KI-24]]. It did **not** resolve the
  per-producer roll-up here — the section still reads the last-writer-wins aggregate row.

## KI-33 — Per-producer slippage splits an item's history when a producer pushed anonymously then identified (C3 Inc 2.5)

- **Detail:** C3 Inc 2.5 partitions each checklist item's observation stream by `author_id` before
  running `is_slipping`, so two machines' interleaved pushes no longer corrupt the streak. A producer that
  pushed some observations on the **legacy anonymous** token (`author_id` NULL) and later switched to its
  **own** key has that item's history split across the `None` stream and its author stream.
- **Why it matters:** each `is_slipping` arm (postponed / lingering) needs ≥2 observations **in the same
  stream**, so a split history has *shorter* streams — which can only **miss** a real slip, never invent a
  phantom one. The failure mode is conservative (a false negative on a since-migrated producer's older
  history), and it self-heals as fresh identified pushes accumulate.
- **Severity:** low (conservative; transient; only affects a producer that straddled the anonymous→keyed
  cutover).
- **Status:** By-design. The alternative — cross-attributing anonymous history to whoever later
  identified — would break the unforgeable server-derived-attribution invariant. Left as-is.

## KI-34 — The project page is growing dense; section categorization may need a redesign (E2)

- **Detail:** As the project page accretes sections (forward-look milestones, live checklist, per-contributor
  cards, reports, discussion, and now the Unit-5 "Working agreements" band), the single two-column layout is
  starting to feel bloated and under-categorized. The **live checklist in particular runs very long** — every
  item in every milestone is listed flat, independent of the milestone cards directly above it. The
  information is all correct; the *organization* is the concern.
- **Possible direction (not committed):** make the "Forward look" milestone cards **expandable**, showing that
  milestone's related checklist items nested inside the card, rather than a separate flat full-length checklist
  below. That folds two sections into one and ties each item to its milestone. Other groupings are possible;
  this is one candidate, not a settled design.
- **Why it matters:** legibility to an outside reader is the whole point of the dashboard; a page that reads as
  an undifferentiated wall of sections works against that. It is a **UX/information-architecture** concern, not
  a correctness or security one.
- **Severity:** low (cosmetic / IA; nothing is wrong or missing, only how it is grouped).
- **Resolved (2026-07-22, KB surface Inc 1 Unit 3)** — the recorded candidate direction was adopted as
  written, and it held up on real data. The "Forward look" milestone cards are now **expandable groups**
  (`web/src/components/MilestoneGroup.tsx`): each milestone's own checklist items nest inside it, revealed
  on click, and the **separate flat full-length checklist section was retired outright**. Groups are
  **collapsed by default** — that collapse is the density fix itself, since the complaint was that every
  item in every milestone rendered at once. Items belonging to no milestone group trail in an **"Other"**
  group (the same rule the tracker page uses), so retiring the flat list drops nothing. Verified eyes-on
  against a deliberately dense project (3 milestone groups, 13 items, 2 of them ungrouped) across all three
  themes, desktop + mobile: the page went from *3 cards plus a 13-item flat list* to **4 scannable
  summaries** that expand on demand, with per-item states (overdue / due-soon / done) intact inside.
- **Deliberately not changed, so their absence is a decision:** the **"Working agreements" placement is
  kept** where Unit 5 put it (leading the left column as context before the progress detail) — the placement
  question this KI raised is settled as *keep*, not moved. No broader re-categorization of the remaining
  sections (Working agreements, Forward look, By contributor, Reports, Discussion) was attempted: each is
  now a distinct band and the page reads cleanly, so a further layout redesign has no concrete complaint
  behind it. `MilestoneCard` itself is unchanged and still backs the Showcase demo's non-expandable view.
- **Status:** **Resolved** (the density complaint this KI names is fixed). If the two-column layout feels
  bloated again as stage-2/3 surfaces land, re-file with the specific section that reads wrong rather than
  reopening this one — the concrete grievance recorded here (the duplicated flat checklist) is gone.

## KI-36 — Reports sent before a relay grant never land on the dashboard (backfill path added)

- **Detail:** When a project's reports are sent (via `report`/`intake`) while the pushing key
  is **not yet scoped** for that project, relay ingest returns 404 and the fail-soft
  `_relay_push` drops it — the report reaches Discord/Slack but never lands in the relay's
  append-only `relay_reports` history. Concretely hit by `instruction-debugger` (added to
  `orion.toml` and reported, granted on the relay only later). The **recovery path now exists**:
  `orion relay-backfill <project> --generated-at <iso> [--body-file <f>]` pushes the exact
  report content (which the user still has in Slack/Discord) onto the relay — relay-only and
  chat-silent, at the original timestamp — reusing the report path's two-pass redaction and the
  `/ingest` transport. One report per invocation (a batch / `--from-history` replay is a recorded
  follow-on). Idempotence is the preview/confirm gate: the history is append-only, so a re-run
  would add a duplicate row; `--yes` skips the preview for a knowing re-push.
- **Why it matters:** the dashboard is meant to be the durable record of a project's progress; a
  scoping gap at onboarding silently drops history from it. Backfill *recovers* already-sent
  reports, but does not *prevent* the gap.
- **Still open (the forward-fix):** nothing scopes a new project into the relay at `add-project`
  time — a project must be granted separately (`relay-user grant`). A follow-on could **prompt to
  scope a project into the relay during `add-project`** (opt-in, to preserve preview-before-send
  and avoid coupling the config writer to the admin token). This is a distinct SCOPING concern
  from the ingest push, and ties into the multi-producer / auth-revamp pass (whose Unit 1 resolved
  KI-35, the sibling last-writer concern): scheduled multi-producer scoping gets reworked there.
- **Exercised (DF1, 2026-07-21):** the recovery path works. `relay-backfill` pushed an exact
  report body at its original `--generated-at`, previewed first, landed a row on the relay at
  the backdated timestamp, and was correctly **chat-silent** (zero webhook requests during the
  run — verified against the sink log, not just the absence of an error). The forward-fix
  remains open and unexercised in its failing form: on the sandbox relay the push *succeeded*
  without any grant, because legacy shared-token ingest is still enabled and accepts anonymous
  pushes. Reproducing the original 404 drop requires `--disable-legacy-ingest` or a
  contributor key, which is worth remembering when the forward-fix is finally built — the
  behaviour differs by ingest mode.
- **Severity:** low–medium (recovery exists; the forward-fix is a convenience, and the gap only
  bites at onboarding, before a grant).
- **Status:** Partially addressed — the `relay-backfill` recovery command shipped in this slice;
  the `add-project` scope-prompt forward-fix stays **Open**. Revisited at the auth-revamp planning
  pass (2026-07-19): kept **out of the revamp arc** to bound its scope, recorded as a small
  follow-on once the account model lands (the prompt then scopes an account, not a bare key).

## KI-37 — Concurrent first opens of a non-WAL relay DB can fail on the WAL conversion

- **Detail:** `open_relay_store` sets `PRAGMA journal_mode=WAL` on every open. Converting a database
  from the default rollback-journal mode to WAL takes a brief **exclusive** lock, and that PRAGMA does
  **not** honor the connection's busy timeout — so if two workers open a not-yet-WAL database at the
  same instant, one raises `OperationalError: database is locked` immediately rather than waiting.
  Found while building the auth-revamp Unit 2a migration (a concurrency test on a hand-seeded
  rollback-mode DB reproduced it in roughly 5% of runs); it is **pre-existing and unrelated** to that
  migration, which is why it was recorded rather than folded into that unit.
- **Why it matters:** the relay opens the store **per request**, so this is reachable in principle.
  In practice the window is very narrow: it only affects a database that is not yet in WAL mode, and
  a relay's DB is converted on its very first open — which happens before it serves concurrent
  traffic. Every already-deployed relay DB (including the live one) is long since WAL, where the
  PRAGMA is a cheap no-op that takes no exclusive lock.
- **Exercised (DF1, 2026-07-21) — still reproduces:** 40 freshly hand-seeded rollback-mode
  databases, four concurrent `open_relay_store` calls each, gave **2 failures out of 160
  opens** (~1.25%) with `database is locked`. Lower than the ~5% the original note recorded,
  which is expected — the rate is timing-dependent, and the point is that it is non-zero and
  still real. Everything else in the entry holds: a WAL database is unaffected, so no
  deployed relay is exposed.
- **Severity:** low (first-open-only, and a fresh relay converts before taking traffic).
- **Status:** Open, unfixed by choice. A fix would be a small retry/tolerate around the PRAGMA, but it
  must not silently swallow the error and leave the DB in rollback mode — the concurrency semantics
  WAL provides are the reason it is set. Worth doing if the relay ever provisions databases under
  live traffic. Note the sibling race in the same function **was** fixed at Unit 2a: `_ensure_columns`
  ALTERs were check-then-act and two concurrent opens could collide with `duplicate column name`,
  which was reachable on any migrating redeploy under traffic (see CHANGELOG → *"Accounts and
  credentials"*).

## KI-38 — Login throttling has no per-IP dimension, so an attacker can degrade login for everyone

- **Detail:** Unit 3's throttle limits login attempts per **account** (a short lockout after N failures)
  and relay-wide (a coarse rolling bound that catches attempts sprayed thinly across many names). There
  is deliberately **no per-IP dimension**. So someone hammering the login endpoint from one address trips
  the global limiter, and legitimate logins are refused until they stop.
- **Why it matters:** it is a denial-of-service on dashboard login, not a disclosure or bypass — the
  per-account lockout still stops online guessing, and the admin `relay-user password unlock` clears an
  account lockout immediately. At the current N (two humans) the exposure is small and the operator is
  also the victim, so it would be noticed at once.
- **Exercised (DF1, 2026-07-21) — reproduced, with numbers:** against a local relay, a
  legitimate account logged in successfully (200). One client then sprayed **60 wrong
  passwords across 60 different account names**, tripping the relay-wide limiter
  (`_GLOBAL_MAX_FAILURES = 50` in a `_GLOBAL_WINDOW_SECONDS = 5 * 60` window). The same
  legitimate account, with its **correct** password, was then refused. Three details worth
  adding to the entry:
  1. **The cost of the attack is trivial** — 50 requests, well under a second, and sustaining
     the lockout indefinitely just means continuing to send them.
  2. **The victim gets no signal.** The refusal is a plain **401**, indistinguishable from a
     wrong password, so a locked-out user retypes their password and concludes it is broken
     rather than realising they are being throttled.
  3. **The lockout is in-memory only** — restarting the relay clears it immediately (observed
     twice during the sweep). That is a genuine mitigation for the operator (a redeploy is a
     fix) and a limitation of the control (it does not survive a restart for real attackers
     either).
  Method note: an initial attempt appeared to reproduce this and did not — those 403s were
  the same-origin **CSRF** check rejecting requests sent without an `Origin` header, not the
  throttle. The numbers above come from the corrected run.
- **Severity:** low at this scale; would rise to medium the moment the relay serves a real org, where
  one attacker could lock out people who have no idea why.
- **Status:** Open, deferred **by choice** at the auth-revamp planning pass (second-opinion amendment 6,
  user-arbitrated). Per-IP means trusting a forwarded header behind Fly's proxy, and trusting one
  wrongly is worse than not having it — an attacker forges the header and evades the limit entirely. The
  fix is therefore gated on a `--behind-proxy`-style explicit trust flag, which is a misconfiguration
  footgun in its own right. **The seam exists:** `relay/throttle.py` is keyed by DIMENSION (account,
  global), so adding an `ip` dimension is additive rather than a redesign.

## KI-40 — Grants are add-only: no path removes a single project grant

- **Detail:** `orion relay-user grant <name> --project <p>` adds grants, and nothing anywhere
  removes one. There is no `ungrant`/`revoke-grant` subcommand, no admin-API route, and no
  `DELETE FROM relay_user_projects` in the CLI or the relay. The only ways to narrow an
  account's scope today are blunt: `revoke` (deactivates the whole account) or `delete` (drops
  it entirely, then re-add with the smaller set — which mints a new credential the holder must
  be re-issued).
- **Why it matters:** scope is a **security control**, and a control that only ever widens is
  the wrong shape. A grant handed out for one piece of work cannot be taken back without
  disrupting the account. It also leaves stale grants accumulating with no cleanup path —
  found exactly that way in the auth-revamp live close-out, where `macos` carries a grant for
  `sliptest`, a project that does not exist on the relay. That particular grant is harmless
  (a grant to a nonexistent project grants nothing), but it is unremovable, and the next one
  may not be harmless.
- **Note on severity:** low *today* because the deployment has one human, one machine, and one
  agent, all trusted. It rises with the member/visibility work now shipped — the moment scopes
  are handed to people outside the immediate circle, "cannot un-share" becomes a real problem.
- **Exercised (DF1, 2026-07-21):** confirmed by both routes. The `relay-user` subcommand set
  is `{add, list, revoke, grant, key, password, role, rename, set-operator, delete}` — no
  ungrant verb. In the source, the only `DELETE FROM relay_user_projects` anywhere is inside
  `delete_user` in `relay/store.py`, i.e. whole-account deletion. `grant` itself works and is
  idempotent (granting `orion` to an existing contributor reported *"Scope is now:
  applications, orion"*), which is precisely the asymmetry: scope widens on demand and never
  narrows.
- **Severity:** low now, medium once accounts are provisioned for other people.
- **Status:** Open. Recorded 2026-07-20 during the auth-revamp close-out; the approved
  `sliptest` cleanup was **blocked** on it and deferred. Slotted for a proposed
  **command overhaul / revamp slice** (with the sibling wording and lifecycle-ergonomics
  findings) rather than a one-off patch, by user decision — the whole `relay-user` surface
  grew additively across three arcs and deserves one considered pass.


## KI-41 — Redaction's catch-all matches ordinary prose, so reports go out mangled

- **Detail:** the generic catch-all in `redact._PATTERNS` matches any name containing
  `api_key` / `secret` / `token` / `password` / `access_key` / `private_key` / `auth` /
  `credential`, followed by `:` or `=` and a 4+ character value. The keyword may sit
  anywhere inside the name (`[\w.\-]*` on both sides), so ordinary English words match:
  **`auth` matches `authenticated` and `author`**. Observed on the real Orion diff during
  the DF1 sweep — two hits in one report, **both false positives**, both on documentation
  prose:

      authenticated: false   ->   authenticated: [REDACTED_SECRET]

  Also confirmed: `author: yoolee committed the change` → `author: [REDACTED_SECRET]`.
  Quoted JSON keys escape (`"author_id": null` survives, because the `"` breaks the
  name-then-separator match), so the exposure is unquoted YAML/TOML-ish lines and prose —
  which is most of what a diff of a docs-heavy repo contains.
- **Why it matters:** the direction is **fail-safe** — this over-redacts, it never leaks,
  so it is not a security hole. Two costs, though. First, it silently changes meaning in
  text a supervisor reads: `authenticated: [REDACTED_SECRET]` gives no hint that the value
  was `false`. Second, and more important, it inflates the *"N potential secret(s) were
  redacted"* preview warning with routine noise. That warning is the human control
  [[KI-3]] calls **the guaranteeing layer** — the thing redaction's incompleteness is
  *supposed* to be backstopped by. Training the operator to scroll past it degrades the
  actual control, which is a security consequence even though the mechanism is not.
- **Note on the fix:** deliberately NOT patched on discovery. Narrowing a security control
  trades false positives for the risk of false negatives, which is the failure mode that
  matters, so it wants calibration data rather than a quick regex tweak. Two directions,
  neither yet chosen: require the keyword to be a whole word within the name (kills
  `author`/`authenticated`, keeps `AUTH_TOKEN` via the `token` keyword), and/or skip values
  that are common non-secret literals (`true`/`false`/`null`/`none`) — the latter is nearly
  risk-free since no real secret is literally `false`. Any change needs a corpus check
  against real diffs, plus tests pinning that every currently-caught secret shape still is.
- **Severity:** low (correctness/UX now, with a slow erosion of the preview control).
- **Status:** Open. Found 2026-07-21 by the DF1 sweep, on the first `detailed`-share report
  of a real repo. Relates to [[KI-3]] (the same control's false NEGATIVES).


Issues whose full write-up now lives in [`CHANGELOG.md`](../CHANGELOG.md). Kept here as a
one-line index so a resolved id is still traceable from the issue tracker. Newest first.

- **KI-39** — `orion report` aborted with `Unknown collector 'disciplines'` for any project that
  enabled the `disciplines` capability, because `collectors` conflated report collectors (which have
  a `_collect_for` branch) with push-only capability flags (which deliberately do not). Total
  breakage of `report` for affected projects, and unavoidable — `disciplines-push` requires the flag
  to be listed. **Resolved 2026-07-20**, found by dogfooding the new agent account in the
  auth-revamp close-out: the two kinds are now named separately (`REPORT_COLLECTORS` /
  `PUSH_ONLY_COLLECTORS`, with `SUPPORTED_COLLECTORS` derived from them), the report loop skips
  push-only names by constant, and a structural test walks `REPORT_COLLECTORS` asserting each has a
  dispatch branch so the mirror bug cannot recur. See CHANGELOG → *"KI-39 — `orion report` broken
  for any project enabling a push-only collector"*.

- **KI-35** — Project-level settings on the relay cleared on absence: a `/checklist` push that omitted
  `due_soon_days` wiped the stored horizon, so a second producer without that config would silently clear
  a value another producer set (periodically, once E1.3 made pushes schedulable). `kind` had the identical
  bug, demoting a tracker to a project. **Resolved 2026-07-19** in Unit 1 of the auth-revamp arc: both
  settings became **set-only** (absence now means "leave it alone"), and clearing became explicit via a
  tri-state wire value (absent = leave, `null` = clear, int = set) carried by
  `checklist-push --clear-due-soon-days`. `/ingest` stays set-only and now REJECTS an explicit null — a
  report blob must never clear project settings. Residual, accepted at the decision: a horizon that is set
  and then dropped from config persists until someone clears it explicitly (the rarer, human-visible case,
  traded for the silent one). KI-32's per-producer disciplines merge stays deferred. See CHANGELOG →
  *"Set-only project settings + explicit clear (auth-revamp Unit 1)"*.
- **KI-13** — Cadence-aware `report --all --due` filter (a per-project schedule + last-report-time gating,
  the stateless subset carved out of the deferred B5 layer). **Resolved 2026-07-17** in the E1.2 slice:
  `report --all --due` reads each project's `cadence` config against `report_history`'s `MAX(sent_at)` (no
  new schema) and reports only what's due; the B5 *layer* stays gate-deferred (its strongest motivation,
  mixed cadences from one entry, is now served). The full resolution note is kept in place above (with the
  B5-gate stance). See CHANGELOG → *"Forward look & scheduling (E1.2)"*.
- **KI-25** — The skills comb omitted the project/tracker glyph on evidence anchors. **Retired (feature
  removed) 2026-07-13** with the whole skills comb in the living-resume retirement (Units 1-4). No fix was
  needed: the surface it described no longer exists. See CHANGELOG → *"Living-resume retirement — skills
  comb removed"*.
- **KI-27** — The skills `tasks` signal was declared in the vocabulary but never sourced from real
  evidence. **Retired (feature removed) 2026-07-13** with the skills comb; the signal vocabulary is gone.
  See CHANGELOG → *"Living-resume retirement — skills comb removed"*.
- **KI-30** — The aggregate checklist badge/progress (portfolio card, `stats`, at-risk/slipping counts,
  scheduling, report snapshot) was **last-writer-wins across producers** — the portfolio numbers reflected
  only whoever pushed most recently. **Resolved 2026-07-09** by the **"effective checklist"** merge: at ≥2
  active identified producers the displayed numbers derive from a union across producers' per-producer
  checklists (done = OR, so a stale "not done" copy can't regress a done item; metadata
  last-writer-per-item), with the aggregate kept as a byte-identical fallback at 0–1 producers. Per-producer
  slippage (partitioning observation streams by `author_id`) landed in the same slice. Skills/disciplines
  roll-ups stay last-writer-wins → **KI-32**. See CHANGELOG → *"C3 Increment 2.5 — per-producer
  consolidation"*.
- **KI-31** — Contributor lifecycle management was incomplete: the admin API (`relay-user`) was
  `add`/`list`/`revoke` only — no way to expand a contributor's scope, rotate a key, or free a revoked
  (UNIQUE, single-use) name. Surfaced by the live two-person dogfood, where it blocked the
  `--disable-legacy-ingest` cutover for the multi-project Mac. **Resolved 2026-07-09** by adding
  **`relay-user grant`** (expand scope, idempotent), **`relay-user rotate`** (re-mint an active user's
  key), and **`relay-user delete`** (hard-delete that frees the name; the user's reports/discussion keep
  their denormalized author). The broader **auth-model revamp** (bare keys → a real key lifecycle / a more
  authentic login system) stays a recorded *direction* in `plans/orion-plan.md`, not part of this fix. See
  CHANGELOG → *"Contributor lifecycle — grant/rotate/delete + legacy-ingest retired"*.
  **Superseded 2026-07-20:** that auth-model revamp shipped, and it **retired `relay-user rotate`** —
  the multi-credential model makes it redundant, and its one-shot semantics invited a silent-401
  window on scheduled machines. Key replacement is now `key add` → deploy → verify → `key revoke`.
- **KI-28** — Comments and Discussion were two overlapping conversation systems (E2 Inc 5): the
  identity-first, two-way discussion loop and the older C2 comments (`report_comments` + its
  routes/CLI/UI/bot path) did one job with two stores. **Resolved 2026-07-07 (Stage 2, Slice 0)** by
  retiring comments **outright** (no `report_id` tag) and folding them into the discussion model at
  parity (the KI-23 / `render.py` precedent): an idempotent migration tool
  (`relay/migrate_comments.py`) with a lossless collapse-guarded drop, then the SPA / relay-backend /
  CLI-delivery-state / bot retirements (the bot **parked** pending per-user keys). The **live
  production migration ran 2026-07-07**: 9 comments → discussion items (4 developer, 5 supervisor via
  `--developer-ids`), verified at parity, then `report_comments` dropped. See CHANGELOG → *"KI-28
  Stage 2 — comments retired, folded into the discussion model at parity"*.

- **KI-29** — Weekly Windows CI matrix red: `tests/test_config.py::test_discipline_docs_resolve_absolute`
  used a POSIX-only "absolute" fixture (`"/abs/…"` has no drive letter, so Windows treated it as relative
  and the loader — correctly — resolved it against the config dir, failing the assert; found by the first
  real matrix run after the Actions quota reset, 2026-07-06). **Resolved 2026-07-07** by building the
  fixture from `tmp_path` (genuinely absolute on every OS; `as_posix()` keeps the TOML string free of
  backslash escapes). Product behavior was correct throughout — test-only fix. Verified by a manual
  full-matrix `workflow_dispatch` run: all 9 OS × Python jobs green. (The first attempt's single
  py3.12-Windows failure was an unrelated flaky relay live-server socket test,
  `test_revoke_unknown_user_is_404`, which passed on re-run — noting here in case it recurs.)
- **KI-23** — Legacy server-rendered dashboard not yet retired (the relay carried two front-ends:
  the React SPA and the `relay/render.py` HTML views). **Resolved 2026-06-27** at parity — the SPA
  now covers every URL the old dashboard served, so `relay/render.py` and its legacy form routes
  (`GET/POST /login`, `POST /report/:id/comment`) were removed; the relay keeps the JSON API, the
  cookie-authed comment write, the auth/CSP machinery, and SPA serving. See CHANGELOG → *"E2 Inc 4 —
  sectioned dashboard rebuild (SPA): legacy server-rendered HTML retired at parity"*.
- **KI-8** — Vestigial Phase-1 state artifacts after the Phase-2 marker migration: the
  `project_state` table (backfill source) and the always-`""` `ReportBlob.source_marker`.
  **Resolved 2026-06-25** by dropping both — the Phase-1→Phase-2 backfill window closed long
  ago (Phase 2 shipped 2026-06-15) and the relay never required `source_marker`. See CHANGELOG
  → *"Consolidation slice — dashboard-home visibility, add-project completeness, KI-8 cleanup"*.
- **KI-20** — Delivered Slack/Discord messages timestamped in UTC while the dashboard rendered
  Pacific. **Resolved 2026-06-19** by a global `display_timezone` config field (message formatter
  honors it), with the relay-side follow-up (`orion relay-serve --timezone`) landed in Horizon D.
  See CHANGELOG → *"KI-20 — configurable message timezone, aligned with the dashboard"*.
- **KI-19** — Dashboard served inline CSS/JS with no Content-Security-Policy. **Resolved 2026-06-24**
  by a hash-based CSP (SHA-256 of the inline blocks, derived from the same constants the page
  renders) plus the standard security headers. See CHANGELOG → *"Dashboard security hardening — CSP
  + headers"*.

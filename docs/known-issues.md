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

> **DF2 dogfood sweep, 2026-08-20.** Second sweep, same harness, run per DF1's recorded
> disposition (no kickoff, discovery on the sandbox: real repos, real Haiku calls; state,
> webhooks and relay all redirected). Entries carrying an **"Exercised (DF2, 2026-08-20)"**
> line were run for real this time. What DF2 reached that DF1 could not: **all six of DF1's
> unexercised entries except KI-34** — KI-10 (rich Markdown through the Slack lane), KI-16
> (the local-backend *seam*, against a local OpenAI-compatible stub — model quality remains
> untested), KI-17, KI-21 (the rename arm), KI-33 (miss **and** self-heal) — plus KI-36's
> failing form in its real mode (`--disable-legacy-ingest`), which DF1 explicitly could not
> reproduce. KI-34 is since resolved; its resolved layout was verified live on a dense real
> project during the SPA pass. Surfaces landed since DF1, all exercised clean: the
> auth-revamp `relay-user` verbs (key add/list/revoke, password set/unlock + the key-login
> cutover, role, rename, set-operator, revoke-vs-delete name semantics), the S2.2
> `relay-project lifecycle`/`visibility` verbs and the dashboard's Past-projects grouping,
> the About + due-soon carriers with their KI-35 tri-state (set → survives omission →
> explicit clear), the DF1 empty-clobber guard on `disciplines-push` (refuses the
> config-relative accident by name), `relay-backfill` (now attributed under a contributor
> key), `add-project --like`, and the **S2.3 search band** — `GET /api/search` (scope-aware,
> multi-word AND, input validation) and the SPA Search page (grouped, highlighted hits).
> One honest S2.3 dogfood signal: the sweep itself never reached for search to answer its
> own questions — with operator access, `sqlite3` and `grep` were always closer to hand;
> search's audience is the browser-side reader, and there it worked. Findings: **one fix
> PR** (KI-49's failure marker, PR #160) and **two new entries** ([[KI-50]], the unpreviewed
> relay tail — the sweep's significant find — and [[KI-51]]). Not re-exercised, by choice:
> everything DF1 already reproduced on since-unchanged code (KI-1/5/6/7/11/14/24/32/37/38),
> the closed/deferred redaction complex (KI-41/47/48, per their standing decisions), and
> KI-44's deferred thread-cap half.

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
- **Exercised (DF2, 2026-08-20) — fired again on ordinary use, and one DF1 claim needs
  correcting.** The sandbox first report of `instruction-debugger` (107 commits,
  `high_level`, single Discord recipient) truncated at exactly 2000 again. But the DF1 line
  "the preview shows the full report … nothing on the sending side signals the loss" does
  not describe current behavior: the preview shows each audience's **composed** message, so
  a Discord-only audience sees the truncated text *with* its `… [truncated]` marker — the
  chat-side loss is now visible before sending. What that per-audience honesty exposes
  instead: the relay's full copy is then partly unpreviewed, which is [[KI-50]], filed by
  this sweep. The splitting-vs-truncating decision here is unchanged and still deferred.
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
- **A concrete false negative, found and closed 2026-08-06** — the first one this entry has ever
  had by name, and it came from pinning the catch-all's behavior rather than from hunting leaks.
  `Authorization: Bearer <opaque-token>` was matched by the catch-all, but its value matcher
  stops at whitespace, so it redacted the word `Bearer` and left the credential in the clear
  **while reporting a hit_count of 1**. An opaque bearer token matches none of the specific
  patterns, so the catch-all was its only cover. Fixed by skipping an optional `Bearer`/`Basic`
  scheme word; see CHANGELOG → *"Redaction: `Authorization: Bearer <token>` leaked the token"*.
  Two things worth carrying from it: a miss that still increments `hit_count` is strictly worse
  than a plain miss, because it defeats the human check as well as the automatic one — so
  "the count went up" is not evidence a line was handled. And it was found by writing a test
  for what the pattern *currently* did, not by reviewing what it was *meant* to do.
- **A sibling of that leak is STILL OPEN, and is named here rather than left implicit.** A quoted
  *name* defeats the catch-all outright: `headers={"Authorization": "Bearer <token>"}` is not
  redacted, because a `"` before the name breaks the name-then-separator match (`[\w.\-]*`
  cannot cross a quote). Never covered, before or after the scheme fix. Any source file that
  builds a headers dict has this shape, so it is not exotic. Not fixed with the scheme skip
  because it is a property of the **name** matcher, and widening that is its own calibrated
  change — the same discipline KI-41's three rejected designs earned the hard way. Asserted in
  `tests/test_redact.py` against current behavior so it cannot pass silently; that test is meant
  to FAIL when someone closes the gap.
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
- **Exercised (DF2, 2026-08-20) — first real probe of the limits, via `intake` with
  deliberately rich Markdown.** The two supported constructs worked (`**bold**` → `*bold*`,
  nested `_italics_` inside survived correctly). Everything outside the scope reached the
  Slack sink raw, as the entry predicts: `[text](url)` links arrive as literal bracket
  syntax (Slack's own form is `<url|text>`), pipe tables arrive as plain pipe lines, and a
  ` ```python ` fence keeps its language tag as visible text. The boundary is real but only
  reachable through user-authored structured content (intake/notes) — the LLM lane's output
  still has not produced these constructs. The scope choice stands, now with a measured
  edge rather than an assumed one.
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
- **Exercised (DF2, 2026-08-20) — the SEAM, not the quality.** A local OpenAI-compatible
  stub stood in for a runtime, which exercises exactly the contract this entry is about: a
  real 16,805-character collector prompt went through `LocalSummarizer` via config
  (`provider = "local"`, `base_url`, `model`) and the reply flowed into a complete report.
  Both non-conformance arms failed closed as documented — a response missing
  `message.content` gave *"Local summarizer returned an unexpected response shape"*, an
  HTTP 500 gave a clean `SummarizerError`, and neither advanced state. What this
  deliberately does NOT test is summary **quality** on a real local model — the entry's
  original blocker stands, and the capability-floor note under KI-4 is still the only
  evidence there.
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
- **Exercised (DF2, 2026-08-20):** both attribution modes verified on the sandbox relay —
  a shared-token push landed an anonymous row (`author_id` NULL, no "pushed by"), the same
  command under a contributor key landed attributed (`sb-prod-a`, server-derived), and
  `relay-backfill` under a key is attributed too. Confirmed by grep that no
  anonymity-by-choice switch exists anywhere in config or CLI. The entry's open half is
  accurately stated; no change.

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
- **Exercised (DF2, 2026-08-20) — the rename arm (a), reproduced exactly as written:** a
  sandbox tracker item renamed between pushes ("Beta Grant" → "Beta Grant Fellowship") left
  the old key's observation rows orphaned in `relay_observed_items` and started the new key
  with a fresh, history-free stream — visible at both the store and the API. Arm (b), two
  same-titled items collapsing to one key, was **not** constructed this sweep; it remains
  documented-but-unexercised. The identity model behaved as documented; no change.
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
- **Status:** Partly closed. Gap 8 (item status) closed in the Tracker slice (see below); gaps 3/4
  remain by-design for 4a (graceful degradation), closed incrementally when the producer wire is next
  extended; tracked against the contract doc's "Data gaps" list.
- **Update (audit, 2026-07-28): gap 5 is CLOSED — by retirement, not by filling.** DR1-R U3
  (2026-07-24) removed the always-null `description` from the serializer, `types.ts`, the one
  never-true `Project.tsx` conditional, and the contract doc: the observed **About** band (S2.1
  Unit 2) covers the concept, and an *authored* blurb would be a distinct, additive later choice.
  The DF1 line above predates that removal. Gaps **3** (participant roles) and **4**
  (`source_tags`) are now the open remainder.
- **Exercised (DF2, 2026-08-20):** gaps 3 and 4 re-confirmed live against the sandbox relay,
  read off `GET /api/reports/1`: `participants` carries `role: null` and `source_tags` ships
  `[]`. Still the open remainder; still degrading gracefully.
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
- **Exercised (DF2, 2026-08-20) — reproduced end to end, both the miss and the self-heal.**
  A sandbox tracker item was observed once anonymously (due 2026-08-25) and once under a
  contributor key with the deadline moved to 2026-09-01 — a real postponement straddling
  the cutover — and the API showed `slipping: false`: neither stream held ≥2 observations,
  the documented conservative miss. A second identified push (moved again to 2026-09-05)
  flipped it to `slipping: true` — the identified stream now carries the postponement on
  its own, confirming the entry's "self-heals as fresh identified pushes accumulate."
  Exactly as designed; no change.
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
- **Exercised (DF2, 2026-08-20) — the failing form finally reproduced in its real mode,
  which DF1 could not.** With the sandbox relay restarted `--disable-legacy-ingest` (the
  production posture), an `intake` under a contributor key with **no grant** for the
  project delivered to chat and had its relay push 404 — the report dropped from the
  dashboard record, the exact gap this entry describes. Two details worth adding: the CLI
  **does** print `⚠ relay push failed (report still delivered)`, so an interactive run is
  not silent — the silence is specific to unattended runs where nobody reads stdout. And
  `relay-backfill` then recovered the exact body at a chosen `generated_at`, now
  **attributed** (the pushing key's author lands on the backfilled row) — the recovery
  path works in the keyed world too. The forward-fix remains open and re-confirmed:
  `add-project`'s next-steps output still says webhooks + `orion check` and never mentions
  granting the project into the relay.
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
- **Exercised (DF2, 2026-08-20):** still add-only. The verb set is unchanged (`key`,
  `password`, `role`, `rename`, `set-operator` joined the surface in the revamp; no
  ungrant verb among them), `grant` still widens idempotently, and the whole lifecycle
  battery around it behaved cleanly (revoke keeps the name — re-add 409s; delete frees
  it). The asymmetry this entry names is intact.
- **Severity:** low now, medium once accounts are provisioned for other people.
- **Status:** Open. Recorded 2026-07-20 during the auth-revamp close-out; the approved
  `sliptest` cleanup was **blocked** on it and deferred. Slotted for a proposed
  **command overhaul / revamp slice** (with the sibling wording and lifecycle-ergonomics
  findings) rather than a one-off patch, by user decision — the whole `relay-user` surface
  grew additively across three arcs and deserves one considered pass.
- **Audit (2026-07-28):** re-confirmed live (the `sliptest` grant is still on `macos`, still
  unremovable), and the fix is now **mapped**: a store `ungrant_projects` (one parameterized
  DELETE, idempotent), a `POST /api/users/ungrant` route on the existing `_admin_read_named`
  gate reusing `grant`'s project-list validation and returning the remaining scope, an audit
  action string, and the CLI verb. **No session invalidation is needed** — scope is re-read
  from the DB per request (`_allowed_projects`), the same mechanism visibility flips already
  ride. One semantic to document: ungranting a `member` from an org-visible project changes
  nothing (the org-visible union re-adds it) — correct, but the CLI should say so.


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
- **Status:** **CLOSED (won't fix, documented) 2026-08-13** — decided from a fresh measurement
  at the production cap; see the close-out bullets at the foot of this entry. Found 2026-07-21
  by the DF1 sweep, on the first `detailed`-share report of a real repo. Relates to [[KI-3]]
  (the same control's false NEGATIVES) and [[KI-47]] (which inherits the only measurable
  slice, deferred behind a named trigger).
- **Three narrowing designs were built and REJECTED 2026-08-06. The entry stays open, and this
  record exists so the next attempt does not re-walk them.** All three were calibrated against
  27 real `detailed` collector windows (17.6 MB).
  1. **A word boundary** — require the keyword to end at a non-letter, which is what this
     entry's own "Note on the fix" proposes below. Removed 236 false-positive occurrences and
     **silently stopped redacting ~25 real credential names**, because it cannot distinguish
     `tokenizer` from `tokenValue`: `secretKey` (the AWS SDK field), `TS_AUTHKEY` (Tailscale's
     env var), `MINIO_SECRETKEY`, `passwordHash`, `privateKeyPem`, `tokenValue`.
  2. **A stem list** (`author(?!i)`, `authentic`, `authorit`, `tokeniz`). A stem says nothing
     about what follows it, so it swallowed `authentication_string` — MySQL's
     `mysql.user.authentication_string`, which stores a password hash — and `AuthenticatorKey`,
     ASP.NET Identity's TOTP shared secret.
  3. **A word-form list plus a credential-noun veto.** Survived an adversarial pass over 136
     sourceable credential names, a 6,214-file external corpus and a 2,184-name sweep. Rejected
     on cost/benefit rather than on a leak — see the measurement below.
- **The measurement that stopped it, and the metric error it exposed.** The 19% false-positive
  reduction that justified design 3 was measured with the collector's **400-line diff cap
  lifted** — appropriate for *finding* false positives, but it is not what an operator sees. At
  the production cap, across the same 27 windows: **7 previews showed a warning before, 5 after;
  two previews became clean; the median count was 0 both ways and the maximum was 4 both ways.**
  The whole apparatus — 451 regex characters, 9 lookarounds, 21 vocabulary words — moved two
  previews out of twenty-seven. Aggregate hit reduction is not a proxy for reduced warning
  habituation, and reporting it as one was the mistake.
- **Direction now: restructure, do not tune.** The agreed next attempt replaces the monolithic
  regex with a candidate extractor plus a Python classifier as the `re.sub` callback: split the
  identifier on delimiters *and* camelCase, judge the **whole** name so evidence before and
  after a benign word is treated alike (design 3 could not — `author_key` redacted but
  `hash_author` did not, Python having no variable-length lookbehind), exempt only on exact
  benign tokens with no credential evidence anywhere, and default to redacting. Each rule
  carries a named reason, which is more auditable than nested lookaheads, and `hit_count` is
  preserved by counting accepted replacements in the callback. Roughly 50-100 lines of plain
  Python. This shape came out of a cross-model second opinion and is recorded in
  `docs/plan-deviations.md`.
- **A framing correction from the same review, worth keeping:** a hit *count* can never evidence
  a false negative — a missed secret produces no replacement and so no warning. The count shows
  the detector fired, not that it caught everything. The preview does print the full outgoing
  payload every run (`cli.py`), so the real human control is intact; but any doc that leans on
  the count as the safety story overstates it. See the concrete case now recorded under [[KI-3]].
- **Audit (2026-07-28):** `redact.py` unchanged since filing — both candidate narrowings remain
  unimplemented and the entry's calibration requirement stands. Structurally both are edits to
  the one catch-all tuple entry (redact.py:88–109), with `tests/test_redact.py`'s 13 pins as
  the regression floor and `test_benign_prose_is_not_redacted` as the natural home for a
  false-positive corpus.

- **RDX-R Unit 1 landed 2026-08-07: the seam now exists, and this entry is still OPEN.** The
  catch-all is no longer one regex making a judgement. It is a **candidate extractor**
  (`_SECRET_ASSIGNMENT`, which yields assignment-shaped text whose name already carries
  credential evidence) plus a **classifier** (`_classify_name`, an ordered list of named rules
  returning a `_Verdict(redact, reason)`), and the catch-all moved out of `_PATTERNS` into
  `_redact_secret_assignments`, whose count comes from accepted replacements rather than
  `re.subn`. Unit 1 deliberately changed **no** policy: 350,940 generated inputs and 19.6 MB of
  real diffs across 14 repositories produce byte-identical output and identical hit counts
  against `main`. The exemptions this entry is actually about are **Unit 2**, and they now have
  somewhere to go — a rule above the fail-safe default, with a stated reason, testable on its
  own. `redact.py` anchors in the bullets above are superseded: `redact.py:88–109` no longer
  exists as described, and the catch-all now lives below `_PATTERNS` rather than inside it.
- **One PRE-EXISTING regression-floor gap in `tests/test_redact.py`, now CLOSED separately (PR
  #154, merged 2026-08-11).** Relaxing the value matcher from `{4,}` to `{1,}` used to survive the
  whole suite. The 4-character minimum is what keeps `token = abc` out of both the output and the
  preview count, and nothing pinned it. Both sides of the boundary are now asserted
  (`token = abc` survives, `token = abcd` redacts) so the constant cannot drift in either
  direction, and the test states the tradeoff rather than assuming it: the threshold is
  deliberately permissive in the *leaking* direction, on the reasoning that no credential worth
  protecting is three characters long. It landed as its own PR off `main` because the gap
  pre-dated the restructure and was unrelated to it.
- **A second reported gap turned out NOT to be real, and the way it got here is the point.** The
  Unit 1 verifier pass reported that deleting the `(?!\[REDACTED_)` lookahead also survives the
  suite, reasoning that `test_single_secret_is_counted_once_not_double` passes for the wrong
  reason because its input is quoted (`token = "sk-…"`) and the quote blocks the re-match on its
  own. **The quote does not block it.** The catch-all's optional `['\"]?` consumes the opening
  quote, so the just-inserted `[REDACTED_API_KEY]` matches the value matcher in the quoted form
  exactly as in the unquoted one — measured: without the lookahead, `token = "sk-…"` becomes
  `token = [REDACTED_SECRET]"` with `hit_count` 2. Deleting the lookahead **fails** that test, which
  pins its property precisely as its docstring claims. **I published this claim here and in PR
  #153 without running it**, having verified every other finding in the same report — the one that
  sounded least surprising went unchecked. Recorded rather than quietly deleted because it is the
  same failure this entry keeps documenting from the other side: an independent reader is a
  corrective, not an oracle, and a claim about a security control is worth a command either way.
- **One record correction, found by re-verifying the anchors before building.** The restructure
  kickoff carried "`hash_author` does not redact on `main`" forward as a delta to be fixed.
  **It is wrong about `main`.** Measured: `hash_author = value1234` → `hash_author =
  [REDACTED_SECRET]`, hit_count 1. `auth` matches anywhere inside a name on `main`, so `main`
  has no such gap — the asymmetry belonged to **rejected design 3**, which is what the
  "Direction now" bullet above says and says correctly. The claim drifted when it was copied
  into the kickoff as a comparison against `main` instead of against design 3. Consequence
  worth keeping: Unit 1's acceptance bar became **strict parity** rather than "parity plus one
  intended difference", which is a stronger bar, not a weaker one.
- **THE CONSTRAINT ON UNIT 2, and the most important thing on this page for whoever builds it.**
  A declined candidate still **emits its whole matched span verbatim**. The precise invariant,
  established by the verifier pass rather than reasoned at:
  - **A decline can never hide a LATER match.** `re.sub` scans the original string, so
    `match.end()` — and therefore every subsequent match span — is identical whether the callback
    replaces or hands back `group(0)`. Confirmed over 60,000 adversarial multi-assignment inputs,
    zero cases where declining changed the span set.
  - **The entire risk is therefore the declined span's own text.** Which gives the rule Unit 2 has
    to satisfy: **an exemption is sound only if the whole matched span is safe to emit verbatim.
    That is a claim about the VALUE, not about the name.**
  A **name-only rule cannot make that claim**, because `group(1)` is frequently a container key —
  a YAML key, an HTTP header — rather than the variable that governs the value, so the text behind
  it is arbitrary. And the prose words this entry wants to exempt are the *worst* case, because
  they are also real credential names: `oauth`, `author`, `authentication` and `authorization` all
  contain `auth`, so the extractor yields them. Two distinct leak shapes, both measured with a
  name-only prose exemption in place:

      oauth: API_TOKEN=abcdef123456                    -> h=0, an assignment hidden behind the key
      authorization: 8f4e2a91c7b3d5e60192837465afbcde  -> h=0, a raw token AS the value
      author: EX.EXAMPLEONLYnotareal.0000key0000       -> h=0, a vendor key AS the value

  All three redact correctly today (`hit_count` 1, identical to `main`). The third value is
  **deliberately synthetic**: it stands in for shapes the seven specific patterns miss — SendGrid's
  `SG.` (no rule at all) and Stripe's `sk_live_` (our rule is `sk-`, one character off) — and it
  reads as obviously fake because a realistic Stripe literal in the test file **was blocked by
  GitHub's secret-scanning push protection**, correctly, since a scanner cannot know a literal is
  fabricated. Fixtures for this control should carry synthetic values and name the real vendor in
  prose. **The second shape is the
  sharper one:** its value contains no `:` or `=` at all, so the two obvious remedies — re-scan the
  declined span, or refuse to decline while the span holds an assignment — *both fail to fire*.
  Note also that exempting `authorization` **re-opens the `Bearer` leak PR #152 just closed**.
  **Two further corrections to how this trap was first written up here:**
  1. A declined span is **not line-bounded**. The separator is `\s*[:=]\s*` and `\s` matches
     newlines: `oauth:\n    API_TOKEN=abcdef123456` matches with `group(1)` = `oauth`, separator
     `":\n    "`, and the span reaching onto a *later line*. Anyone reasoning about how much a
     decline swallows must not assume one line.
  2. `_classify_name(name)` is **not where an exemption can live** — it never sees the span. The
     source docstring's earlier advice to add rules "here, above the default" contradicted the
     remedy; both have been corrected. **Widening that signature is Unit 2's first design
     decision**, left open deliberately rather than guessed at without a consumer.
  Pinned by `test_a_declining_rule_would_shadow_a_secret_in_the_value_KNOWN_TRAP`, which asserts
  the leak under a stubbed prose exemption, asserts the structural fact it rests on (the extractor
  really does yield the prose word on those lines), and carries a control line proving the trap is
  about prose containers rather than about declining in general. When Unit 2 closes it, that test
  fails, and the failure is the signal to rewrite it.
- **What the extractor's keyword requirement does and does not buy.** It stops a *benign* first
  name shadowing a secret — `env: DATABASE_PASSWORD=hunter2supersecret` would otherwise match on
  `env`, be declined, and the password would walk out. That is why the vocabulary stays in the
  extractor, pinned by `test_the_extractor_anchors_on_the_credential_name_not_an_earlier_one`. It
  buys **nothing** against the trap above, which is about credential-ish names.
- **How all of this was found, which is the transferable part.** Three successive versions of the
  safety claim here were wrong. **Two of the three were caught by an independent verifier pass on
  the diff, and neither by any local instrument**: (1) the vocabulary belongs in the classifier —
  leaks on benign names; found **in the plan-mode pass, before any code**, by building the naive
  shape and measuring it, so this one is not to the verifier's credit; (2) a stub that declined
  only names *without* credential evidence, which the extractor never yields, so it exercised zero
  decline paths while reading as proof of safety; (3) "the trap is bounded to assignments,
  therefore re-scanning is sufficient" — disproved by the raw-token rows above. Version (2) had
  already passed a 350,940-input differential, 19.6 MB of real diffs, and six mutation checks.
  One more distinction worth keeping, since it is easy to overstate: the mutation that now catches
  version (2) was **written after the finding and derived from it**, so it is a regression
  guarantee, not independent corroboration. The narrow lesson,
  worth more than "verify harder": **a test whose stub never fires still passes**, so a pin on a
  not-yet-reachable path must be checked for *reachability*, not merely for green. The broader one
  is the one this control keeps re-teaching: local instruments answer "did this change what
  happens to text I already have?" and cannot answer "is there a shape I stopped catching?".
- **CLOSED (won't fix, documented) 2026-08-13, by decision from a fresh measurement at the
  production cap.** The Unit 2 plan-mode pass re-baselined the per-preview distribution on
  current data with `scripts/redaction_baseline.py` (committed in the same PR as this update):
  110 collector-shaped windows — the 87 real reports in `report_history` replayed at their
  actual boundaries as if `share_level = "detailed"`, plus one window per active day for the
  nine never-reported local repos — each assembled exactly as `collect()` assembles a report
  (commits + diffstat + sensitive/noise-filtered diff, 400-line cap) and scored with the
  shipped `redact()`. The numbers, quoted from the committed instrument's **pinned,
  reproducible run** — `python scripts/redaction_baseline.py --as-of
  2026-08-13T09:00:00+00:00`, two consecutive runs byte-identical:

      BASELINE detailed   : warn  31/110 ( 28%)  median 0  max  27  hits  108
      BASELINE high_level : warn   1/110 (  1%)  median 0  max   1  hits    1
      minus prose-name (UNSOUND — leaks)        : warn 17/110  hits 54  previews-cleaned 14
      minus benign-value (sound)                : warn 29/110  hits 88  previews-cleaned  2
      minus prose-name AND benign-value (sound) : warn 30/110  hits 93  previews-cleaned  1
      minus code-value (KI-47's class)          : warn 22/110  hits 86  previews-cleaned  9

  Three facts decided it. (1) **Every sound exemption is negligible.** The only variant that
  moves the warning rate is the blanket prose-name exemption, and that is the design the
  constraint bullet above forbids — its 54 exempted hits include 21 `Authorization` spans and
  the `oauth`/`authorization` container shapes that re-open the `Bearer` leak PR #152 closed.
  The sound variants clean 1–2 of the 31 warned previews. (2) **The warnings cluster where no
  sound rule can reach.** 22 of the 31 warned windows are Orion's own repo, whose docs and
  tests deliberately carry credential-shaped fixtures — a warning-free detailed preview of
  this repo is structurally unreachable, and redacting those fixtures is the control working.
  (3) **No configured project uses detailed share today.** Every stanza in the live config is
  `high_level`, under which the same 110 windows produce one hit total — fittingly, the
  commit *subject* of `4628867`, the Bearer-leak fix, whose own message contains
  `Authorization: Bearer <token>` and so trips the catch-all in the commits section — so the
  operator-facing benefit is entirely conditional on a future config change. This is the
  second time this entry's honest answer was "don't build it" (the first is the 2026-08-06
  record above), and the entry closes rather than standing as an invitation to a third
  attempt. Re-measuring live is one command: `python scripts/redaction_baseline.py`
  (add `--as-of` for a reproducible pin). **Three record errors in the drafts of this very
  bullet were caught by two pre-PR verifier passes and are corrected above** — the
  high_level hit was misattributed to a `Co-Authored-By:` trailer (structurally impossible:
  the commits section carries `%s` subjects only); the instrument measured the
  `applications` repo twice (`Path.resolve()` does not case-normalize on macOS, so the
  config's lowercase path failed to match the on-disk `Applications`; the dedupe now
  compares device+inode); and the first corrected draft still quoted a denominator nobody
  else could reproduce, because a live run is **time-varying by design** — new report rows
  and new commits add windows, including an observer effect where committing this very
  close-out moved Orion's own HEAD and added a window. That is why the run quoted above is
  pinned with `--as-of`. Six wrong claims on this one control have now been caught by
  independent review and none by any local instrument.
- **The seam question, answered for the record rather than built** — it was Unit 2's first
  design decision, and any future revisit inherits the answer: capture the value as an
  explicit `group(3)` in `_SECRET_ASSIGNMENT` (a capturing group cannot change what matches,
  so the edit is parity-verifiable) and widen the seam to `_classify(name, value)`. The
  span-safety requirement then **reduces to a value-safety claim**: every other component of
  a declined span — name, separator, optional quote, `Bearer`/`Basic` scheme word — is a
  public token the pipeline already emits or discards harmlessly, so "the whole matched span
  is safe to emit verbatim" holds exactly when the value is safe.
  `test_a_declining_rule_would_shadow_a_secret_in_the_value_KNOWN_TRAP` stays in the suite
  unchanged: nothing declines, and the trap it pins remains the live constraint on any future
  rule.
- **Two stale duplicate bullets were removed from this entry in the same pass.** PR #153
  appended the corrected versions of the record-correction bullet and the Unit 2 constraint
  bullet without deleting the earlier ones, so the entry carried both — and the earlier
  constraint write-up stated the disproved claim ("the trap is bounded to assignments, so a
  re-scan of the declined span is sufficient") as live fact. The corrected invariant is the
  one that remains above: the two obvious remedies both fail on raw-token values, and the
  whole risk is the declined span's own text. One true detail from the removed version worth
  keeping: the seven specific patterns run before the catch-all, so an `sk-`, `ghp_`, JWT or
  AWS-shaped credential inside a would-be-declined span is already redacted (and protected by
  the `(?!\[REDACTED_)` lookahead) before the classifier sees the line — verified, and NOT
  sufficient to make declining safe.

## KI-43 — `graduate-idea` duplicated `add-project`'s flags and has drifted (live functional gap)

- **Detail:** `graduate-idea` was built by copying `add-project`'s argparse flags verbatim
  (cli.py:616–676 vs 490–581). `add-project` later grew `--tracker-file`, `--incubator-file`,
  and `--seed-tasks-from`; `graduate-idea` never did. Consequence, verified by tracing:
  `orion graduate-idea <name> --collectors git,tracker` reaches `render_project_stanza`, which
  hard-fails with *"enables the 'tracker' collector but no tracker_file was given"*
  (scaffold.py:246–248) — and the command has **no flag to supply one**. Graduating an idea
  into a tracker-carrying project is impossible without falling back to `add-project` by hand.
- **Why it matters:** it is the textbook failure mode of flag duplication — the copies drift,
  and the drift is invisible until someone walks the untraveled path (found by the 2026-07-28
  audit's producer-side pass, not by any test or real use). The durable fix is structural, not
  additive: share the parser via argparse `parents=[...]` so the drift class dies, rather than
  copying the three missing flags (which would just re-arm it).
- **Severity:** medium-low (a real broken path on a shipped command, but on a rarely-walked
  lane — `graduate-idea` has no recorded real use since D-era).
- **Status:** **CLOSED** by AU1-R F4 (2026-07-29), structurally rather than additively. Both
  commands now draw their shared flags from one `_project_registration_parser` passed as
  `parents=[...]` (the first use of argparse `parents` in the codebase), so a flag cannot be
  added to one without the other. A `--help`-diff test asserts the two flag sets differ only
  by graduate-idea's intentional three (`--name`, `--incubator`, `--force`) and fails with a
  readable diff naming the shared parent as the fix. That guard was itself verified by
  injecting a flag on `add-project` alone and watching it fail.
- **Two things the filing did not capture, found while fixing:**
  1. **`parents=[...]` alone would not have fixed the bug.** Three more sites dropped the
     values even once the parser accepted them: main's dispatch, `cmd_graduate_idea`'s
     signature, and its delegation call to `cmd_add_project`. The parser accepting a flag
     whose value is then discarded is the same bug in a new place, which is why the
     regression test asserts against the *written config* rather than the exit code.
  2. **`--incubator-file` cannot be shared and must stay duplicated.** It exists on both
     commands with the same `dest` and genuinely different meanings — on `add-project` the
     new project's incubator collector file, on `graduate-idea` the source index to read.
     Putting it on the parent raises `conflicting option string` at parser-build time. Each
     parser declares its own, with the reason recorded at both sites, and the anti-drift test
     documents why it appears in neither direction of the diff.
- **Verified for real,** not only in tests: `graduate-idea "Photo Overlay" --collectors
  git,tracker --tracker-file ROADMAP.md` now registers cleanly in a throwaway repo and writes
  `tracker_file = "ROADMAP.md"` into the stanza.

## KI-44 — Relay HTTP server: no socket timeout and unbounded request threads

- **Detail:** `_RelayHandler` never sets a socket `timeout` (BaseHTTPRequestHandler defaults
  to None) and `_read_raw_body` does a blocking `rfile.read(length)`, so a client that sends
  headers and then stalls the body holds its thread indefinitely. `ThreadingHTTPServer`
  spawns one thread per request with no pool or cap. Together: a trivial slowloris-style
  client can accumulate stuck threads until memory or scheduling degrades the relay. The
  Argon2 semaphore (S2.0 Unit 3) bounds *hashing* memory but not thread count; Fly's proxy in
  front softens but does not eliminate the exposure.
- **Why it matters:** it is a denial-of-service shape, not a disclosure or bypass — same class
  as [[KI-38]], and like it, the severity rises the moment the relay serves people beyond the
  operator. Unlike KI-38 it needs no trust-boundary decision to fix: a class-level
  `timeout = 30` on the handler is a two-line hardening (BaseHTTPRequestHandler honors it),
  and a bounded-thread server subclass is a small, self-contained follow-on.
- **Severity:** low-medium (reachable today; cheap to close; no design fork involved).
- **Status:** **HALF-OPEN** as of AU1-R F2 (2026-07-29). The **timeout half is closed**:
  `_RelayHandler.timeout = 30`, so a client that stalls mid-body no longer holds a thread
  indefinitely. The **thread cap is still open, by decision rather than by omission** — the
  bounded-thread subclass was weighed and deferred, because the memory-heavy path (the Argon2
  semaphore) already bounds the resource that actually bit, and sizing a thread cap wants
  observed load rather than a guessed constant. **Revisit trigger:** any observed memory
  pressure or connection pile-up on the relay, or the serving-layer re-litigation already
  scheduled at stage-3/oracle scoping.
- **Scope note worth keeping** (found while fixing, not in the original filing): the relay
  leaves `protocol_version` unset, so it speaks HTTP/1.0 and closes after each response. The
  timeout therefore guards a **stalled request read** and not an idle keep-alive connection,
  of which there are none. A second interaction: a fired timeout is reported by the stdlib
  through `log_error`, which was routed into the no-op `log_message` — so before F2's logging
  change a timeout would have fired **invisibly**. The two bands had to land together.

## KI-45 — The web suite, type-check, and build are not in CI

- **Detail:** `.github/workflows/ci.yml` runs `python -m pytest` only — there is no Node
  setup and no `vitest` / `tsc` / `vite build` step on any trigger. The 88 web tests and the
  SPA's strict TypeScript run only when someone runs them locally. A web regression can land
  on `main` with CI green; a TypeScript compile error would surface only when the next
  `fly deploy` fails mid-Docker-build, which is the latest and worst possible moment.
- **Why it matters:** the SPA is the product's primary surface and roughly a third of its
  code. Everything the backend suite enjoys (regressions caught at PR time, a green-suite
  claim that means what it says) simply does not apply to `web/`. The gap is invisible
  precisely because local discipline has been good — every recorded slice ran the web suite
  by hand — but CI exists so the guarantee does not depend on discipline. The fix is one
  additional CI job (~20 lines: setup-node with cache, `npm ci`, `vitest run`, `tsc -b`,
  `vite build`), cheap on the Actions quota since it is Linux-only.
- **Severity:** medium-low (no known regression has shipped through the gap; the gap itself
  is structural).
- **Status:** **CLOSED** by AU1-R F1 (2026-07-29). `.github/workflows/ci.yml` gained a `web`
  job — setup-node 20 (matching the Dockerfile's build stage) with the npm cache, then
  `npm ci`, `npx tsc -b`, `npx vitest run`, `npm run build` — running on every push and PR.
  Verified by the job's own first run on the PR that introduced it: green in 36s. Two shapes
  of the fix were deliberately declined and recorded in the workflow header: it is **not
  matrixed** (the SPA has no cross-platform promise to prove — it is built once in a Linux
  Docker stage, so Linux + Node 20 is the only build environment that ships), and **coverage
  is not collected** (two dev-only dependencies for a number nothing gates on; revisit when a
  second contributor lands code, or when a bug turns out to have hidden on an untested path).
  Full write-up moves to `CHANGELOG.md` at the AU1-R arc close-out.

## KI-46 — No operational floor: backups, health checks, logging, and release provenance are manual or absent

- **Detail:** four related absences on the deployed relay, found together because they are
  one posture: **(a) backups are manual and per-session** — no schedule, no stated RPO, no
  offsite copy, no tested restore; the S2.2 close-out's backups did not survive their session,
  leaving production's newest restore point three days behind the live schema until the AU1
  audit re-pulled one. **(b) No health checks** — the relay exposes no health endpoint and
  `fly.toml` defines no `[[http_service.checks]]`, so the platform cannot tell a healthy
  relay from a wedged one. **(c) No logging** — zero `import logging` across `relay/` and
  `src/orion/`; output is print-to-stderr, so production failures leave little evidence and
  no structure to search. **(d) No release provenance** — one git tag in the repo's history,
  no version identifier queryable from the running service, and `fly deploy` ships the
  working tree, so "what code is running?" has no authoritative answer after the fact.
- **Why it matters:** these are the items a release engineer declines to sign off without,
  and they share a failure mode the code-quality work cannot compensate for: **operational
  gaps fail silently in production**, not loudly in review. At one operator and five trusted
  users the operator *is* the monitoring, which has genuinely sufficed — but stage 2 widens
  the audience by design, and the backup half has already bitten once (the S2.2 loss). Each
  item is individually cheap: a scheduled pull or volume snapshot + a documented restore, a
  health endpoint + Fly checks, stdlib `logging` with one formatter, and a tag-on-deploy
  convention + a version constant on the wire.
- **Severity:** medium for the backup half (real data, one volume, no drill); low-medium for
  the rest, rising with audience.
- **Status:** **Partly closed** by AU1-R F2 (2026-07-29). Gaps **(b)**, **(c)**, and **(d)**
  are closed on the build side:
  - **(b) health checks** — an unauthenticated `GET /healthz` returning `{status, version}`,
    routed ahead of both the auth spine and the SPA's catch-all `index.html` fallback (that
    ordering is the whole trick: under `--web-dir` an unmatched path returns the SPA shell
    with a 200, so a check on a mis-ordered route would have reported healthy forever), plus
    an `[[http_service.checks]]` block in `fly.toml`.
  - **(c) logging** — stdlib `logging` on one stderr handler, levelled and timestamped,
    replacing the seven bare prints AND the no-op `log_message`, so request lines, 4xx/5xx,
    auth failures, and fired timeouts all leave greppable evidence. Level is env-tunable via
    `ORION_RELAY_LOG_LEVEL`.
  - **(d) release provenance** — `ORION_BUILD_SHA` baked at image build and served by
    `/healthz` + logged at startup, with the deploy convention
    (`--build-arg … $(git describe --always --dirty)`, plus tag-on-deploy) in
    `docs/deployment.md`. Verified end to end in a real image build: with the arg it reports
    the stamp, without it reports `"unknown"` — an absent answer rather than a wrong one.
  Gap **(a), the backup half**, is closed on the build side by AU1-R F3 (2026-07-29), with one
  operator action outstanding:
  - **A tested restore path now exists.** `docs/deployment.md` documents restore end to end,
    and the procedure was **walked against a real production pull**, not a synthetic fixture:
    `integrity_check` ok, a relay started against the restored file, login succeeded, the
    portfolio read back every project/tracker with lifecycle states intact, and an individual
    report fetched with its per-project number and prev/next nav working. The one unexercised
    step is writing the file back to the volume, which could only be tested by overwriting
    live data.
  - **A current dated backup exists** at `~/orion-backups/orion-relay.20260729-f3drill.bak`,
    so the "newest restore point is days behind the live schema" condition that opened this
    gap is cleared as of today.
  - **A finding that partly re-rates the original entry: Fly volume snapshots were already
    running.** Verified on `orion_data` — daily, five present, **5-day retention**. The filing
    said backups were "manual and per-session" with "no offsite copy", which was true of the
    *operator-owned* layer but understated the platform's. The real gap was always the horizon
    (nothing survives past 5 days) and the single-account risk, not the absence of any copy.
    The documented RPO now states both layers honestly: ≤ 24 hours inside the 5-day window,
    ≤ 7 days outside it.
  - **A safer pull method replaces the one previously documented.** The old advice ran
    `PRAGMA wal_checkpoint(TRUNCATE)` on the live database before a file copy — a *write* to
    production taken only to make `cp` safe. The runbook now uses SQLite's online backup API
    with a `mode=ro` source, which is consistent under WAL and provably cannot alter
    production, and writes the temp copy to the container's `/tmp` rather than onto the mounted
    volume.
  - **The weekly job is now INSTALLED AND VERIFIED (2026-07-29), so gap (a) is CLOSED.**
    `com.orion.relay-backup` is bootstrapped in the user's `gui` domain and was kickstarted to
    prove it: `last exit code = 0`, launchd itself rewrote the dated file in `~/orion-backups/`,
    and the job's own `integrity_check` reported ok with 74 reports. "A current backup exists
    without a human remembering" is true as of today.
  - **Installing it found two bugs in the runbook that reading it had not** — worth recording as
    a *type*, since both were confidently documented and both were wrong:
    1. **`fly ssh console` does NOT wake a stopped machine** (it fails: "app has no started
       VMs"). The runbook had claimed the opposite, from a session where the machine had been
       woken by a separate `curl` moments earlier — the wake was mis-attributed to `ssh`. With
       `min_machines_running = 0` the machine is stopped most of the time, so the scheduled job
       would have failed on nearly every run. A `/healthz` wake step now leads the script, which
       is a neat closing of the loop: F2's health endpoint is F3's wake probe.
    2. **`fly sftp get` refuses to overwrite an existing file**, so any same-day manual pull or
       retry broke the run. Now pulls to `.part` and `mv`s into place, which also means a
       partial transfer can never clobber a known-good backup.
    The pull logic moved out of the plist into a committed `relay-backup.sh`, reversing the
    inline-one-liner choice recorded as `PD-AU1F-5` — once it needed a wake step, a temp-file
    dance, and a verification pass, a version-controlled script was plainly the right shape.
  **Deployed and confirmed live 2026-07-29 (Fly release v27).** `/healthz` answers on production
  reporting `f56a544` — `main`'s HEAD, no `-dirty` suffix — so "what code is running?" has a real
  answer for the first time; the logs show the format change at the deploy boundary, including
  access lines for Fly's internal checkers. The **scale-to-zero question, which shipped
  explicitly unverified, is now VERIFIED in Orion's favour**: with the checks block live and the
  app idle ~26 minutes, the machine returned to `stopped` on its own, so checks do not defeat
  autostop. A nuance found while confirming it, now documented: a stopped machine reports the
  check as `warning` ("the machine hasn't started"), so on a scale-to-zero app this check must
  **not** be used as an uptime alarm — idle-healthy and broken read alike. Its value is
  "is a *running* machine serving?". Relates to [[KI-44]] (same posture, server-side; its timeout half landed in the
  same unit) and [[KI-38]] (same audience-widening severity curve).

## KI-47 — Redaction fires on secret-ish NAMES assigned CODE, not secrets

- **Detail:** the catch-all's largest false-positive class is not prose (that is [[KI-41]]) but
  source code: a name that genuinely is secret-ish, assigned something plainly not a secret
  because it is an expression, a type annotation, or a keyword argument. Measured over 27 real
  `detailed` collector windows (17.6 MB), these are the top hits *today*:

      auth=_admin_auth())                          125x
      admin_token = _load_relay_admin(config_path)  55x
      view_token=_VIEW)                             44x
      admin_token: str,                             30x
      token: str,                                   24x

  1212 hits across that corpus, a large majority of this shape.
- **Why it matters:** it is the same cost as KI-41 — it inflates the *"N potential secret(s)"*
  preview count with routine noise — and it is the bigger contributor. A developer reporting on
  a codebase that is *about* auth (which Orion is) meets it constantly. Fail-safe direction: it
  over-redacts, it never leaks.
- **Why it is separate from KI-41:** KI-41 is a question about the *name*; this is a question
  about the *value*. Distinguishing an opaque credential from a Python expression has no
  arguable-free heuristic — reject values containing `(`/`)`/`,`, require minimum entropy,
  reject bare identifiers — each trades directly against false negatives with no safe line.
- **Where it should be solved:** in the classifier restructure recorded under [[KI-41]], not as
  another lookahead. A value-shape rule expressed as a named Python predicate can be read,
  tested and reasoned about; the same rule crammed into the regex cannot.
- **Scope note:** the corpus is Orion-weighted and Orion's own source is *about* tokens, so this
  density is a worst case. But the report that opened KI-41 was of Orion's own repo, so the
  worst case is a recurring one.
- **Severity:** low (correctness/UX, with the same slow erosion of the preview count that KI-41
  describes).
- **Status:** Open, **deprioritized 2026-08-13** behind a named revisit trigger (the
  2026-08-13 update below). Filed 2026-08-06 from the KI-41 calibration corpus, not from a
  report anyone read. Relates to [[KI-3]] and [[KI-41]].
- **Update 2026-08-07: the place to solve this now exists.** RDX-R Unit 1 landed the extractor +
  classifier seam (see [[KI-41]]), so the value-shape rule this entry asks for can be written as
  a named Python predicate above the fail-safe default instead of another lookahead. Unit 1
  changed no policy, so every count quoted above still stands. **This is scheduled LAST (Unit 3),
  deliberately** — it is the least-understood rule of the three and benefits from the classifier
  already being in place and trusted. One constraint inherited from Unit 1 and worth reading
  before starting: **a declining rule emits its whole matched span verbatim**, so exempting
  `auth=_admin_auth())` is a claim about that entire span, not about the name — and `group(1)` is
  often a container key rather than the governing variable. The invariant, the counterexamples
  that rule out a name-only rule, and the note that `_classify_name(name)` cannot host such a rule
  are written up under [[KI-41]] and asserted by
  `test_a_declining_rule_would_shadow_a_secret_in_the_value_KNOWN_TRAP`.
- **Update 2026-08-13, from the Unit 2 close-out measurement: this class is smaller than the
  2026-08-06 corpus suggested — and it is now the only slice with any measured operator
  benefit.** At the production 400-line cap over 110 collector-shaped windows (method and
  numbers under [[KI-41]]'s close-out; `scripts/redaction_baseline.py` is the one-command
  re-run), code-shaped values are **22 of 106** catch-all hits — not an order of magnitude
  above prose (54). The uncapped 2026-08-06 counts above are real but cap-dominated: the
  dense `auth=_admin_auth())` clusters sit in windows the production cap truncates. Removing
  this class would clean **9 of the 31** warned previews (28% → 20%) — more than any sound
  KI-41 variant (1–2), and still modest. **Deprioritized rather than scheduled:** no
  configured project uses `detailed` share today, so no operator sees this noise. **Revisit
  trigger:** the first time a project flips to `share_level = "detailed"` for real supervisor
  use, run the instrument; if the warning rate habituates in practice, THIS class — not
  KI-41's prose — is the slice to scope. Two constraints carry over unchanged from [[KI-41]]:
  the exemption judgement needs the value (the seam answer recorded there), and a code-shape
  rule is **not sound by construction** — an unquoted credential passed as a function
  argument would ride out inside an exempted span — so the acceptance bar is constructed
  credential shapes and a conforming implementation attacked, never a corpus.

## KI-48 — The redaction catch-all backtracks superlinearly on pathological input

- **Detail:** the catch-all's name group is `[\w.\-]*` on both sides of the keyword alternation
  with no atomic grouping, so input that repeatedly *almost* completes the match explores
  quadratically many prefix splits. Measured on `main`: `("secret_token_auth_" * 100) + "Z"`
  (1.8 KB) takes **1.9 s**; doubling it to 3.6 KB takes **15 s**. `"authorization" * 400`
  (5.2 KB) takes **20 s**. Ordinary input is unaffected — 17.6 MB of real collector output
  scans in about 4 s.
- **Why it matters:** `redact()` runs on collector output, which is attacker-adjacent in
  principle — a commit message or diff hunk in a collected repository is text someone else may
  have written. CPU exhaustion, never a disclosure. Same class as [[KI-44]].
- **What bounds it today:** the git collector caps the diff at 400 lines and denylists whole
  file classes, so realistic input is tens of KB. No slow run has been observed. That is a
  *containment* argument depending on a constant in another module, not a fix.
- **Note on the fix:** not obvious. Python's possessive quantifier (`[\w.\-]*+`, 3.11+) is the
  *wrong* tool — it would stop the prefix giving ground, so `AUTH_TOKEN=…` would cease to match
  at all. The real directions are anchoring the name match on the keyword instead of searching
  for it, or bounding name length — both of which the classifier restructure under [[KI-41]]
  would naturally provide.
- **Severity:** low (reachable only through content Orion is pointed at, bounded by the diff
  cap, never a disclosure).
- **Status:** **CLOSED** by the RDX-R Unit 1 restructure (2026-08-07), with the fix measured
  rather than assumed — the entry above asked for exactly that. The extractor now carries a
  `(?<![\w.\-])` lookbehind pinning a match to the **start** of a run of name characters, so
  the engine no longer retries the whole prefix/keyword/suffix split at every offset inside a
  long name-like run. Measured through the public `redact()`, before and after:

  | input | `main` | after |
  |---|---|---|
  | `("secret_token_auth_" * 100) + "Z"` (1.8 KB) | 1.80 s | **0.0030 s** (599×) |
  | `("secret_token_auth_" * 200) + "Z"` (3.5 KB) | 14.57 s | **0.0124 s** (1177×) |
  | `"authorization" * 400` (5.1 KB) | 20.30 s | **0.0120 s** (1685×) |

  Pinned by `test_pathological_name_run_does_not_stall_the_redactor` at a deliberately loose
  2-second bound — ~165× above the measured time so a loaded CI box cannot make it flap, and
  ~7× below the old behaviour so it genuinely fails on the old pattern (confirmed by mutation:
  removing the anchor fails the test). **Three things worth keeping from the fix:**
  1. **The anchor is not a narrowing and cannot remove a match.** Starting at a run's first
     character, the greedy prefix can still reach any keyword the run contains, so every name
     the old pattern matched still matches. Verified rather than argued: 350,940 generated
     inputs and 19.6 MB of real `git log -p` across 14 repositories, byte-identical output and
     identical hit counts either way.
  2. **It is still quadratic in the worst case**, just with a small enough constant that the
     git collector's 400-line diff cap is no longer load-bearing as containment. Bounding name
     length remains available if a future input ever makes it matter.
  3. **Ordinary input got faster too** — the 19.6 MB corpus scans in 1.74 s against `main`'s
     4.83 s, an incidental 2.8× that was not the goal.
  Found 2026-08-06 by an independent verification pass that timed adversarial inputs, and
  **pre-existing** — not introduced by the KI-41 work. Relates to [[KI-44]].


## KI-49 — The weekly relay-backup job fails silently

- **Detail:** `com.orion.relay-backup` (launchd, weekly) runs `relay-backup.sh`; its only
  failure signals are a non-zero `last exit code` in `launchctl print` and error lines in
  `orion-backup-launchd.log` — nothing an operator routinely sees. Between 2026-07-29 and
  2026-08-20 **every run failed** (first Fly API "tunnel unavailable" timeouts, then flyctl's
  stored token going invalid outright), so layer 2 of the backup posture was down for three
  weeks. It was discovered only because the S2.3 deploy close-out's runbook happens to begin
  with a manual backup pull.
- **Why it matters:** the weekly pull exists precisely for the failure classes layer 1 (Fly's
  daily snapshots, 5-day retention) cannot cover — a loss noticed late, or the Fly account
  itself. A silently dead job converts the documented "≤ 7 days" worst case into unbounded
  loss while the operator's mental model still says "backed up weekly." An unattended job
  whose failure mode is silence is worse than no job, because it displaces the manual habit
  that would otherwise exist.
- **What bounds it today:** layer 1 kept running throughout; the store is small and
  low-churn; and the deploy runbook's pre-deploy pull refreshes the operator copy on every
  release (which is exactly how this was caught).
- **Direction for a fix (cheapest first):** a dated `FAILED-<date>` marker file written into
  `~/orion-backups/` on any error (the operator looks there anyway, and a marker sorts next
  to the backups it interrupts); or a staleness check (newest backup older than 8 days →
  visible complaint) run from somewhere routinely seen; the zero-code floor is a runbook
  line — check the newest backup's date monthly. Alerting *through the relay* would invert
  the dependency (the thing being backed up reporting on its own backups) — avoid.
- **Severity:** low-medium — no data was lost this time, and layer 1 bounded the real
  exposure, but the failure mode is structural and already occurred once.
- **Status:** open. Filed 2026-08-20 from the S2.3 close-out; the immediate cause (lost
  flyctl auth) was fixed the same day by an interactive `fly auth login` and the job
  re-proven (kickstart → exit 0, consistent pull). The silent-failure gap is what remains.

## KI-50 — The preview shows only the composed channel messages, so the relay's full copy can carry an unpreviewed tail

- **Detail:** the report preview renders one block per audience — the **composed**
  per-channel message, exactly what that channel will receive. The relay push is separate
  and deliberate: `_relay_push(full_blob)` always ships the **full** report body
  (commented in `cli.py`: "the relay always receives the FULL, unfiltered report"). When
  every previewed channel truncates, the difference between the two is text that leaves
  the machine without ever being shown. Reproduced in DF2 on an ordinary config: first
  report of `instruction-debugger` (107 commits, `high_level`), single **Discord**
  recipient — the preview and the delivered Discord message were both exactly 2000
  characters ending in `… [truncated]`; the relay row's body was **5583** characters.
  Characters 2001–5583 reached the hosted relay unpreviewed.
- **Why it matters:** preview-before-send is the **guaranteeing layer** ([[KI-3]]): the
  human check is what redaction's admitted incompleteness is backstopped by. Redaction
  itself ran on the full body (both passes), so the automatic layer is intact — the hole
  is precisely in the human layer, for exactly the bytes the human never saw. The trigger
  is narrow but ordinary: it needs a report over the smallest channel cap (2000 — which
  [[KI-2]] shows a normal first report exceeds) delivered to an audience whose every
  channel truncates. A Discord-only project is the realistic case; every current live
  chat project also carries a Slack recipient (40k cap), whose preview block shows
  effectively everything, so today's exposure is configs that drop to one channel.
- **Relation to KI-2:** this is not the truncation itself (that stays KI-2, deferred).
  DF2 corrected KI-2's DF1-era claim — the preview now honestly shows each audience's
  truncated message. This entry is what that correction exposes: the honest per-channel
  preview is not a preview of everything that leaves the machine.
- **Candidate fix, deliberately not built this sweep:** preview is a security control, so
  reshaping it needs an explicit decision, not a drive-by patch (the KI-41 arc's standing
  ruling). The shape that suggests itself: when the relay is enabled and no previewed
  block contains the full body, add one labeled block ("relay — full record") so the
  preview's coverage matches the send again. Alternatives: preview the full body first
  and the per-channel renderings after; or gate the relay push to the previewed prefix
  (worse — it makes the dashboard record incomplete).
- **Severity:** low-medium (security-control shape; narrow, ordinary trigger; no leak
  observed — the automatic redaction layer did run on everything).
- **Status:** Open — needs decision. Found by DF2 (2026-08-20) on the sweep's first real
  report.

## KI-51 — Unknown `/api/*` paths return the SPA shell with a 200, not a JSON 404

- **Detail:** under `--web-dir`, any unmatched GET falls through to the SPA's
  `index.html` — the deliberate catch-all that client-side routing needs, and the
  ordering trap the `/healthz` work documented. That fallback also swallows unmatched
  **API** paths: `GET /api/projects` (not a route — the real one is `/api/portfolio`)
  returns `200 text/html`. A JSON client gets the page shell where it expected data.
- **Why it matters:** an API consumer probing a wrong path sees a 200 and then a JSON
  parse error, which reads as "the relay is broken," not "wrong endpoint" — exactly how
  DF2 hit it (a scripted read guessed `/api/projects` and got a `JSONDecodeError`). Not a
  security issue: auth still gates every real route, and the shell is what any
  unauthenticated browser gets anyway. The fix shape is small: an `/api/` prefix guard
  ahead of the SPA fallback that returns a JSON 404 for paths no route matched.
- **Severity:** low (developer-experience / API hygiene).
- **Status:** Open. Found by DF2 (2026-08-20).

Issues whose full write-up now lives in [`CHANGELOG.md`](../CHANGELOG.md). Kept here as a
one-line index so a resolved id is still traceable from the issue tracker. Newest first.

- **KI-42** — Report titles showed raw Markdown: a title is the report body's first line, which is
  Markdown, so `**bold**` or `## heading` reached the timeline and report page verbatim (filed in the
  DR1 review). **Resolved 2026-07-24** (DR1-R U3) by flattening the chosen line in
  `relay/api.py:_headline` — strip a leading ATX heading marker + inline emphasis/link/code/image
  marks, leaving single `_`/`*` so `snake_case` survives. Kept relay-local (a few regexes) so the
  separately-deployed relay stays import-free of `src/orion`. See CHANGELOG →
  *"DR1-R — presentation-debt paydown"*.
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

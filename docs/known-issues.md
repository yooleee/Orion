# Known Issues & Cross-Phase Concerns

A lightweight tracker for bugs, design questions, and tech debt that are **not tied to a
single phase** — the local stand-in for an issue tracker until the project is hosted
(then these can migrate to GitHub Issues).

**Lifecycle:** an item lives in the open list while it is open. When it is resolved, its
full write-up moves to the relevant slice's section of [`CHANGELOG.md`](../CHANGELOG.md)
(Added / Changed / Fixed / Removed) and the open entry is removed from here, leaving a
one-line pointer in the **[Resolved](#resolved)** index at the bottom so the id and its
fate stay traceable. Phase-specific work belongs in
[`plans/orion-plan.md`](../plans/orion-plan.md), not here.

**Fields per entry:** a stable id, a one-line title, the detail, *why it matters*, a
severity (low / medium / high), and a status (Open / Needs decision / Monitored /
Deferred).

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
- **Severity:** medium
- **Status:** Decided (by-design); per-recipient state deferred → **C3** (with KI-11).

## KI-2 — Discord message truncation is lossy

- **Detail:** Messages over Discord's 2000-character limit are truncated with a marker
  rather than split across multiple messages.
- **Why it matters:** A long (but already redacted) report loses its tail. Splitting into
  multiple messages would preserve everything; truncation was chosen as the simplest
  correct behavior for Phase 1.
- **Severity:** low
- **Status:** Deferred (enhancement).

## KI-3 — Redaction pattern set is inherently incomplete

- **Detail:** `redact._PATTERNS` is a living list of known secret shapes. No regex set can
  match every possible secret, so false negatives are always possible.
- **Why it matters:** Redaction is one layer of defense in depth, not a guarantee. The
  guaranteeing layer is the human preview-before-send. This entry exists to keep the risk
  visible and the pattern list under periodic review, and to resist any future change that
  would treat redaction as a sufficient control on its own.
- **Severity:** ongoing / by-design
- **Status:** Monitored.

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
- **Severity:** low
- **Status:** Open (evaluate on real projects during use; Haiku is the working quality bar).

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
  due-dates/at-risk; see "Horizon-C direction settled" in `[plans/orion-plan.md](../plans/orion-plan.md)`
  and the strategy doc) lands at the *same* "cadence needs Orion's own state" threshold — so it most
  naturally arrives **with** this scheduling layer and the Horizon-C stateful process, not separately.
- **Status:** Deferred → Horizon C (gate evaluated 2026-06-17; build when per-project cadence or
  activity-gating is actually wanted, most likely alongside the Horizon-C listener).

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
- **Status:** Partly addressed by C3 Increment 1 (2026-06-25). The multi-party **identity**
  infrastructure now exists (viewers log in; a session carries an authenticated user), and the
  **comment half is done**: a logged-in viewer's dashboard comment is now stamped with their
  authenticated identity, not a self-asserted name, so a commenter cannot spoof someone else. What
  remains is the **report-submitter** half — reports are still produced by the single local "me" per
  machine, so the blob carries no `author`/`submitter`. That needs multi-user *submission* (a later
  increment), at which point an `author` field plus a config switch for optional anonymity is a clean
  additive step on the existing seam, not new infrastructure.

## Resolved

Issues whose full write-up now lives in [`CHANGELOG.md`](../CHANGELOG.md). Kept here as a
one-line index so a resolved id is still traceable from the issue tracker. Newest first.

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

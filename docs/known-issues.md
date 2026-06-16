# Known Issues & Cross-Phase Concerns

A lightweight tracker for bugs, design questions, and tech debt that are **not tied to a
single phase** — the local stand-in for an issue tracker until the project is hosted
(then these can migrate to GitHub Issues).

**Lifecycle:** an item lives here while it is open. When it is resolved, it moves to the
**Fixed** (or Changed) section of [`CHANGELOG.md`](../CHANGELOG.md) and is removed from
here. Phase-specific work belongs in [`plans/orion-plan.md`](../plans/orion-plan.md), not
here.

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
  already got it. Neither is obviously right; it needs a deliberate choice (possibly
  per-recipient state).
- **Severity:** medium
- **Status:** Needs decision (flagged in the plan's Phase 1 edge-case table).

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
- **Severity:** low
- **Status:** Open (evaluate on real projects during Phase 1 use).

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

## KI-8 — Vestigial state columns/fields after the Phase-2 marker migration

- **Detail:** Phase 2 moved delta markers to a per-(project, collector) `collector_markers`
  table. Two Phase-1 artifacts are now vestigial: the `project_state.last_commit`/
  `last_reported` columns (kept only as the one-time backfill source in `open_state`) and
  `ReportBlob.source_marker` (now always `""`, since no single marker is meaningful when
  several collectors run).
- **Why it matters:** Dead schema/fields can mislead a future reader into thinking they are
  still authoritative. They are retained deliberately: dropping a SQLite column is not an
  idempotent "IF NOT EXISTS" operation, and `source_marker` is a frozen part of the
  portable `ReportBlob`. Removing them is a future migration, not a Phase-2 change.
- **Severity:** low
- **Status:** Deferred (remove in a dedicated state-schema migration).

## KI-9 — Slack/Discord use plain text; Block Kit + richer Discord formatting deferred

- **Detail:** Phase 3 delivers Slack as a single `{"text": …}` mrkdwn POST (mirroring the
  Discord `{"content": …}` Markdown POST). Slack's Block Kit (structured header/section/
  divider blocks) was deliberately *not* used: it breaks the "message is one string"
  pipeline (`compose -> str`, string preview, string send), would re-derive structure that
  `merge.py` flattens, and adds block-size limits.
- **Why it matters:** Block Kit (and the equivalent richer Discord formatting — embeds) is a
  nicer *look*, but it is orthogonal to the Phase-3 goal of getting both channels routing
  live. When it is built it should be done deliberately — likely with a small `ReportBlob`/
  `compose` change to carry structured sections so blocks build naturally — and **paired**:
  do Block Kit and richer Discord formatting together as one "structured rendering" upgrade.
- **Severity:** low
- **Status:** Deferred (a future formatting-upgrade phase, not a Phase-3 change).

## KI-10 — `_to_slack_mrkdwn` is a structural translator, not a full converter

- **Detail:** `compose._to_slack_mrkdwn` converts only the two Markdown constructs Orion
  emits — `#`/`##` header lines → `*bold*` lines, and `**bold**` → `*bold*`. It does not
  handle arbitrary Markdown (links, nested emphasis, code fences, tables, etc.).
- **Why it matters:** Orion controls what reaches it (merge's `## ` titles and the LLM
  summary's occasional `**bold**`), so a full converter would be speculative complexity. If a
  future source introduces other Markdown that must render in Slack, extend the translator
  then. Flagged so the scope is a conscious choice, not an oversight.
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
- **Severity:** low
- **Status:** Deferred (enhancement; build when per-project cadence or activity-gating is
  actually wanted).

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

## KI-15 — No CLI for inspecting config; config is hand-edited only

- **Detail:** The CLI has no way to view what's configured or change a setting — e.g. after
  `install-hook` you can't confirm/flip `auto_send` from the command line, you edit `orion.toml`
  directly (surfaced 2026-06-16 when a hook was installed before the project was opted in). The
  intended fix is **read-only** inspect commands (`projects` / `show` / `check`), tracked as
  roadmap item **B6**.
- **Why it matters:** The real gap is *visibility/discoverability*, which read-only commands
  solve cleanly. A *write* command (`config set …`) is deliberately **out of scope**: it would
  break the settled "Orion never writes its config" decision (the reason TOML + read-only
  `tomllib` was chosen) and force a comment-preserving TOML writer dependency
  (`tomlkit`/`tomli-w`) against the minimal-dependency principle. Config is a declarative,
  human-authored file; hand-editing it is the intended workflow.
- **Severity:** low
- **Status:** Planned (read-only inspect commands = B6; config-writing intentionally excluded).

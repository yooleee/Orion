# Changelog

All notable changes to Orion are recorded here.

Orion is **pre-release and in active development** — it has no published version yet, so
progress is tracked by **phase** (the project's natural units of progression) rather than by
semantic version numbers. Formal [SemVer](https://semver.org/) versioning will begin once
Orion reaches a genuinely usable, shareable state (for example, at open-sourcing). Within each
phase, changes are grouped under **Added / Changed / Fixed / Removed** in the
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) style, newest phase first, each
noting *why* where it aids understanding.

This file looks **backward** (what was built). For the forward-looking design and phase plan,
see `plans/orion-plan.md`; for open issues and cross-phase concerns,
see [`docs/known-issues.md`](docs/known-issues.md).

## C3 Increment 2.5 — per-producer consolidation (2026-07-09)

Finishes the multi-producer **data** story Increment 2 started. Inc 2 made the produce/ingest layer
multi-producer (attributed reports, per-producer checklist cards) but left three roll-ups
last-writer-wins or single-producer: the aggregate checklist badge (**KI-30**), slippage, and
skills/disciplines. This slice makes the multi-producer data *correct* before more producers arrive.
**Additive-only** — two new `CREATE TABLE IF NOT EXISTS` tables, zero ALTERs, no auth-spine change; the
aggregates stay dual-written as the fallback and the SPA is untouched (merged numbers arrive on the
existing wire contract). Built strictly unit-by-unit off
[`docs/per-producer-consolidation-kickoff.md`](docs/per-producer-consolidation-kickoff.md) (PRs #89–#93).

### Fixed

- **KI-30 — the aggregate checklist badge/progress was last-writer-wins across producers.** The portfolio
  card, project `stats`, scheduling, and report snapshot now read an **effective checklist** merged across
  active identified producers: at **≥2** producers the displayed items are the union of each producer's
  per-producer checklist copy, with `done = OR` (a stale "not done" copy can never regress a genuinely-done
  item — exactly KI-30's flicker) and non-done metadata taken **last-writer-per-item**; at **0–1** producers
  the aggregate row is returned **byte-identically**, so every single-producer / anonymous deployment is
  unchanged. One pure `derive.merge_producer_checklists` + `derive.effective_checklist` fold, wired through a
  single `store.effective_checklist(conn, project)` helper swapped in at all four read surfaces so they can
  never disagree. `derive.item_key` hoisted as the one identity rule (`api._item_key` and
  `record_observations` delegate to it).
- **Per-producer slippage — interleaved two-machine pushes corrupted the slippage signal.** `slipping_item_keys`
  now partitions each item's observation stream by `author_id` before running the untouched `is_slipping`,
  and unions per stream (`slipping_item_keys_by_author`). Two machines pushing the same item no longer
  produce a false "postponed" (one machine's earlier date followed by the other's later date) or an inflated
  "lingering" count. Producer cards mark slippage from **their own** stream; the aggregate rows, milestones,
  scheduling, and the portfolio count use the union. Legacy all-`NULL`-author data collapses to one stream —
  byte-identical to before.

### Added

- **`relay_producer_disciplines` and `relay_producer_skills`** — per-producer storage tables on the
  `relay_producer_checklists` pattern (`(project, author_id)` PK, denormalized `author_name`, JSON payload,
  `updated_at`), each with an `upsert_producer_*` writer and a `producer_*_for` active-join read helper.
  **Dual-written** beside the untouched aggregates on every write path: disciplines on `/disciplines`; skills
  on **both** the per-project `/skills` push **and** the primary `/skills-batch` sync — for the batch, the
  producer rows are reconciled (upsert + prune) **inside `replace_all_skills`'s single transaction**, so a
  crash rolls back both tables together and the producer prune is **always bounded by `author_id = ?`**
  (never touches another producer's rows, even for an unrestricted admin caller). This is **storage now,
  display later**: the read helpers exist but no display surface consumes them yet, because per-producer
  provenance cannot be backfilled and must be captured the moment it exists (see **KI-32**).

### Changed

- `store.observed_history` now surfaces each row's `author_id` (the seam Inc 2 stamped but left unread);
  `delete_user` hard-delete now drops all three live per-producer tables (checklists, disciplines, skills)
  while leaving history (reports, discussion) intact.
- Records **KI-32** (aggregate skills/disciplines stay last-writer-wins; comb-level per-producer merge +
  display deferred — a naming-canonicalization design problem, not a mechanical union) and **KI-33** (a
  producer that pushed anonymously then identified has its slippage history split across streams —
  conservative: it can only miss a slip, never invent one).

Closes **KI-31**. The live two-person dogfood showed the admin API was `add`/`list`/`revoke` only —
you couldn't expand a contributor's scope, rotate a key, or free a revoked name — which stranded the
multi-project Mac and blocked retiring the shared ingest token. Three commands fill the gap, and with
them the deliberate legacy-token cutover was finished on the live relay.

### Added

- **`relay-user grant <name> --project P …`** — add project(s) to an existing user's scope
  (idempotent; returns the full scope after the grant). Closes the "scope frozen at creation" gap.
- **`relay-user rotate <name>`** — re-mint an **active** user's key: the old key and any live session
  stop working; the new key is printed once. Identity, grants, and attributed history are untouched. A
  **revoked** user is a clean **409** (revoke/rotate/delete stay distinct — `delete` + `add` to revive).
- **`relay-user delete <name>`** — hard-delete: removes the user row, its grants, and its **live**
  per-producer checklists, **freeing the name**; it **preserves** the user's reports/discussion history,
  whose denormalized `author_name` still renders (`author_id` is off the wire).
- New admin endpoints `POST /api/users/{grant,rotate,delete}` (admin-token gated, audited) + store
  helpers `grant_projects`/`rotate_key`/`delete_user`. No schema change.

### Changed

- **The shared ingest token is retired on the production relay** (`relay-serve
  --disable-legacy-ingest`, added to the deploy). Only named per-user contributor keys can push now;
  the shared token 401s. Done after every producer migrated to its own key — the Mac's `macos` was
  granted all four of its projects and re-keyed via the new `grant`/`rotate` before the cutover.

## C3 Increment 2 — two-person shared base: attributed producers on the ingest path (2026-07-08)

Advances **C3** ("2 = contribute/write access") and closes the report-submitter half of **KI-17**.
The read/discussion layer was already multi-party, but the **produce/ingest layer was
single-producer by construction** (one shared identity-free ingest token, no author on reports,
last-writer-wins checklists). This slice makes it multi-producer so two people working on different
parts of the same project each see what the other is doing. Built unit by unit off
`docs/two-person-shared-base-kickoff.md` (PRs #81–#84); auth design hardened by a Codex
`/second-opinion`. New KI-30 records the remaining aggregate roll-up (last-writer-wins).

### Added

- **A push-only `contributor` role.** A producer identity that authenticates the machine ingest
  endpoints with its own server-minted per-user key, scoped to its granted projects. Two role
  allowlists make the boundary fail-closed both ways: a `contributor` key can never resolve an
  interactive dashboard login, and a `viewer`/`supervisor` key can never push — one credential
  never spans both auth worlds. `relay-user add --role contributor`.
- **Server-derived producer identity on every Bearer endpoint** (`_resolve_bearer_principal`).
  Identity comes from the key via the existing key-verifier machinery, never from config or a
  request body. Reports are attributed (a **"pushed by <name>"** on the timeline and report page,
  from new nullable `author_id`/`author_name` columns on `relay_reports`); CLI discussion replies
  carry the producer's real name (`--as` is ignored for an identified producer, with a CLI note);
  observations record the observing producer (`author_id` on `relay_observed_items`).
- **Per-producer checklists.** A new `relay_producer_checklists` table (keyed by project + author),
  dual-written beside the untouched aggregate, surfaced as one compact card per contributor on the
  project page (shown only when two or more producers exist).
- **`relay-serve --disable-legacy-ingest`** — the operator-driven cutover that retires the shared
  ingest token once every producer has its own key.

### Changed

- **The legacy shared ingest token now pushes anonymously and logs each use.** It keeps working by
  default (a machine credential must not silently expire), but its pushes carry no author, and every
  use emits a log line so an operator can watch it go quiet before running `--disable-legacy-ingest`.
- **One generic 401 for every Bearer auth failure**, and out-of-scope writes now return a **404
  identical to a missing project** — now that named contributor keys exist, distinguishing failures
  would help enumerate them.
- **The store gained its first idempotent `ALTER`** (`_ensure_columns`, `PRAGMA table_info`-guarded),
  so an already-deployed relay self-migrates the new report/observation author columns on open.

### Notes

- Skills/disciplines/meta and the **aggregate** checklist remain last-writer-wins under multiple
  producers (**KI-30**, low severity, additively fixable). The Slack bot stays parked but is now
  unblocked (its revival needs the per-user keys this slice added). 758 backend + 55 web tests green;
  each unit verified eyes-on against a running relay serving the built SPA.

## KI-28 Stage 2 — comments retired, folded into the discussion model at parity (2026-07-07)

Closes **KI-28**. The E2 Inc 5 discussion loop was the identity-first, two-way successor to C2
comments; Stage 1 (PR #74) hid the visible redundancy, and this slice removed the second system
underneath. Comments were retired **outright** (no `report_id` tag — the dogfooding stretch was too
short to earn it; the tag stays a later additive option), migrated **at parity** (the KI-23 /
`render.py` precedent). Shipped as PR #80; kickoff `docs/stage2-comments-discussion-consolidation-kickoff.md`
(+ the 2026-07-07 addendum).

### Added

- **A one-time, idempotent migration tool (`relay/migrate_comments.py`).** Two deliberate
  invocations: `migrate` folds each `report_comments` row into a `relay_discussion_items` entry
  (`role=supervisor` by default, `author_id` NULL, blank author → "anonymous", body prefixed
  `[re: report N]`, timestamp preserved); `drop` removes the table. Idempotent on a four-tuple key so
  a re-run is a no-op. The drop is **parity-guarded**: it refuses (naming the ids) unless every comment
  maps **one-to-one** onto a distinct discussion row — so a content-key collision (two byte-identical
  comments) can never be silently lost on the destructive step. A `--developer-ids` override attributes
  the developer's own comments as `role=developer` instead of `supervisor` (honest attribution in the
  append-only thread).

### Removed

- **The entire comment system, at parity.** The `report_comments` store (and its `_SCHEMA` block); the
  comment endpoints (`GET`/`POST /api/comments`, `POST /api/reports/:id/comments`); the serializers'
  `comments` field; the SPA `CommentList`/`CommentComposer` + the report page's conversation section +
  `postComment`/`Comment`; the `orion comments` CLI + `pull_comments`; and the local
  `get/set_comment_watermark` accessors. The project-level **Discussion** thread is now the single
  conversation surface (dashboard, CLI, and contract).
- The local **`comment_watermark` table is intentionally kept** (now orphaned): its accessors are gone,
  but the `CREATE TABLE` stays so an existing state DB is not force-migrated (the no-local-drops idiom).
  It can be dropped in a later cleanup.

### Changed

- **The Slack bot is PARKED, not removed.** Its only write target was `POST /api/comments`, which
  retired; repointing it at the discussion write must wait for per-user keys (that Bearer path stamps
  role `developer`, but a chat reply is supervisor speech). `orion bot` now prints a parked notice and
  exits; the pure decision core (`orion.bot.core`) + the Slack shell + `[bot]` config + the `slack-bot`
  extra are kept as the revival seam.

### Live migration (2026-07-07)

- Ran against the deployed relay (`project-orion.fly.dev`): a WAL-safe backup first, a full dry-run +
  rehearsal on a copy, then in-container `migrate --developer-ids 1,2,3,8` + `drop`. **9 comments across
  3 projects** became discussion items — 4 (the developer's own test comments on `orion`) as
  `developer`, 5 (Paul/Bob/Dad) as `supervisor` — verified at parity (dashboard eyes-on) before the
  drop. Backend 736 + web 48 tests green.

## Horizon P — public-showcase pass (2026-06-29)

Horizon P (publish) fired early, in a reframed form: not the OSS launch (that decision stays
untriggered), but a **public-showcase pass** — the repo made safe and presentable to show
(applications, portfolio) with all private data out. Shipped as PRs #75–#76; detail in the
roadmap's Horizon P band.

### Added

- **A clickable simulated demo project on the public showcase (PR #76).** The no-login `/showcase`
  gains a fabricated sample project a cold visitor can click through (overview, to-dos, reports,
  Disciplines, Skills) to see the dashboard populated. Built as **frontend-only typed fixtures** —
  no backend endpoint, no store seeding — so an anonymous leak of real data is impossible by
  construction. The real login stays one link away.

### Changed

- **Personal-reference scrub + repo tidy (PR #75).** Personal references replaced with role-indexed
  placeholders across the tracked tree; internal and OSS-launch files (kickoff docs, the prepared
  OSS docs) untracked + gitignored, kept local until an actual OSS launch. A **secret-scan over the
  full history** came back clean, so history was left intact. Why: the repo doubles as a portfolio
  piece — the showcase pass makes it safe to show now without burning the later go-public option.

## E2 Inc 5 — supervisor-interaction loop (2026-06-29)

The first concrete E5 step: an ongoing, attributable, per-project **two-way discussion**
(supervisor ↔ developer) with Orion as connective tissue + memory — it carries, threads, and
remembers; it **generates nothing** (observe-not-originate holds). Merged as PR #74; kickoff in
`docs/supervisor-interaction-loop-kickoff.md`.

### Added

- **Per-project discussion thread, end to end (Units 1–4, PR #74).** A `relay_discussion_items`
  store with first-class identity (`author_id` + role `supervisor|developer|orion`, and a new
  `supervisor` role on `relay_users`); cookie-authed write + project-read endpoints (identity
  server-derived, append-only, existence-hiding 404s); Bearer machine routes plus an
  `orion discussions pull/reply` CLI loop with a local watermark (a reply lands as
  role=developer); and an SPA discussion panel with colour-coded role badges, XSS-inert bodies,
  and a role-gated composer. 799 backend+CLI + 50 web tests green; the full loop verified
  end-to-end against a running relay. Unit 5 (a read-only "open directions" derivation —
  supervisor messages awaiting a reply) is deferred-additive.

### Changed

- **Comments↔Discussion consolidation, stage 1.** Seeing the loop rendered showed the project page
  had two near-identical conversation surfaces (Discussion + Comments) doing one job. The project
  page now carries only the project-level **Discussion** thread; per-report **Comments** stay on
  the report-detail page (report-scoped feedback). Stage 2 — unify the data model (optional
  `report_id` context on discussion items), retire `report_comments` and the comment UI at parity,
  migrate existing rows, fold the bot comment path — is planned as
  [KI-28](docs/known-issues.md), deliberately after some dogfooding. Kickoff:
  `docs/stage2-comments-discussion-consolidation-kickoff.md`.

## E2 Inc 4 — sectioned dashboard rebuild (SPA) (2026-06-28)

The dashboard rebuilt as a React/Vite single-page app served single-host by the relay (a read-only
JSON API), faithful to the `design/` handoff. Shipped slice by slice (each its own PR); per-slice
detail and the band map live in `docs/e2-inc4-dashboard-rebuild-kickoff.md`.

### Added

- **Skills comb section (4c) — a "partial living resume".** A new **Skills** section shows the kinds of
  work and skills the user's projects *demonstrate*, rendered as a **Capability Comb**: per category, teeth
  whose height encodes each skill's derived **depth**, with evidence cards below anchoring every skill to
  the projects that demonstrate it (`observed · <projects> · <signals>`). It is observe-not-originate (a
  resume that is *derived*, never authored): a per-project collector gathers observed evidence (git
  languages from tracked files + recent commit subjects, plus any `discipline_docs` for topical focus) and
  an **opt-in, cache-gated** Haiku step reframes it into skills **grounded only in that evidence** (no
  aspirational entries). The seam mirrors 4b: `orion skills-push` (`src/orion/extract.py` +
  `collectors/skills.py`, redact-before-LLM, content-hash cache; gated by a per-project `skills = true`
  flag) → `POST /skills` → `relay_project_skills` upsert → `GET /api/skills`
  (`api.serialize_skills`: **merges** each project's skills across the portfolio by normalized name, unions
  the evidencing projects + signals, and **derives each tooth's depth** from evidence weight + cross-project
  breadth — the one place that sees the whole portfolio; **scope-filtered first** so a skill evidenced only
  by an out-of-scope project never leaks) → `web/src/routes/Skills.tsx` (a pure `skillsComb.ts` layout
  helper maps depth→tooth height and groups by category). Untrusted text renders inert. Verified eyes-on on
  **genuine extracted data** across all three themes. This **replaces the planned Cross-project Connections
  graph** (`design/` §8 / `desktop-07`): the projects are mostly independent, so a literal relationship
  graph was thin — reframed mid-build with the developer into the more honest, useful skills comb (recorded
  in [KI-25](docs/known-issues.md) and the kickoff). Backend 744 + web 43 green.

- **Disciplines & directions section (4b).** A new **Disciplines** section reflects the working
  principles Orion observes in a project's own docs, split into a **Global** group (cross-cutting
  conventions) and per-project groups, each card a title, a "why", and an `observed · <source>` footer.
  Observe-not-originate end to end: a new doc-centric collector reads the configured `discipline_docs`
  **unchanged** and reframes only the principles their author stated into cards via an **opt-in,
  cache-gated** Haiku step (the docs are never marked up; the model never invents). The seam mirrors the
  live-checklist push: `orion disciplines-push` extracts (`src/orion/extract.py` +
  `collectors/disciplines.py`, redact-before-LLM, a content-hash cache in the producer state store so the
  model runs only on a changed doc) → `POST /disciplines` → `relay_project_disciplines` upsert →
  `GET /api/disciplines` (`api.serialize_disciplines`: the Global/project split + dedupe with a
  deterministic source pick, **scope-filtered first** so a global stated only in an out-of-scope project
  never leaks) → `web/src/routes/Disciplines.tsx`. The **collector stamps `source`** (the repo-relative
  doc), never the model, so the footer's claim is literally true; untrusted text renders inert (no
  `dangerouslySetInnerHTML`). `discipline_docs` is an explicit, opt-in per-project config list. Verified
  eyes-on vs `desktop-06` across all three themes against the live `orion` docs. Backend 714 + web 36 green.

### Changed

- **Skills comb reworked to a global, resume-grade extraction + a true comb visual (4c rework;
  resolves [KI-26](docs/known-issues.md)).** The initial 4c was judged not-yet-reasonable on real
  data: skills arrived per-project (a fragmented vocabulary), read as project components rather
  than resume-grade skills, and the visual wasn't true to the comb form. `orion skills-push` became
  **`orion skills-sync`**, a **global two-pass** extraction: pass 1 derives one deduplicated,
  resume-grade canonical vocabulary across all projects (on Sonnet — the one step judged worth the
  step-up, since it synthesizes cross-project); pass 2 attributes skills per project **blind to the
  others**, so existence-hiding stays structural. Writes go through a new atomic batch endpoint
  (`POST /skills-batch`: one transaction + prune + an empty-clobber guard), and depth boundaries
  were re-tuned for the now-accurate breadth. The visual (`web/src/routes/Skills.tsx`,
  `web/src/lib/skillsComb.ts`, `base.css`) was redesigned to the true comb-shaped-skills form: a
  horizontal spine (breadth) with teeth hanging down (tooth length = depth), category-labelled
  segments, evidence cards kept. Backend 763 green; web 7/7 + build green; eyes-on-verified on real
  seeded data across all three themes. This entry supersedes the pre-rework `skills-push` shape
  described under "Added" above.

### Removed

- **Legacy server-rendered HTML retired at parity (closes [KI-23](docs/known-issues.md)).** With the
  SPA covering every URL the old dashboard served (Projects home, Project, Report, Tracker, Scheduling,
  Showcase, login), the relay no longer carries two front-ends. Deleted `relay/render.py` (all
  `render_*` views, the `_PAGE_CSS`/`_PAGE_JS` blocks, their CSP hashes) and the **legacy form routes**
  that depended on it: `GET/POST /login` (the HTML login form — replaced by `POST /api/login`) and
  `POST /report/:id/comment` (the form comment write — replaced by the cookie-authed JSON
  `POST /api/reports/:id/comments`). Relocated the three still-used pieces: `_headline` → `relay/api.py`
  (so `api.py` no longer imports `render.py`), and `MAX_COMMENT_BODY_CHARS` / `MAX_AUTHOR_CHARS` /
  `_DISPLAY_TZ` → `relay/server.py`. `_security_headers` dropped its now-dead HTML branch (the SPA's
  own strict-`script-src` CSP rides on the index via `_send_file`). **Kept** unchanged: the JSON API,
  the comment write path, the auth/cookie/CSRF machinery, `GET /logout`, the machine Bearer routes, and
  the store/derive layers. With no `--web-dir` the relay is now **API-only (headless)** rather than
  serving legacy HTML. Backend tests pruned/repointed to the JSON surface (the pure render tests
  deleted; legacy route tests either dropped as redundant with the `test_api_*` coverage or repointed
  to the JSON routes): backend suite 676 green, web 33 green.

## E2 Inc 3 — forward-looking planning layer, rung 1 (observe + remember) (2026-06-26)

The forward-looking layer (E1): due-dates, at-risk, slippage, and derived milestones, built on an
**observe-and-remember, never originate** model. Shipped unit by unit (each its own PR); see the
six-unit ladder in `docs/e2-inc3-kickoff.md`.

### Added

- **Derived milestones** (Unit 5, the LAST rung-1 unit, vertical slice — deploy after merge). Rolls
  the live checklist up into per-**section** milestones and surfaces them. The grouping is OBSERVED
  from the tracker's own structure, carried on a new additive `ChecklistItem.group`: the numbered
  application sections all share one **"Applications"** milestone, and each to-do **table** is grouped
  by its **nearest preceding heading** (a small additive `Table.heading` was added to the Markdown
  parser to enable this) — so "Non-Application To-Do" and each "Task N" breakdown become their own
  milestones; a plain checkbox list tags no group and yields none. A pure `relay.derive.milestones()`
  turns the grouped items into `{group, done, total, at_risk, nearest_due}` rows (progress, the soonest
  **open** deadline, and an at-risk roll-up reusing `count_at_risk` so the milestone and the per-item
  treatment never disagree). Surfaced as a **"Milestones"** section ABOVE the checklist on the project
  page, plus a **"Next: <group> by <date>"** hint on each portfolio card (the soonest-due milestone,
  precomputed by `latest_report_per_project`, today-gated). `group` is redacted on the structured lane
  like `key`. Verified eyes-on against the live tracker: "Applications — 0/4 done · next due Jun 12,
  2026 · 2 at risk", "Non-Application To-Do — 0/8 done", and the card's next-milestone hint, with the
  two at-risk reconciling against the per-item overdue/due-soon dates below. At-risk roll-up only —
  a milestone **slipping** count is deferred (the seam is there; slipping stays per-item for now).

- **Slippage surfaced on the dashboard** (Unit 4, relay-side — deploy after merge). The first
  consumer of the observation history: a pure `relay.derive.is_slipping()` / `slipping_item_keys()`
  flags an **open** item as *slipping* when its history shows either (a) its **deadline moved later**
  (postponed — the signal the rung's eyes-on exercises) or (b) it has **lingered open past due**
  across ≥2 observations. Both arms require history, so slipping is deliberately distinct from Unit 2's
  point-in-time at-risk (a brand-new overdue item is at-risk, not yet slipping; a done item never
  slips). Surfaced as a per-item **"↘ slipping"** marker (violet — its own axis, not a louder overdue)
  on the **project page** checklist, and an **"N slipping"** badge on the portfolio card (count from
  the observation history, gated on today in the display zone). The report page keeps Unit 2's
  treatment — slipping is a project-page signal. Verified eyes-on against the live tracker: a postponed
  application deadline and a lingering-overdue course both show "↘ slipping", while a steady
  due-in-4-days item shows at-risk but *not* slipping.

- **Observed-state memory store** (Unit 3, vertical slice — deploy after merge). The "remember" half
  of the forward-looking layer: a new **append-only** `relay_observed_items(project, item_key,
  due_date, done, observed_at)` table records one observation per checklist item on **every** push
  (both `/checklist` and report ingest), so the relay accumulates history a later slice derives
  slippage from. It is a **downstream projection** — rebuildable from the pushes, authoring nothing.
  The key design point is a **stable `item_key`**: the tracker now emits each application's bare
  **title** as a new optional `ChecklistItem.key` (carried on the wire, redacted), because the item
  `text` embeds the status (`"Title - In progress"`) and so changes when the status does; the relay
  keys observations by `key` when present, else the item `text` (tasks/table items, which carry no
  status, are already stable). This makes an item's identity **survive a status change** — verified
  on the live tracker: advancing an application Not-started → Submitted keeps one `item_key`, so its
  two observations accumulate under one identity. The table is additive (`IF NOT EXISTS`, no
  migration); the read `observed_history()` folds latest-per-key to rebuild current state. Identity
  model + edge cases documented in **KI-21** (complements KI-6). No new UI yet — surfacing slippage
  is Unit 4.

- **Due dates, overdue/at-risk surfaced on the dashboard** (Unit 2, relay-side — deploy after merge).
  The first visible forward-looking win. A new pure module `relay/derive.py` classifies each open,
  dated item against **today in the relay's display zone** (KI-20): `overdue` (deadline before today),
  `due_soon` (within 7 days, inclusive), or neither — done and undated items are never flagged, and a
  date-only deadline is treated as end-of-day so a deadline of *today* is due-soon, not overdue. The
  project page now shows each open item's due date as an enhanceable `<time>` (the existing
  relative-time JS turns it into "in 3 days" / "1 week ago" with no JS change), tinted **overdue**
  (red, with a `⚠` marker) or **at-risk** (amber); the portfolio card gains an **"N at risk" badge**,
  counted in `latest_report_per_project` against the same "today". `due_date` already rode the stored
  checklist JSON (Unit 1), so the store needed no decode change. The 7-day horizon is a constant with
  a per-project `due_soon_days` knob deferred (the function parameter is the seam). Verified end-to-end
  against the live applications tracker: an overdue course deadline renders red with `⚠`, an
  internship due in 4 days renders amber, a fellowship 3 weeks out renders plain, and the card shows
  "2 at risk".

- **Tracker deadlines are parsed and carried** (Unit 1, local-only — no deploy). The `tracker`
  collector now reads the deadline each item already holds — a `- **Deadline:**` / `- **Due:**`
  field, or a `Deadline` / `Due` / `Target` table column — via a new `_parse_deadline` helper, and
  carries it on `ChecklistItem.due_date` (additive; defaults `None`, so the tasks collector and every
  existing call site are untouched). It rides the wire on **both** serialization paths (the report
  blob and the `/checklist` push) as an optional per-item `due_date`, single-sourced through a new
  `report.serialize_checklist_item` and carried through the redaction rebuild. Only **explicit-year**
  formats are accepted — ISO `YYYY-MM-DD`, or `Month D, YYYY` (full/abbreviated month) with trailing
  time/timezone context tolerated. A **year-less** form like `Sun, Jun 14` parses to `None` on
  purpose: inferring the year would mislabel a genuinely-past deadline as upcoming. Parsing never
  raises (a typo yields `None`). Nothing is surfaced yet — deriving overdue/at-risk and rendering it
  is Unit 2. Verified against the real applications tracker (three application deadlines parsed; a
  multi-line `Deadlines:` block and the year-less table rows correctly yield `None`).

### Changed

- **Strategy invariant clarified — observe vs originate** (Unit 0, docs-only, 2026-06-26). The
  forward-state gate is settled: Orion may persist *observed* state — a rebuildable, append-only
  **downstream projection** of what the source docs claimed over time — without violating
  *reframing, not originating*. The invariant is **no *authored* forward state**, not "no *persisted*
  forward state": the line is **observe vs originate**, not derive vs persist. Reconciled
  `docs/orion-strategy.md` (the planning-layer direction moves from "deferred" to "in build") and the
  `plans/orion-plan.md` roadmap framing (E1 lifted out of the not-built grouping → building as E2
  Inc 3). Adds *live, purposeful* memory, distinct from the *dead* Phase-1 state KI-8 removed.

## Consolidation slice — dashboard-home visibility, add-project completeness, KI-8 cleanup (2026-06-25)

A small post-E2-Inc-2.6 cleanup: one dashboard-visibility fix plus three deferred CLI/state
items, each shipped as its own reviewable PR (#52, #53, #54). No new dependencies; the only
deploy was the relay for Unit 1.

### Fixed

- **The dashboard home shows checklist-only projects** (Unit 1, #52). `latest_report_per_project()`
  (`relay/store.py`) is now **project-driven, not report-driven** — its row set is the union of
  projects with a report OR a live checklist — so a dashboard-only project (a live checklist but
  zero reports, e.g. the `applications` tracker) finally gets a portfolio card and a
  `/project/<name>` link instead of being reachable only by direct URL. `render_portfolio()`
  (`relay/render.py`) tolerates a no-report card (omits the headline, falls back to the checklist's
  `updated_at` for last-activity). Relay-only, no schema change; deployed to Fly.

### Added

- **`add-project --tracker-file` / `--incubator-file`** (Unit 2, #53). `render_project_stanza`
  completed its `collector_files` map for all four file-backed collectors, so
  `add-project --collectors tracker` no longer `KeyError`s (the reason the `applications` tracker
  had to be hand-edited into `orion.toml`). Config-only — no file is created (these point at rich
  user docs).
- **`add-project --seed-tasks-from <doc>`** (Unit 3, #53). When a defaulted `tasks_file` is being
  created, seed its checklist from a doc's Markdown tables (reusing `collectors/_markdown.parse_tables`
  — parse, no LLM) instead of the empty starter. Picks the text column by preference and maps an
  optional status column to done/open; done-marker detection uses word boundaries plus a `not` veto
  (so `incomplete` / `not done` stay open). A doc with no usable table warns and falls back to the
  starter — it never fails the add.

### Removed

- **KI-8: the vestigial Phase-1 state artifacts** (Unit 4, #54). Dropped the `project_state` table
  and the one-time `_backfill_git_markers` from `state.py`, and the always-`""` `source_marker` from
  `ReportBlob` / `build_report` / `serialize_blob` (`report.py`). The Phase-1→Phase-2 marker-migration
  window closed long ago (Phase 2 shipped 2026-06-15; live DBs backfilled then, new DBs never had the
  table), and the relay never required `source_marker` — so the wire change is backward-compatible
  (the relay still ignores a missing or extra field). Relay comments updated to match.

### Tests

- `pytest`: **620** green locally (CI quota-capped until 2026-07-01 → merged on local-green). New:
  checklist-only store/render/server cases and interleaved ordering (Unit 1); tracker/incubator
  stanza round-trips and `--seed-tasks-from` done/open seeding + fallback + `_status_is_done` guards
  (Units 2–3); a fresh `open_state()` creates no `project_state` table, and the wire payload
  serializes without `source_marker` (Unit 4). The two legacy-backfill tests were removed with the
  code they covered. Verified by hand: the seeded checklist reads back through the real `tasks`
  snapshot, and the live dashboard shows the `applications` card after deploy.

## Dashboard portfolio overview — visibility surface, increment 1 (2026-06-25)

The first slice of evolving the relay dashboard into a richer multi-project visibility and
showcase surface — the project's founding intent (family/others can see what's being worked on
and comment back, asynchronously, on a dashboard rather than chat). The home page becomes a
cross-project portfolio instead of a flat list. Relay-local and additive: reuses report data
already in the relay, no new blob fields, no schema change, no local-CLI/config changes,
stdlib-only. Functional layout only — a dedicated aesthetic pass is deferred.

### Changed

- **The dashboard home (`GET /`) is now a portfolio overview.** One card per project showing
  the project name (linking to its history), a one-line headline drawn from the latest report's
  first line, the report count, and last activity as a relative time. Replaces the old flat
  name + count + raw-timestamp list. Viewer scope is unchanged — a scoped family viewer still
  sees only their granted projects' cards (the same `_allowed_projects` filter).

### Added

- **`latest_report_per_project()`** (`relay/store.py`) — one query returning each project plus
  its latest report's id and body. "Latest" matches `history()`'s ordering (`generated_at DESC,
  id DESC`), so the home's latest agrees with the project page's, even when a report is
  backfilled out of generation order.
- **`render_portfolio()` + `_headline()`** (`relay/render.py`) — the card render and a pure
  first-non-empty-line extractor (truncates at ~100 chars with an ellipsis; omits the headline
  when the body is empty). Replaces `render_index`. The headline is the report's own
  (attacker-influenceable) body text, so it is `_esc`-escaped like every dynamic value. Lean
  `.portfolio`/`.card` styling lives in `_PAGE_CSS`, so the hash-based CSP auto-tracks it.

### Tests

- `pytest`: **533** (+12). Store tests pin the latest-by-`generated_at` rule (incl. a
  backfill-ordering case proving consistency with `history()`); render tests cover the card,
  headline truncation/escaping/empty-fallback, and `_headline` boundaries; a live-server test
  asserts a project's latest first line appears on `/` end to end. The CSP contract test stays
  green after `_PAGE_CSS` changed (auto-recompute). Verified eyes-on in a real browser:
  multi-project cards, correct latest-report headlines, working relative times, no CSP violation.

## Dashboard security hardening — CSP + headers (2026-06-24)

A small, `relay/`-local hardening slice following C3 Increment 1, which put a login-gated,
comment-bearing dashboard on the public internet. No user-facing behavior changes — this adds
defense-in-depth to an internet-facing surface that renders user-influenced text (comments,
project names). Stdlib-only, no new dependencies.

### Added

- **Hash-based Content-Security-Policy** on every dashboard HTML response. The two inline blocks
  the page renders (`_PAGE_CSS`, `_PAGE_JS`) are allowlisted by the SHA-256 of their content,
  computed in `relay/render.py` (`PAGE_CSS_HASH` / `PAGE_JS_HASH`) from the SAME constants `_page()`
  emits — so the policy can never drift from the markup, and no `unsafe-inline` is needed (the
  inline-asset choice from C1 is kept intact). The policy locks everything else down:
  `default-src 'self'`, `base-uri 'none'`, `form-action 'self'`, `frame-ancestors 'none'`,
  `object-src 'none'`.
- **Standard security headers.** `X-Content-Type-Options: nosniff` and `Referrer-Policy:
  no-referrer` on all responses; `X-Frame-Options: DENY` on HTML; `Strict-Transport-Security`
  (2-year, `includeSubDomains`) only when HTTPS-exposed (gated on the same hosted signal the
  cookie's `Secure` attribute uses, so a plain-http loopback dev relay does not send it).

### Tests

- `pytest`: **521** (+3). A render-side **contract test** recomputes the inline blocks' hashes the
  way a browser does and pins them to the exposed constants, guarding the invariant that the CSP can
  never block the dashboard's own CSS/JS. Server tests assert the CSP (with both hashes) and headers
  ride a dashboard GET, and that HSTS appears only in a hosted posture. Verified eyes-on against the
  rendered page in a real browser: styling and the relative-time JS both run with **no** CSP
  violation in the console.

### Fixed

- **KI-19** (dashboard served inline CSS/JS with no CSP) — resolved by the above.

## C3 — multi-party dashboard access, Increment 1 (2026-06-24 – 2026-06-25)

Brings per-user identity and access control into the relay dashboard (the C3 multi-party
"watershed"), integrated from the start rather than bolted on later. The driver is real
dogfooding: share a project's state with a helper or supervisor, control who sees which
project, and a guest/showcase view. Built in four stacked PRs (#39 access foundation, #43
provisioning [replaced #40], #41 follow-ons, #42 test isolation), merged to `main` and **deployed
to production** at `orion-relay-horizon-c.fly.dev`. Stdlib-only, no new dependencies. A Codex
`/second-opinion` hardened the design.

### Added

- **Per-user login + sessions.** The dashboard is now gated by a per-user **login key** and a
  signed, stateless **session cookie** (`GET`/`POST /login`, `GET /logout`), replacing the C2
  shared-view HTTP Basic prompt. The cookie carries only an id, a version, and an expiry
  (`HttpOnly; SameSite=Lax; Secure` when hosted); the user's role and project scope are re-read
  from the database on every request, so a change applies immediately. Sessions persist across
  restarts and expire server-side (`--session-days`, default 30).
- **Roles + per-project read scope.** Two roles today: `admin` (sees all projects, provisions
  users) and `viewer` (scoped). A viewer sees only granted projects on the index, and any project
  or report outside their scope returns **404** so its existence stays hidden. Default-deny: a
  viewer with no grants sees nothing.
- **Stateless revocation.** Revoking a user deactivates them and bumps a `session_version` in one
  step, so their key stops logging in and any cookie already in a browser dies on its next request,
  with no server-side session store.
- **Relay admin API.** `POST /api/users` (provision: mint a key once, store only its peppered
  verifier, audit), `GET /api/users` (roster, no credential material), and `POST /api/users/revoke`,
  all authenticated by the SEPARATE admin token. An append-only `relay_admin_audit` trail records
  who provisioned or revoked whom.
- **`relay-user add` / `list` / `revoke`** — the admin CLI over that API. `add` prints the new
  user's access key **once** (it is never stored or retrievable later); `list` shows roles, status,
  and scope (no credentials); `revoke` cuts off access immediately.
- **Three independent relay secrets** (each its own `.env` variable, never derived from one
  another or from the ingest/view tokens): `ORION_RELAY_SESSION_KEY` (cookie signing),
  `ORION_RELAY_USER_PEPPER` (key verifier), `ORION_RELAY_ADMIN_TOKEN` (provisioning). An optional
  `[relay].admin_token_env_var` config field names the last one.

### Changed

- **`ORION_RELAY_VIEW_TOKEN` is repurposed, not removed.** It is no longer an HTTP Basic password.
  It is now the **legacy bootstrap-admin login key**, usable to log in only while no users have been
  provisioned (or with an explicit `--allow-legacy-admin` opt-in). It still gates the dashboard and
  still satisfies the fail-closed guard that a non-loopback bind must carry a read secret.
- **`relay-serve` fails closed** when the dashboard is access-gated (a view token or an admin token
  is set) but the session secrets are missing, rather than serving a login that could never work.

### Tests

- `pytest`: **505** (relay server + admin API, the cookie/crypto core, the `relay-user` CLI, and
  config parsing). Verified eyes-on against a live relay: login → cookie → scoped view, logout,
  tampered/expired/revoked cookie rejection, the full `relay-user` add → login → revoke lifecycle,
  the ingest token rejected at `/api/users`, and no secret in any log line.

### Notes

- Hardening folded in from the Codex `/second-opinion`: independent secrets (bound blast radius),
  trust the DB not the cookie (no privilege rides in a forgeable/stale cookie), a peppered key
  verifier (a DB leak alone cannot test candidate keys), the gated/deprecated legacy admin, and a
  canonical-`Origin` CSRF check (do not blind-trust `Host`). A slow KDF is intentionally not used:
  the keys are server-minted and ≥256-bit random, so there is nothing to brute-force.
- Increment-1 non-goals (named seams, not built): contributor/write access, a guest/demo role,
  self-service signup, per-recipient delivery state, and upgrading the comment author to the
  authenticated identity (KI-17).

## Horizon D — onboarding & visibility (2026-06-23 – 2026-06-24)

Opens Horizon D (OSS-readiness & local enhancements), acting on the hackathon dogfood's #1 finding
(onboarding friction) and the cross-project visibility gap.

### Added

- **`orion add-project`** — the first command that writes `orion.toml`, and the only one. Register a
  project from inside its own directory in one step: it infers the name (the folder) and repo path
  (the git top level), copies recipients from another project with `--like` or takes them via
  `--recipient "Name:channel:ENV_VAR"`, previews the stanza, and appends it (never rewrites existing
  content). Creates a minimal config when none exists. `--print` shows without writing; `--yes` is
  non-interactive. This refines the "Orion never writes config" rule to "never as a *side effect* of a
  run" — see `plans/orion-plan.md` "D1". Dependency-free (stdlib has no TOML
  writer; an appended known-shape stanza needs none).
- **`orion status`** — a read-only, cross-project digest of what still needs reporting. Per project it
  shows new unreported activity (`new: git`) or `up to date`, plus how long since the last report. It
  reuses the report flow's activity detector so it can't disagree with a real `report`, and derives
  the last-report time from `report_history` (no new schema). Fail-soft per collector; nothing is sent.
- **Per-recipient signal routing (D5).** A recipient can now carry an optional `signals` filter — a
  subset of the project's `collectors` — so different people receive different slices of one project's
  report (a mentor gets your notes, a teammate gets git). Omitting it keeps today's behavior (the
  recipient receives everything). Each run composes one **filtered** message per distinct
  `(channel, signals)` audience and previews/delivers per audience, so two recipients who want the same
  slice on the same channel share one rendering. The filter is config-level *content* routing on the
  existing named-recipient seam — **no per-recipient identity or state** (that stays deferred to C3).
  The hosted relay still receives the **full, unfiltered report**; only chat-channel delivery is
  filtered. An unknown or empty `signals` is rejected at load time. See `plans/orion-plan.md` "D5".
- **Incubator signal (D4)** — a fifth collector, `collectors/incubator.py`, that reads an idea-pipeline
  Markdown table (an incubator's `index.md`) and reports **new ideas** (with their one-line pitch) and
  **status transitions** (`refining → validated`). Structured lane (no LLM), following the `tasks.py`
  delta pattern: the marker is the full `{idea: status}` map as sorted-keys JSON, so an unchanged table
  reports nothing. The parser locates the Idea/Status columns by header (tolerating re-ordered or extra
  columns and a missing pitch column) and identifies an idea by its title, unwrapping a `[Title](path)`
  link. A missing file is a clear `IncubatorError`; a file with no idea table is a valid empty pipeline.
  Enable it like any file-backed collector (`collectors = ["incubator"]`, `incubator_file = "…"`); the
  intended use is a dedicated `[projects.incubator]` routed to mentors/family via D5 `signals`
  (example in `orion.toml.example`). See `plans/orion-plan.md` "D4".
- **`orion graduate-idea` (D4 follow-on).** Turn a **graduated** incubator idea into a tracked project
  in one step: it reads the incubator index (from the configured incubator project, or
  `--incubator-file`), matches the idea title case-insensitively, checks its status is `graduated`
  (`--force` to override), derives the project name by slugifying the title (`--name` to override),
  then **delegates to `add-project`** — so the preview-before-write, recipient resolution, repo
  inference, and re-load validation are all reused. It is read-only on the incubator file (it writes
  only `orion.toml`). Shares add-project's flags (`--like`, `--recipient`, `--print`, `--yes`, …).
- **`orion relay-serve --timezone <zone>` (KI-20 follow-up).** The hosted dashboard's display zone,
  previously hardcoded to `America/Los_Angeles`, is now configurable per relay process. The zone is
  validated at startup (a typo fails cleanly, not with a traceback) and threaded through the renderers;
  omitting the flag keeps the Pacific output byte-identical. The relay does not read `orion.toml`, so
  the flag is the source.

### Changed

- **Summarizer prose style (idea #7).** The summarizer system prompt now instructs the model to write
  clean prose — no em-dashes, no semicolons, and no generic LLM filler — so progress summaries match
  the project's Writing & Documentation Style. A lean one-line prompt tune (deliberately not a
  doc-inheritance mechanism); applies to both share levels.
- **OSS-readiness docs polish (D3).** README status line + blurb now reflect Horizons A–C shipped /
  Horizon D underway and surface `add-project` and `status`; a new "How it compares" section positions
  Orion honestly against incumbents (Gitmore, Gitrecap, dev-journal). `docs/new-project-setup.md` leads
  with `add-project` (hand-edit kept as the alternative). The runtime-deps count is corrected to 3
  (`anthropic`, `python-dotenv`, `tzdata`).

### Removed

- The README "Strategic assessment" pointer to a private `~/Developer/incubator/...` path (it would
  404 for anyone cloning the repo). The new "How it compares" section carries the positioning
  self-contained.

## C2-bots — native Slack bot: two-way in chat (2026-06-19)

The next Horizon-C slice (`docs/phase-c2-bots-kickoff.md`): an
always-on **Slack bot** so a supervisor's reply in a mapped channel lands in the **existing relay
comment store** — visible on the dashboard and via `orion comments`, unchanged. Built in four
reviewable checkpoints (PRs #16–#19). `pytest`: **367**. The first new runtime dependency
(`slack-bolt`) is **optional and lazily imported**, so the core clone-and-run install stays at three
deps. Smallest first slice: one platform, no slash commands, replies attach to a project's *latest*
report (no message→report map yet — the relay endpoint already accepts an optional `report_id` for
that later, additive change).

### Added

- **Relay `POST /api/comments` (`relay/server.py`).** A Bearer-authed machine sibling of the existing
  `GET /api/comments` pull-back: validates `{project, body, author?, report_id?}`, attaches the
  comment to the project's latest report (or an explicit `report_id`), reuses `add_comment`. **No
  CSRF check** (a Bearer token isn't browser-auto-attached). Comments stay non-redaction-scanned
  (inbound, access-gated) — the outbound redaction rule is untouched.
- **Bot package (`src/orion/bot/`), split pure-core / sync-shell.** `core.py` — `decide_forward`, the
  pure decision logic encoding the inbound threat model (drop bot/webhook authors → loop prevention,
  drop subtyped events, forward only configured channels, strip + cap). `relay_client.py` — a sync
  stdlib-`urllib` `post_comment` (URL derived from the one `[relay].url`, failures → `DeliveryError`).
  `slack_bot.py` — the thin Socket Mode shell (the only file importing `slack-bolt`, lazily): event
  normalization, fail-soft author resolution, and a fail-soft decide-then-relay handler.
- **`orion bot` command + `[bot]` config.** `BotConfig` + `SUPPORTED_BOT_PLATFORMS` + `_parse_bot`
  (global opt-in table: platform, the two token env-var *names*, channel→project bindings validated
  against real project names; reuses `[relay]` as the write target). `cmd_bot` mirrors
  `cmd_relay_serve` (blocking, Ctrl-C clean stop), gated on both `[bot]` and `[relay]` enabled.
- **Optional extra `slack-bot = ["slack-bolt>=1.18"]`** + docs: [`docs/slack-bot.md`](docs/slack-bot.md)
  (Slack-app walkthrough, limits, threat model), `orion.toml.example` `[bot]` block, `.env.example`
  Slack tokens.

### Tests

- 53 new across the four checkpoints: the relay endpoint (auth/validation/caps/latest-resolution/
  `report_id` override/no-reports-404/route coexistence), the pure core (every guard + boundary), the
  relay client (URL derivation + error translation), the Slack shell glue (testable without
  `slack-bolt`; missing-dep `ConfigError` via `skipif`), the `[bot]` config validation, and `cmd_bot`
  arg/gate/secret wiring.

### Notes

- The optional-extra / lazy-import shape is a **stage-bound** smallest-slice choice, expected to
  graduate to a first-class integration as the bot becomes load-bearing; the seams (pure core, relay
  client, platform-neutral config, `report_id`-optional endpoint) keep that additive.

## KI-20 — configurable message timezone, aligned with the dashboard (2026-06-19)

A small consistency fix: delivered Slack/Discord messages timestamped in **UTC** while the
dashboard rendered **Pacific**, so the same report showed two different times. `pytest`: **308**.

### Added / Changed

- **`display_timezone` config field (`config.py`, global, optional).** Defaults to
  `America/Los_Angeles`, validated at load time as a real IANA zone (a bad value is a clean
  `ConfigError` naming it). The message formatter (`compose._format_timestamp`) now converts the
  stored UTC instant to this zone, so a delivered message reads the **same** time as the dashboard
  by default (DST-correct PDT/PST). A user with non-Pacific recipients can set `display_timezone =
  "UTC"` (or any zone). **Behavior change:** message timestamps now default to Pacific, not UTC.
- The display zone is threaded through `compose` → the channel builders → `_format_timestamp`; the
  CLI passes `config.display_timezone` at both send sites (`report`, `intake`).

### Known follow-up

- The relay dashboard's zone is still independently hardcoded to Pacific (`relay/render.py`), so an
  *override* to a non-Pacific zone wouldn't reflect on the dashboard yet. Making the relay's zone
  configurable too is the additive next step (KI-20, deferred to avoid touching the deployed relay
  in this local-side fix).

## C2 comment pull-back — `orion comments` + unread tracking + skill surfacing (2026-06-19)

Closes the C2 loop **into the developer's workflow**: supervisor replies that previously lived only
on the dashboard are now pulled back to the machine where you work. The first C2 slice made the loop
two-way *on the dashboard*; this increment surfaces those replies where you report. All stdlib, **no
new dependencies**. `pytest`: **304**. See `docs/phase-c2-kickoff.md`.

### Added

- **`orion comments <project>` command (`src/orion/cli.py`).** Pulls a project's supervisor comments
  back from the relay. `--all` re-reads everything without moving the unread marker; `--json` emits
  the raw response for the session skill; the default is a human listing
  (`author · <Pacific time> · body`). Requires an enabled `[relay]` (a clear error otherwise) and
  authenticates with the **same Bearer token the push uses** — whoever can push a project's reports
  can read its replies. A failed pull advances nothing.
- **Relay read endpoint `GET /api/comments?project=&since_id=` (`relay/server.py`).** A Bearer-authed
  machine-JSON route, dispatched *before* the dashboard's Basic gate so the two consumers keep
  distinct auth schemes cleanly. Validates `project` (required) and `since_id` (non-negative int or
  400); returns `{"comments": [...], "latest_id": N}`, an empty 200 for a project with nothing new
  (matching the dashboard's empty-state philosophy). Backed by a new `comments_for_project` store
  query (`relay/store.py`) that JOINs comments→reports to resolve the by-project link the flat schema
  can't.
- **Local pull client `pull_comments` (`src/orion/delivery/relay.py`).** Mirrors `push` (stdlib
  `urllib`, Bearer auth, `DeliveryError` mapping); derives the read URL from the configured ingest
  URL via `urljoin(url, "/api/comments")`, so one configured `url` serves both directions.
- **Unread watermark (`src/orion/state.py`, `comment_watermark` table).** A per-`(project, relay_url)`
  cursor (`get/set_comment_watermark`) so each pull shows only what's new and changing relays starts a
  fresh cursor. Kept separate from `collector_markers` — a read-cursor is not a report delta.
- **Session-skill surfacing (`orion-session` skill).** After a successful `orion intake`, the skill
  runs `orion comments … --json` and surfaces any new replies in-session ("Since your last report,
  <supervisor> said: …"), closing the loop without opening the dashboard.

### Why it's shaped this way

- **Pull by project, watermark local.** The local side never recorded the relay's comment ids (the
  push discards them), so the natural handle is the project. "Unread" is a per-developer notion, so
  the relay stays a dumb append-only store and the cursor lives locally. Ids are a monotonic
  autoincrement, so `id > since_id` is a robust cursor with no clock/tie issues.
- **Comments aren't redaction-scanned** (same reasoning as the comment POST: inbound supervisor text
  on an access-gated relay, not the developer's outbound secrets) — but the endpoint is still
  Bearer-gated, auth checked before any query.

## Dashboard maturation — refined-minimal restyle + Pacific time + relative timestamps (2026-06-19)

The relay dashboard is now genuinely **supervisor-facing** (post-C2 deploy), so it gets a quality
pass: a refined-minimal restyle, California-time display, and a relative-timestamp progressive
enhancement. Server-rendered HTML stays the source of truth — every page is fully functional with
**no JavaScript**. `pytest`: **278**.

### Added

- **Refined-minimal restyle (`relay/render.py`, `_PAGE_CSS`).** A token-based palette (CSS custom
  properties) defined once each for light and dark (`prefers-color-scheme`); a real type scale and
  vertical rhythm; styled list rows (link left / quiet meta right), report `<pre>` blocks on a
  bordered surface, the comment thread + form, and a `:focus-visible` ring across links, buttons,
  and inputs. Evolves the existing minimal look — no framework, no build step.
- **Relative timestamps (progressive enhancement).** Timestamps now render as
  `<time datetime="<ISO>">…</time>` (new `_time_tag`); one small inline script (`_PAGE_JS`) rewrites
  them to "2 days ago" with the absolute time on hover. With JS off, the absolute time stands. The
  script writes via `textContent` only (never `innerHTML`) and reads only server-escaped values, so
  it adds **no XSS surface** — the every-value-escaped rule still covers safety.

### Changed

- **Dashboard timestamps now display in California time (`_format_ts`).** Converts to a fixed
  `America/Los_Angeles` zone via stdlib `zoneinfo` and labels with the live abbreviation, so it
  reads **PDT in summer, PST in winter** automatically (not the server's or reader's local time).
- **New runtime dependency: `tzdata`** (`pyproject.toml`). The IANA tz database for `zoneinfo` —
  data-only, no transitive deps, CPython-core-maintained. Required because `python:*-slim` (the
  relay's Docker base) and native Windows lack a system tz database. Runtime deps are now
  `anthropic`, `python-dotenv`, `tzdata`.

### Notes

- New known issues recorded: **KI-19** (inline CSS/JS vs. a future Content-Security-Policy) and
  **KI-20** (delivered Slack/Discord messages still timestamp in UTC while the dashboard is Pacific
  — to align when delivery is next enriched).

## C2 first slice — dashboard comments (2026-06-19)

**Orion's first inbound write surface.** Supervisors can comment back on a report from the
dashboard — making the loop two-way. The slice extends the live relay (no bot, no new infra); its
center of gravity is inbound security, gated more tightly than the feature itself. Comments are
append-only, flat, plain text, with an optional self-entered display name (free text, **not**
authenticated identity — that is C3). `pytest`: **276**. **Deployed & verified live (2026-06-19):**
`fly deploy`'d to the running relay and exercised through the real dashboard — comments (with and
without a name) persisted across a page refresh and a full server restart, confirming the volume DB
auto-migrated the `report_comments` table.

### Added

- **`POST /report/<id>/comment` (`relay/server.py`).** The comment write path, enforced in order:
  **auth** (reuses the dashboard view secret over HTTP Basic — read access == comment access; open
  on loopback dev), **CSRF** (a new `_origin_error` requires the `Origin` header present and its
  host equal to the request `Host`; works behind the Fly proxy), **validation** (parses the
  urlencoded form; requires a non-empty body within length caps; rejects oversized/empty with 400),
  **report-existence** (404 for a stale/forged id), then store + **303 POST-redirect-GET** so a
  refresh does not resubmit. `do_POST` is now a small router (`_handle_ingest` / `_handle_comment`).
- **`report_comments` table + `add_comment` / `comments_for` (`relay/store.py`).** Auto-migrating
  (the existing `CREATE TABLE IF NOT EXISTS` on open creates it on the deployed volume at next
  startup — no manual migration). Append-only; oldest-first reads.
- **Comments section on the report page (`relay/render.py`).** `render_report` gains an optional
  `comments` argument (additive — existing callers unaffected) and renders the escaped thread plus a
  plain-HTML post form. **Every** comment field (author, body, timestamp) goes through the existing
  `_esc` path — the stored-XSS defense; pinned by render tests that neutralize a `<script>` comment.
- **Tests:** comment store round-trip/ordering/scoping; the full POST checklist (stored+303,
  no-creds 401, cross-origin/missing-Origin 403, missing-report 404, empty/oversized 400); render
  escaping + form presence.

### Why

- **Not redaction-scanned, deliberately.** Redaction is an *outbound* control for the developer's
  own secrets; an inbound supervisor comment shown only on the access-gated dashboard is a different
  threat — XSS-escaping on render is the relevant control, not redaction.
- **Length caps live in `render.py`, imported by `server.py`** — one definition shared by the form's
  `maxlength` hint and the server's authoritative enforcement (server already imports render; the
  reverse would be a cycle).

## Post-C1 deploy — Fly artifacts + config hardening (2026-06-19)

**C1 is deployed.** The relay runs on Fly.io over HTTPS, reachable by a supervisor, verified end to
end (local `intake` → relay → auth-gated dashboard). Plus a security hardening surfaced by the
deploy. `pytest`: **257**.

### Added

- **`fly.toml` + `.dockerignore`:** a committed Fly.io deploy config (volume mount, port 8787,
  force-HTTPS, scale-to-zero) and a build-context ignore list (keeps `.env` / `orion.toml` / sqlite
  out of the image build) — makes the Fly deploy reproducible.

### Changed

- **`docs/deployment.md`:** Option A references the committed `fly.toml`; added a "Common gotchas"
  section from the first real deploy — `*_env_var` holds a variable NAME not the secret value, "say
  yes to a single volume" (a single-writer SQLite store wants one), and the scale-to-zero cold-start
  note.

### Fixed

- **`*_env_var` value-vs-name footgun (config validation).** `token_env_var`, `webhook_env_var`, and
  `api_key_env` are now validated to look like environment-variable NAMES at config load. Pasting the
  secret VALUE where the name belongs fails with a clear `ConfigError` — and no longer **echoes the
  secret** (the old "secret '<value>' is not set" path leaked it). Heuristic, but catches the common
  shapes (leading digits, hyphens, URL punctuation).

## C1 second slice — deploy-safe relay + dashboard hardening (2026-06-18)

Makes the C1 relay safe to host **beyond loopback** and polishes the read-only dashboard, so it can
give a supervisor a real URL. Local collection / redaction / delivery is unchanged. `pytest`: **254**.

### Added

- **Dashboard read auth (HTTP Basic):** the relay's web view is gated by a shared view secret
  (`ORION_RELAY_VIEW_TOKEN`, any username + this as the password). Enforced **only when set**, so
  loopback dev stays password-free.
- **Fail-closed bind guard:** the relay refuses to start if it binds a non-loopback host without a
  view secret — a world-readable dashboard is impossible by construction.
- **`relay-serve --require-view-auth`:** forces the view secret even on a loopback bind, for the
  reverse-proxy topology where the host-based guard alone can't see the dashboard is publicly exposed.
- **Deployment recipe:** a host-agnostic `Dockerfile`, `Caddyfile.example`, and a
  `docs/deployment.md` runbook (Docker / Fly / Render and reverse-proxy paths, with a
  build → smoke-test → deploy flow).

### Changed

- **Dashboard hardening:** report id on the detail page, a project breadcrumb + a 404 back-link,
  UTC-humanized timestamps, a section-count badge, keyboard-focus styling, and an empty-participants
  guard.

### Fixed

- **KI-18 — proxy-exposure blind spot:** the fail-closed guard keyed off the *bind host*, so a
  loopback bind behind a reverse proxy wasn't required to set a view secret (the proxy could expose an
  unauthenticated dashboard). `--require-view-auth` closes it — a forgotten secret now fails closed;
  the deploy recipe prescribes the flag for the proxy topology.

## Post-C1 — OSS-readiness pass: CI, contributor docs, onboarding friction (2026-06-18)

The hosting-agnostic polish that follows the C1 slice — making Orion ready to be public and
smoother to onboard. No change to the core pipeline's behavior. `pytest`: **228**.

### Added

- **CI** (`.github/workflows/ci.yml`): GitHub Actions runs the suite on push to `main` and every
  pull request across {Linux, macOS, Windows} × {Python 3.11, 3.12, 3.13} — finally *enforcing*
  the cross-platform claim (it immediately caught two Windows-only test bugs, since fixed).
- **OSS front door:** `CONTRIBUTING.md`, `SECURITY.md` (private vulnerability reporting), and
  `.github/` issue + PR templates.
- **`orion baseline <project>`:** records a project's current state as already-reported **without
  sending**, so a new project's first report covers only new activity instead of dumping its
  entire git history. Reuses the existing collector path + `set_marker` (no new collector API).
- **`ORION_CONFIG` env var:** `--config` falls back to `$ORION_CONFIG`, so non-interactive callers
  (the session skill, git hooks, schedulers) can set the config path once instead of passing
  `--config` every time.

### Changed

- README/docs polish: CI badge, a "Confirm it works" checkpoint, a sharpened WSL2-cron caveat, and
  the new `baseline` / `ORION_CONFIG` notes; the `orion-session` skill can skip asking for the
  absolute config path when `ORION_CONFIG` is set.

### Decided

- **KI-1 (multi-recipient partial-failure):** keep advancing state on **≥1** successful delivery
  (not all-or-nothing, which would let a permanently-broken recipient block everyone and re-spam
  the working ones). Per-recipient delivery state is the real fix, deferred to **C3** (with KI-11).
  See [`docs/known-issues.md`](docs/known-issues.md).

### Fixed

- Two Windows-only test-fixture bugs surfaced by CI's first run: repo paths written into a
  double-quoted TOML string (Windows `\` read as escapes → now `as_posix()` forward slashes), and a
  `/tmp/...` assertion that doesn't hold under Windows path separators. Production code was
  unaffected and already documented the forward-slash guidance.

## Phase C1 (first slice) — Hosting-agnostic relay + read-only dashboard (2026-06-18)

Opens **Horizon C** (local-first → hosted/hybrid) with the part buildable *without* settling the
hosting decision (Path A Cloudflare vs Path B self-host stays deferred, and is *informed* by this
slice). Local Orion gains one **opt-in, fail-soft** outbound seam — "serialize the portable blob +
a Bearer token → POST to a configured URL" — and a **Path-B reference implementation** of the
hosted half (a small stdlib Python relay + read-only dashboard) lands in a new, separately-
deployable top-level `relay/` package. **Zero new dependencies**, **no core-pipeline changes**.
`pytest`: **226** (was 172 at B-close; +54). Awaiting sign-off.

### Added

- **Portable blob contract, `serialize_blob` (`report.py`).** A single, explicit,
  `orion_version`-stamped JSON serialization of the report blob (tuples → arrays) — the documented
  contract the relay seam rests on.
- **`[relay]` config (`RelayConfig` + `_parse_relay`).** A global, opt-in table (`enabled`, `url`,
  `token_env_var`), validated like `[summarizer]`; absent/disabled is a pure no-op, so every
  existing config is unchanged. Token lives in `.env` (named by `token_env_var`), never in config.
- **Relay push target (`delivery/relay.py`).** A `push(blob_json, url, token)` sender mirroring the
  Discord/Slack pattern (stdlib `urllib`), sending the serialized blob **verbatim** with an
  `Authorization: Bearer` header.
- **Wired, fail-soft, into `report` and `intake`.** After a successful delivery, the blob is also
  pushed to the relay (once per run). A relay error is reported but **never** fails the run or
  blocks state advancement. `check` now also reports relay-token readiness (a missing token is a
  *warning*, not a failure — the report still sends).
- **`relay/` package (the hosted half, separately deployable; not part of the `orion` wheel):**
  - **`store.py`** — its own SQLite store mirroring the `report_history` shape (no dependency on
    `orion.state`).
  - **`server.py`** — `ThreadingHTTPServer` with `POST /ingest` (**Bearer auth** via
    `hmac.compare_digest`, payload **shape/version validation**, store → **201**) and the read-only
    dashboard GET routes (`/`, `/project/<name>`, `/report/<id>`, 404 otherwise).
  - **`render.py`** — server-rendered HTML (stdlib f-strings, inline CSS, no JS/template engine)
    with `html.escape` on **every** dynamic value (XSS-safe).
- **`orion relay-serve`** — launches the local reference relay (`--host` default `127.0.0.1`,
  `--port` 8787, `--db`, `--token-env`); reads the ingest token from `.env` with a clean
  `SecretsError` when missing.
- **`docs/new-project-setup.md`** — a hands-off clone-to-first-report recipe, including the relay
  dashboard quickstart.

### Security

- The relay ingest is **authenticated** (Bearer token, constant-time compared, never echoed) and
  **validated** (shape + version) before storage — Orion's first inbound surface, handled per the
  "authenticate + validate" must-hold. A 401 distinguishes a missing header from a wrong token
  (no secret is leaked: a single shared token has no identity to enumerate) and advertises
  `WWW-Authenticate: Bearer`.
- The dashboard renders **redacted** content only (the blob is twice-redacted before it leaves the
  machine) and **escapes every dynamic value**. Its access boundary at this stage is **loopback
  binding**; real read-auth rides with the deferred hosting decision.

## Phase B5 — Scheduling layer: gate evaluated, deferred into Horizon C (2026-06-17)

Phase B5 was **⏳ Conditional** — a scheduling *layer* (`report --all --due`, activity-gating,
quiet hours, per-recipient cadence) to be built **only if** OS-delegation had been outgrown. The
gate was evaluated this session and the decision is to **defer B5 and fold it into Horizon C**
(no B5 code). Mixed cadences are already expressible with multiple OS-scheduler entries,
activity-gating already exists implicitly (a no-delta run is already a no-op), and the plan's own
sequencing analysis puts the real need for in-Orion scheduling state alongside the Horizon-C
bidirectional listener that would host it. The seam stays clean (KI-13: no new schema needed —
last-report time is derivable from `report_history`), so a later build is additive. With B1–B4
and B6 signed off and B5 deferred, **Horizon B's local-automation scope is complete.** Details in
`plans/orion-plan.md` (Phase B5 status) and
[`docs/known-issues.md`](docs/known-issues.md) (KI-13). No code changed; `pytest`: **172/172**.

### Verified

- **Carried-over B4 live/manual verification completed.** The optional end-to-end checks deferred
  at B4 sign-off were run against a throwaway git repo delivering to the real Discord + Slack
  channels: (1) the **default Anthropic/Haiku** path rendered a summary and delivered, with a
  seeded fake AWS key redacted from the detailed diff; (2) a **local** backend (Ollama,
  `qwen2.5:0.5b`) rendered a local-model summary and delivered; (3) the local backend **failed
  closed** on an unreachable endpoint — clean `SummarizerError` (exit 1), nothing sent, state not
  advanced (same delta re-reported on the next successful run). No follow-up fixes needed; this
  confirms the B4 provider seam end to end.

### Changed

- **Docs: local-model guidance clarified to "lightest *adequate*".** `README.md` and
  `orion.toml.example` now frame the local `[summarizer]` model choice around quality-adequacy —
  Haiku 4.5 as the bar, with a note that very small models degrade noticeably (observed at 0.5B
  during B4 verification) — rather than implying either that a large model is *required* or that
  the tiniest model suffices. KI-4 records the capability floor and a possible (non-foundational)
  model-tier comparison; the `plans/orion-plan.md` "Horizon B → C boundary review" captures it
  alongside the webhook→bot / dashboard-first direction notes.

## Phase B4 — Summarizer flexibility: provider-agnostic seam + local model (2026-06-17)

The summarizer step is no longer hardwired to one Anthropic model. A small **provider-agnostic
seam** lets the model — and the provider, including an optional **local** model — be chosen by
config, while the default stays the lightest adequate one (Claude Haiku 4.5) and existing
`orion.toml` files keep working unchanged. Internal flexibility only: no new signal, no new
channel, and redaction + preview-before-send are identical for every backend. Net new runtime
dependencies: **0** (the local backend uses stdlib `urllib`).

### Added

- **Optional global `[summarizer]` table** (`config.py`) — `provider` (`anthropic` default, or
  `local`) and `model`, plus `base_url`/`api_key_env` for the local backend. Validated like the
  existing `share_level`/`collectors` checks (`SUMMARIZER_PROVIDERS`; an explicit `model` and
  `base_url` are required for `local`, since there is no universal default for either). Absent
  table → Anthropic/Haiku, so the common case needs no config and upgrades change nothing.
- **`Summarizer` seam** (`summarize.py`) — a one-method `Summarizer` Protocol
  (`summarize(text, share_level) -> str`) with two backends: **`AnthropicSummarizer`** (the
  former single call, now taking the configured model) and **`LocalSummarizer`**, which POSTs
  the OpenAI-compatible `/chat/completions` shape to any local endpoint (Ollama / llama.cpp /
  LM Studio / vLLM) over stdlib `urllib`. Both share the one security-relevant system prompt and
  the empty-result guard; every backend translates its failures into `SummarizerError` (fail
  closed). `SummarizerError` still wraps each provider so the rest of the codebase never imports
  a provider SDK.
- **Backend-aware `check`** (`cli.py`) — readiness now reports the *configured* summarizer's
  required secret: `ANTHROPIC_API_KEY` for Anthropic, the named `api_key_env` for a keyed local
  endpoint, or prints that **no API key is required** for a keyless local one.
- **Tests** — 172 total (+18): the local backend (OpenAI-shape POST, configured model + shared
  security prompt, Bearer auth only when keyed, fail-closed on unreachable/HTTP-error/bad-shape/
  empty), the Anthropic backend behind the seam (configured model flows through; API errors
  wrap), `_build_summarizer` dispatch + the per-provider key-fetch policy, the `[summarizer]`
  config validation, and `check` for a keyless / keyed local backend.

### Changed

- **`summarize.py` generalized behind the seam** — the module-level `MODEL` constant and the
  standalone `summarize_raw(text, share_level, *, client)` are **removed**; the call lives in
  `AnthropicSummarizer.summarize`, and `cli._build_summarizer(summarizer_cfg, get_required)`
  constructs the configured backend (explicit provider dispatch, mirroring `_sender_for` /
  `_collect_for` — not a registry). The lazy "build only when a raw collector has activity"
  behavior is preserved, so a structured-only run still needs no key or client. The shared CLI
  test fixture patches the seam (`cli._build_summarizer` via `conftest.use_summary`) instead of
  the old `cli.summarize_raw`.

### Notes

- **Per-step model choice deferred.** The roadmap lists it, but there is currently **one** LLM
  step (the git summary), so per-step selection would be premature; the seam keeps it an additive
  change when a second LLM step appears (build seams, not futures).
- **OpenAI-compatible shape, not a runtime's native API.** The local backend targets
  `/chat/completions` (the common denominator across local runtimes) rather than, e.g., Ollama's
  native `/api/chat` — one code path for all of them. Tradeoff and the conditions under which it
  might change are tracked as **KI-16**.

## Phase B3 — Richer rendering: Slack Block Kit + Discord embeds (2026-06-17)

Reports now render as each channel's **native structured format** — a Discord **embed** and a
Slack **Block Kit** message — instead of a single plain string, done as one paired upgrade. This
is presentation-only: no new signal, no new cadence, and every privacy guarantee is unchanged
(both redaction passes still run; preview-before-send still shows exactly what is sent). Net new
runtime dependencies: 0. Resolves KI-9; KI-10 updated (still scoped, now applied per section).

### Added

- **Discord embeds** (`compose.py`) — a titled, colored embed card with one **field per signal
  section** (Markdown preserved in field values for Discord to render). No `content` is sent
  alongside the embed (Discord would render both, duplicating the report).
- **Slack Block Kit** (`compose.py`) — a `header` block, a `context` line for the date, and one
  `section` block per signal with dividers between them, plus a `text` field as the notification/
  accessibility fallback (Slack does not show it in place of the blocks).
- **Faithful preview from the payload** — `ComposedMessage.preview` is rendered from the actual
  payload's text fields, so the preview-before-send gate shows exactly the text that will leave
  the machine even though the payload is now JSON.
- **Graceful overflow fallback** — if a report exceeds a channel's structured limits (Discord
  embed: ≤25 fields, ≤1024 chars/field, ≤6000 total; Slack: ≤50 blocks, ≤3000 chars/section), it
  falls back to the plain `{content}`/`{text}` message (truncated) rather than send an invalid
  payload. A complete report beats a rejected one.
- **Tests** — 154 total (+6 net): `tests/test_report_compose.py` now pins the embed/Block Kit
  structure, the per-channel bold dialect, inline-bold translation in section bodies, the intake
  single-section case, and **both** overflow→plain fallbacks; `tests/test_cli.py` asserts a real
  run carries twice-redacted sections on the blob.

### Changed

- **`ReportBlob` carries structured sections** (`report.py`) — a new defaulted
  `sections: tuple[(title, body), …]` field (additive; `body` stays the canonical redacted text
  and plain-text fallback). `intake` (one body, no sections) renders as a single "Update" section.
- **Redaction pass 2 runs per section** (`cli.py`) — the second redaction pass now scrubs each
  section body before the blob is built, so every block/embed text field is twice-redacted; the
  flat `body` is the merge of those already-twice-redacted sections (byte-identical to the old
  "merge then redact" for normal content). A structured payload can never become a redaction
  bypass.
- **`compose()` returns a `ComposedMessage(payload: dict, preview: str)`** instead of a string,
  and **owns truncation/size limits**. **Delivery is now pure transport**: `send(payload, url)`
  serializes and POSTs the payload as-is (`delivery/discord.py`, `delivery/slack.py`), with the
  `DeliveryError` contract and Discord User-Agent unchanged.

### Notes

- **Always-on, no config toggle.** Rich rendering is the default with a built-in plain fallback;
  a per-project `rich = false` toggle was deliberately deferred (the compose seam is kept clean so
  it stays an additive change). Splitting an oversized report across multiple embeds/messages
  (instead of falling back to plain) is a possible future enhancement.

## Phase B6 — CLI config-inspect commands (2026-06-16)

Read-only visibility into the config, closing the gap surfaced while using `install-hook` (there
was no way to *see* what's configured or confirm a flag like `auto_send`). Net new runtime
dependencies: 0; `config.py`/`secrets.py` reused unchanged.

### Added

- **`orion projects`** (`cli.py`) — list every configured project with its key facts:
  `auto_send`, `share_level`, collectors, and recipients (name + channel).
- **`orion show <project>`** — one project's fully-resolved config: paths, flags, collectors,
  and each recipient's channel + webhook env-var **name** (never the URL).
- **`orion check`** — validate the config (reusing `load_config`), then report per-project
  send-**readiness**: the git repo path exists, each recipient's webhook secret is present, and
  `ANTHROPIC_API_KEY` is present when the git (raw) lane is in use. Secrets are reported by NAME
  as `set`/`MISSING`, **never by value**; exits non-zero if the config is invalid or any required
  item is missing, so it works as a pre-flight gate. (Soft items like a not-yet-created collector
  file warn but don't fail.)
- **Tests** — 148 total (+7): `tests/test_inspect.py` covers `projects`/`show` (right facts, no
  URL leak, unknown-project error) and `check` (ready → exit 0; missing webhook → flagged by name
  + exit 1, with a seeded secret value confirmed absent from output; invalid config → exit 1).

### Notes

- **Read-only by design.** These commands never write `orion.toml`. A `config set`-style writer
  was deliberately **excluded**: it would break the settled "Orion never writes its config"
  decision (the reason for TOML + read-only `tomllib`) and need a comment-preserving TOML-writer
  dependency. Config stays a hand-edited, declarative file. (Resolves KI-15.)

## Phase B2 — Claude Code session skill (2026-06-16)

Adds the fourth (and final) ingestion signal — **coding sessions** — as a *pushed summary*, not by
Orion parsing session files. A bundled Claude Code skill summarizes the session and sends it via
the existing `intake`. Orion does **not** re-summarize: the skill's summary is what's delivered
(after redaction), on the structured lane. Net new runtime dependencies: 0.

### Added

- **`skills/orion-session/` skill** (`SKILL.md` + `skills/README.md`) — a separable Claude Code
  skill (outside the Python package). It drafts an outcome-focused, secret-free session summary,
  **shows it for approval in the session**, then sends it with `intake --yes`. Install by copying
  into `~/.claude/skills/`. README gains a "Claude Code session skill" section.
- **Tests** — 141 total (+2): `tests/test_intake.py` pins `intake --yes` (delivers with no
  `input()` prompt via a tripwire, and a seeded fake key is still redacted) and that plain
  `intake` still previews (the load-bearing "`--yes` is the only bypass").

### Changed

- **`intake` gains `--yes`** (`cli.py`) — skips the terminal preview and sends non-interactively,
  for the session skill (which runs `intake` through a non-interactive shell, where the preview
  would EOF-abort). The human gate moves into the session (the skill shows the summary first).
  Both redaction passes still run. Unlike `report --yes`, there is **no `auto_send`-style gate**:
  `report` can run unattended (cron), but `intake` is always an explicit push, so a deliberate
  `--yes` is sufficient. Without `--yes`, `intake` previews exactly as before.

## Phase B1 — Event-driven triggers / git hooks (2026-06-16)

Opens **Horizon B** (local automation). Orion can now report **automatically on a git event**: a
git hook fires `report <project> --yes` when you commit or push. Orion still does not watch the
repo and ships no daemon — the hook is what runs it. The report pipeline is **unchanged**: the
hook only calls the existing `report --yes`, so every Horizon-A guarantee (two-pass redaction,
the `--yes` + `auto_send` gate, exit-0-on-skip) carries over. Net new runtime dependencies: 0.

### Added

- **`orion install-hook <project>` command** (`cli.py`) — installs a portable git hook that
  auto-reports the project. `--hook {pre-push,post-commit}` (default **pre-push**, which batches
  the commits you push and is less noisy than per-commit); `--print` shows the script without
  writing; `--force` replaces an existing hook (it refuses to clobber one otherwise — this is the
  only place Orion writes into your repo). Warns when the target project isn't `auto_send`-opted
  (the hook would run but skip sending).
- **`src/orion/hooks.py`** — `build_hook_script` (pure builder for the `#!/bin/sh` hook:
  backgrounded `report --yes`, always `exit 0`, forward-slash paths so it's valid under the `sh`
  git uses on Windows, output to `<git-dir>/orion-hook.log`) and `resolve_hooks_dir`
  (`git rev-parse --git-path hooks`, so it's correct for worktrees / `core.hooksPath`, not a
  hardcoded `.git/hooks`).
- **`docs/git-hooks.md`** — the runbook (pre-push vs post-commit, the background/exit-0/never-
  block-git design, the `auto_send` requirement, the log file, per-OS notes, review/replace/
  remove, and hook-manager coexistence). README gains an "Event-driven reports" section.
- **Tests** — 139 total (+13): `tests/test_hooks.py` pins the generated script's safety
  properties (delegates to `report --yes`, backgrounded, always `exit 0`, forward-slash-only
  paths, self-describing), `resolve_hooks_dir` against a real repo (and `GitError` on a non-repo),
  and the command end to end (writes an executable hook, honors `--hook`, refuses to clobber
  without `--force`, `--print` writes nothing, unknown-project error, non-`auto_send` warning);
  plus two `test_secrets.py` cases for the config-relative `.env` discovery below.

### Changed

- **`load_secrets` now also reads the `.env` beside the `--config` file** (`secrets.py`), in
  addition to the working-directory search. A git hook or scheduled job starts in some *other*
  directory, so the default CWD-based `.env` lookup couldn't find Orion's central secrets; now,
  passing `--config` (which hooks and the scheduling runbook already do) is enough to locate the
  `.env` next to `orion.toml`. Precedence is unchanged (`override=False`): an exported environment
  variable still wins, and the config-relative `.env` wins over a CWD one. This fixes secret
  discovery for **both** event-driven and scheduled unattended runs.

## Phase 4 — Scheduled digests / unattended send (2026-06-15)

Orion can now run **unattended** on a cadence, so an OS scheduler can deliver digests without a
human at the keyboard — **without weakening preview-before-send**. No scheduler is built: Orion
delegates timing to the OS's native tool (the Phase 3.5 stance), and this phase adds only the
non-interactive, opt-in send path that makes that safe. Each run already reports only the delta
since the last report, so a daily job is naturally a daily digest. Redaction is unchanged on
every path. This resolves the Phase-4 tension recorded as KI-12.

### Added

- **`auto_send` project field** (`config.py`) — a per-project boolean (default `false`) that
  opts a project in to preview-less delivery. Type-validated like `share_level` (a non-boolean
  is a clear `ConfigError`, so a privacy switch can't be truthy-by-accident).
- **`report --yes`** (`cli.py`) — the non-interactive flag for scheduled runs. The human
  preview is skipped **only** when `--yes` **and** `auto_send=true` are *both* present (defense
  in depth): `--yes` on a project without `auto_send` is **skipped and logged, never sent**, and
  without `--yes` every run previews as before (config alone never bypasses the gate).
- **`report --all`** (`cli.py`) — report on every configured project in one run, **fail-soft**
  (one project's error doesn't stop the rest), with a one-line outcome tally. Exits non-zero
  **only on a real failure**, so a scheduler alerts on genuine problems, not on routine
  "nothing to send" runs. Exactly one of `{project, --all}` is required.
- **`docs/scheduling.md`** — a per-OS runbook (cron / systemd timer / launchd / Task Scheduler)
  for wiring `python -m orion report --all --yes`, including the **WSL2 caveat** (cron runs only
  while WSL is up → drive it from Windows Task Scheduler, or run native) and the
  minimal-environment gotchas (absolute paths, the venv's Python, `git` on PATH). The README
  gains a "Scheduling" section pointing to it; `orion.toml.example` documents `auto_send`.
- **Tests** — 126 total (+11): a new `tests/test_schedule.py` for the unattended-send safety
  contract (auto-send skips the preview yet still redacts a seeded key; `--yes` without
  `auto_send` sends nothing; `auto_send` **without** `--yes` still previews — the load-bearing
  test; `--all` is fail-soft and only opted-in projects deliver; the `project`/`--all` usage
  errors), plus the `auto_send` config-parsing/validation tests. The preview-gate tests use an
  `input()` *tripwire* that fails if the prompt is ever reached on an unattended path.

### Changed

- **`cmd_report` split into a thin setup wrapper + `_run_report(project, conn, assume_yes)`**
  (`cli.py`) — setup (config/secrets/state) and its global errors stay in `cmd_report`, while
  the per-project pipeline moves into `_run_report`, which owns its own fail-soft error handling
  and returns a `STATUS_*` outcome. This is what lets single-project and `--all` share one loop
  (DRY) and report a meaningful exit code. No collector, redaction, summarizer, compose, or
  delivery logic changed.
- **Shared CLI test setup extracted to `tests/conftest.py`** — the real-repo builder, the
  config writer (now with an optional `auto_send`), the mock fixture, and the scripted-`input`
  helper now live in one place, reused by `test_cli.py` and `test_schedule.py` (DRY).

## Phase 3.5 — Cross-platform portability pass (2026-06-15)

An audit plus targeted fixes so Orion runs natively on Windows, macOS, and Linux, and a
recorded cross-platform scheduling stance that de-risks Phase 4. The audit confirmed the core
was already highly portable (`pathlib` throughout, explicit `encoding="utf-8"` on every text
read, `splitlines()`, no `shell=True`, a component-built timestamp), so this is fixes — not a
rewrite. **No** collector, redaction, summarizer, compose, or delivery logic changed, and no
new data path reaches the LLM or a channel.

### Added

- **`python -m orion` entry point** (`src/orion/__main__.py`) — the portable invocation that
  works identically on every OS, regardless of where the venv puts the launcher
  (`.venv/bin/` vs `.venv\Scripts\`). It delegates straight to `cli.main`, sharing one code
  path with the installed `orion` console script. The docs now lead with this form.
- **Console UTF-8 guard** (`cli._ensure_utf8_output`, called at the top of `main`) —
  reconfigures stdout/stderr to UTF-8 so the status glyphs (`⚠`, `✗`) cannot raise
  `UnicodeEncodeError` when output is redirected to a pipe/file on Windows (where Python
  otherwise falls back to the cp1252 code page). A no-op where the stream is already UTF-8
  (macOS/Linux, modern Windows terminals) and silently skipped on non-reconfigurable streams
  (pytest capture, embedded interpreters), so it never breaks the CLI. The glyphs are kept.
- **"Supported platforms" docs** — the README states native Linux/macOS/Windows 10/11
  support, the Python 3.11+ / `git`-on-PATH requirements, and an honest "tested on" line;
  setup now shows per-OS venv activation and OS-agnostic `python -m pip`/`python -m pytest`.
- **Windows TOML-path guidance** (README + `orion.toml.example`) — write `repo_path` with
  forward slashes or as a single-quoted literal, because a backslash is a TOML escape.
- **Package classifiers** (`pyproject.toml`) — `Operating System :: OS Independent` and the
  supported Python versions (3.11–3.13), backing the README's support matrix in metadata.
- **Testing docs** — `docs/testing.md` (a living catalog: test categories, *why* each
  matters, how to run, and intentional non-targets) and `docs/portability-smoke-test.md` (the
  per-OS manual runbook for native Windows/macOS), linked from the README and the plan.
- **Tests** — 115 total (+10): the `python -m orion --help` entry point (a subprocess test
  exercising real `-m` module resolution) and the console UTF-8 guard (requests UTF-8 when
  supported; no-op without `reconfigure`; swallows `ValueError`/`OSError`; covers both
  streams); plus a suite audit that confirmed every test file is current and closed five
  additive gaps (Slack-token redaction; git noise-glob, diff-cap, and subdir-sensitive
  exclusion).

### Changed

- **Docs lead with `python -m orion`** instead of `.venv/bin/orion`, and the development
  commands use `python -m pip`/`python -m pytest` — identical across all three OSes.

## Phase 3 — Slack delivery + recipient routing (2026-06-15)

A project can now report to Discord *and* Slack in one run, with each recipient routed to
their own channel and webhook.

### Added

- **Slack delivery** (`delivery/slack.py`) — a single `{"text": …}` mrkdwn POST via stdlib
  `urllib`, a sibling of the Discord sender with the same `send(message, webhook_url)` shape
  and shared `DeliveryError`. (Block Kit deferred — see `docs/known-issues.md` KI-9.)
- **Slack message rendering** (`compose.py`) — a `slack` branch plus `_to_slack_mrkdwn`,
  which translates the Markdown Orion emits to Slack's dialect (`## h` → `*h*` bold lines,
  `**b**` → `*b*`). Discord rendering is unchanged. (Structural-only — KI-10.)
- **Per-channel routing** (`cli.py`) — each run composes once per distinct recipient channel
  and delivers each recipient their channel's rendering via `_sender_for(channel)`. The
  preview shows one labeled block per channel with a single combined confirm; a single-channel
  run is unchanged. Applies to both `report` and `intake`.
- **Config/secrets** — `SUPPORTED_CHANNELS` now includes `"slack"`; a Slack recipient names a
  `webhook_env_var` pointing at a Slack incoming-webhook URL in `.env` (no new mechanism).
- **Tests** — 105 total (+15): the Slack sender, Slack compose rendering, and multi-channel
  routing (each recipient gets the right format via the right webhook; combined preview;
  decline aborts all; one channel failing doesn't block the other; intake fans out too).

### Changed

- **`_deliver` and `_preview_and_confirm` now take a per-channel `messages` dict** instead of
  a single string, so one run serves multiple channels. Channel→sender dispatch is a small
  `_sender_for` function (call-time name resolution, mirroring `_collect_for`), keeping the
  senders monkeypatchable and avoiding an import-time capture bug.

## Phase 2 — Structured lane (2026-06-15)

Report-ready signals that skip the LLM entirely (only raw git activity is ever summarized by
Claude). A run now collects from every enabled signal, merges them into one sectioned message,
and previews/sends it once.

### Added

- **Tasks collector** (`collectors/tasks.py`) — reports items newly checked off in a
  Markdown checklist (`- [x]`) at a per-project `tasks_file`. Structured lane (no LLM).
  Its marker is the full current completed set, so an already-reported item never repeats.
  (Items are identified by text — see `docs/known-issues.md` KI-6.)
- **Notes collector** (`collectors/notes.py`) — sends a hand-written `notes_file` when its
  content changes (a content-hash "replace model"; see KI-7). Structured lane (no LLM).
- **`orion intake <project>`** — sends a pushed/hand-written update (`--message` or stdin),
  the same entry point the Phase-6 Claude session skill will use. No collector, no LLM, no
  delta marker (a push, not a delta); still runs two-pass redaction + preview-before-send.
- **Merge step** (`merge.py`) — combines per-collector bodies into one report with titled
  `## ` sections, skipping empty ones. One preview, one delivered message per run.
- **Per-collector state markers** (`state.py`) — a generic `collector_markers`
  (project, collector) table replaces the single `last_commit`, so each signal tracks its
  own delta independently. Pre-Phase-2 git markers are migrated automatically on open.
- **Config** — `collectors` now accepts `"tasks"`/`"notes"`; each requires its file path
  (`tasks_file`/`notes_file`) only when enabled, resolved relative to the config file.
- **Tests** — 90 total (+38): the two collectors, the merge helper, per-collector markers
  and backfill, the multi-collector orchestrator (incl. a test proving the structured lane
  never calls the summarizer), and the `intake` command.

### Changed

- **`cmd_report` is now multi-collector** — it loops over the enabled signals, redacts each
  (pass 1), summarizes only the raw lane, merges, redacts the merged body (pass 2), then
  previews/sends once and advances each collector's marker only after a successful send.
  The Anthropic client is built lazily, so a structured-only run needs no API key.
- **Delivery extracted** into a shared `_deliver` helper used by both `report` and `intake`.
- **`ReportBlob.source_marker` is vestigial** (always `""`) now that markers are
  per-collector; the legacy `project_state.last_commit` column is kept only as the
  migration source (see KI-8).

## Phase 1 — MVP: git → summary → Discord (2026-06-15)

First end-to-end slice: `orion report <project>` reads git activity since the last report,
redacts secrets, conditionally summarizes with Claude, previews in the terminal, and on
confirmation posts to a Discord webhook — then advances per-project state so the next run only
covers what is new.

### Added

- **Report pipeline** wired end to end in `src/orion/`:
  `config → secrets → state → git collect → redact → summarize (Haiku) → redact →
  compose → preview/confirm → Discord (urllib) → advance state`.
- **TOML config** (`config.py`) — a per-project registry (repo path, share level,
  collectors, named recipients). Recipients name an env var holding their webhook URL,
  so the config itself never contains a secret.
- **sqlite3 state store** (`state.py`) — last-reported commit per project + a redacted
  report history. State advances *only* after a successful send.
- **Git collector** (`collectors/git.py`) — a hybrid payload (commit messages +
  diffstat + a capped, secret-filtered diff). Sensitive paths (`.env`, `*.pem`, keys,
  `credentials*`, …) are excluded from the diff at collection time.
- **Two-pass redaction** (`redact.py`) — scrubs API keys, tokens, JWTs, PEM blocks, and
  secret-ish assignments; runs once before the LLM and once before sending, and reports
  a hit count for the preview.
- **Conditional summarizer** (`summarize.py`) — one Claude Haiku 4.5 call on the raw
  lane only; the lane seam is built so Phase 2's structured lane is purely additive.
- **Preview-before-send gate** (`cli.py`) — default-no confirmation showing the
  redaction count; any pipeline error aborts before send (fail-closed).
- **Discord delivery** (`delivery/discord.py`) — a single JSON POST via stdlib
  `urllib.request`, with length truncation and uniform error handling.
- **Human-friendly date line** in composed messages — e.g. `June 15, 2026 · 1:32 AM
  UTC` — while the stored timestamp stays canonical ISO 8601.
- **Test suite** — 52 tests covering config, state, secrets, redaction (a secret-format
  corpus), the git collector, the summarizer (dependency-injected client), compose, and
  an end-to-end CLI run against a real temporary repo.
- **`docs/test-messages.md`** — a log of real messages delivered during testing,
  organized by channel.

### Fixed

- **Redaction hit count was double-counting** a single secret. A value caught by a
  specific pattern (e.g. `sk-…`) that also sat in a secret-ish-named assignment
  (`token = "sk-…"`) was re-matched by the generic `NAME=value` catch-all, which ate the
  just-inserted `[REDACTED_API_KEY]` token — inflating the count (2 for 1) and mangling
  the placeholder. *Why it mattered:* the "⚠ N secrets redacted" notice is part of the
  human-gate trust signal, so an inflated count is misleading. *Fix:* a negative
  lookahead (`(?!\[REDACTED_)`) so the catch-all skips placeholders an earlier pattern
  already inserted. No leak either way — this corrected the count and cosmetics only.
- **Discord delivery failed with HTTP 403** against real webhooks. The request sent no
  `User-Agent`, and Discord's edge (Cloudflare) blocks the default `Python-urllib/x.y`
  agent. *Why it mattered:* delivery is Phase 1's only outbound step, so this broke the
  one feature that has to work live. *Fix:* send a descriptive `User-Agent` header
  (Discord's API requires one); pinned by a delivery test asserting a non-default agent.

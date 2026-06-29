<!-- =========================================================================
docs/stage2-comments-discussion-consolidation-kickoff.md
---------------------------------------------------------------------------
Responsible for: The grounded kickoff for STAGE 2 of folding C2 comments into the
                 E2 Inc 5 supervisor-interaction discussion model — the full
                 data-model unification (Stage 1, the project-page de-dupe, already
                 shipped in PR #74).
Role in project: Read this at the START of the Stage 2 session, THEN do the per-unit
                 plan-mode pass and build. This captures the direction, the settled
                 decisions, the open decisions (with a recommendation each), and the
                 verified reuse pointers — NOT a finished design. Model:
                 docs/supervisor-interaction-loop-kickoff.md.
                 Builds on: E2 Inc 5 (the discussion loop, PR #74, merged 2026-06-29);
                 Stage 1 (project page = single Discussion surface, in #74).
========================================================================= -->

# Kickoff: Comments → Discussion consolidation, Stage 2 (data-model unification)

## Context — why this, why now

E2 Inc 5 shipped the **supervisor-interaction loop**: an attributable, per-project,
two-way discussion (supervisor ↔ developer) with first-class identity, across the relay,
the CLI (`orion discussions pull/reply`), and an SPA panel. Seeing it rendered made a
redundancy obvious: the dashboard now had **two near-identical conversation surfaces**,
Discussion and Comments, doing one job. The discussion loop is effectively the matured,
identity-first, two-way **successor** to C2 comments.

The differences are mostly under the hood:

| | Comments (C2) | Discussion (E2 Inc 5) |
| --- | --- | --- |
| Anchor | a specific **report** (`report_comments.report_id`) | the **project** (a persistent thread) |
| Identity | free-text `author`, `role: null` (pre-C3, can be anonymous) | server-derived `author_id` + `author_name` + real `role` |
| Direction | supervisor writes, developer reads (`orion comments` is pull-only) | both write (developer replies via CLI) |
| Auth | optional (open relay → anonymous) | required |

**Stage 1 (SHIPPED in #74):** removed the duplicate Comments section from the project
page. The project carries only the project-level Discussion thread. Per-report Comments
stay on the report-detail page. The *visible* redundancy is gone, but **two systems still
exist underneath**: two stores, two endpoint families, two CLI surfaces, two component
sets.

**Stage 2 (this kickoff):** the real consolidation. Fold comments into the discussion
model so there is **one** conversation system, then retire `report_comments` and the
comment endpoints/UI **at parity** (the `render.py` / KI-23 precedent). Deferred to here
on purpose: it was sequenced **after some dogfooding**, because real usage answers the one
design fork that otherwise has to be guessed (see Open decision 1).

## Settled decisions (do not re-litigate)

1. **Direction:** Discussion is the single conversation model. Comments fold in. A
   "comment on report N" becomes a discussion message tagged to that report (or comments
   are retired outright — see Open decision 1).
2. **Retire at parity.** No comment is lost. Existing `report_comments` rows are migrated,
   not dropped, and comments keep working until the discussion model reaches parity.
   Precedent: KI-23 (the server-rendered dashboard retired only once the SPA was at parity).
3. **Stage 1 is done** (project page = single Discussion surface). This kickoff is only the
   data-model + endpoint + CLI + bot + SPA + migration work beneath it.

## What NOT to lose (permanent invariants)

- **Observe-not-originate.** Orion authors nothing. The `orion` item role stays reserved
  (a later grounded-responder rung), never written by a human or a migration.
- **Server-derived, unforgeable attribution.** The cookie path derives role from the
  principal; the Bearer path hardcodes its role. A client cannot claim a role it does not
  hold. Migration is the one place identity is *assigned* rather than derived — do it
  honestly (see Open decision 2).
- **Append-only.** No edit/delete of anyone's words. The migration preserves every body,
  author, and timestamp.
- **Existence-hiding** (out-of-scope project → 404, identical to missing) and **XSS-inert
  rendering** (React text node, never `dangerouslySetInnerHTML`).

## Open decisions (settle in the build session's plan pass — recommendation each)

1. **Report-context: a tag, or drop it?** Does a discussion item gain an optional, nullable
   `report_id`, so a message tagged to a report renders on the report page (untagged =
   project-level)? **Recommendation: let dogfooding decide.** If report-scoped comments get
   real use, add the nullable `report_id` tag (it is the one genuine capability comments
   had). If they go unused, **skip the tag and retire comments outright** — strictly
   simpler. This is precisely why Stage 2 was deferred past first use.
2. **Legacy comment migration / role mapping.** `report_comments` rows have a free-text
   `author`, no `role`, no `author_id`. Map them to discussion items as: `author_name` =
   the old author (or "anonymous" when empty), `author_id` = NULL, `report_id` = the old
   `report_id` (if the tag is kept), body/`created_at` preserved. **Recommendation: role =
   "supervisor"** (their historical intent — comments were supervisor feedback), recorded
   honestly as a best-effort historical mapping (legacy comments never carried identity).
   Sub-decision: **migrate-and-drop** `report_comments` (one store, truly DRY) vs. keep it
   frozen read-only. **Recommendation: migrate-and-drop**, unless a dry-run shows migration
   risk on the live data.
3. **The bot path `POST /api/comments`.** Chat (Discord/Slack) is parked, so there is no
   live bot. **Recommendation:
   deprecate/remove the comment write endpoints now; when chat resumes, the bot posts a
   discussion item** (role = supervisor — chat replies are supervisor feedback). Do not let
   a parked integration block the retirement.
4. **CLI `orion comments`** (pull-only) is now redundant with `orion discussions pull`.
   **Recommendation: remove it** (or alias with a deprecation note) and drop the
   `comment_watermark` table once nothing reads it. `orion discussions pull/reply` is the
   single CLI conversation surface.
5. **Serializer/SPA swap.** `serialize_report` should carry the report's tagged discussion
   messages instead of `comments`; `serialize_project` drops `comments`. The SPA report page
   swaps `CommentList`/`CommentComposer` for the discussion components (a report-scoped
   variant), and the comment client/types retire.

## The ladder (smallest reviewable units — exact split is the build session's plan)

Order matters: this is a **retire-at-parity**, so the migration must reach parity before
the comment endpoints/UI are removed. Keep comments working until then.

- **Unit A — data model (if report-context is kept):** nullable `report_id` on
  `relay_discussion_items` (+ index); a `discussion_items_for_report` read (and/or a
  `report_id` filter on the existing read). `IF NOT EXISTS` / `ALTER ADD COLUMN` so no
  destructive migration of the discussion store itself.
- **Unit B — report-scoped write + read:** a report-context discussion write (cookie, on the
  report page) and folding report-tagged items into `serialize_report`. The project read
  already carries `discussions[]`.
- **Unit C — migration (the parity step):** a one-time, **idempotent** migrate of
  `report_comments` → `relay_discussion_items` (per Open decision 2), runnable against the
  live relay DB after a backup + a dry-run on a copy. Then drop (or freeze) `report_comments`.
- **Unit D — CLI + bot retirement:** remove/alias `orion comments`; deprecate or repoint the
  bot `POST /api/comments`; drop `comment_watermark` once unused.
- **Unit E — SPA:** the report page uses the discussion components (report-scoped); remove
  `CommentList`/`CommentComposer` and the comment client/types.
- **Unit F — cleanup + docs:** remove the comment store fns, endpoints, and contract
  entries; resolve the KI; sync the roadmap + parallelization + contract.

**Out of scope:** the grounded-responder rung (Orion answering from observed data); chat-bot
revival; Unit 5 of the loop ("open directions" derivation) — independent and still deferred.

## Parallelization & coupling (carried thought-process)

- **Vertical slice, build coordinated**, like the loop itself: store → relay → CLI/SPA. The
  data-model unit (A) gates the report-scoped read/write (B); the migration (C) gates the
  retirements (D/E/F).
- **The genuine risk is the live-data migration (Unit C)** — it touches the deployed relay
  DB's real comment rows. Make it idempotent, back the DB up first, and dry-run on a copy.
  Everything else is additive or deletion-at-parity.
- D (CLI/bot) and E (SPA) are loosely independent once B's wire shape is fixed; F is the
  final sweep. Update [`docs/parallelization.md`](parallelization.md) as units land.

## Pointers (reuse / retire these — verified at kickoff time, 2026-06-29)

The comment system being folded in (mirror its discussion counterpart, then retire):

- **Store** (`relay/store.py`): `report_comments` (`add_comment`, `comments_for`,
  `comments_for_project`) ⟶ `relay_discussion_items` (`add_discussion_item`,
  `discussion_items_for_project`). The `comment_watermark` table is in `src/orion/state.py`;
  the discussion analogue is `discussion_watermark`.
- **Endpoints** (`relay/server.py`): `_handle_api_report_comment` (cookie),
  `_handle_api_comment_post` (Bearer/bot), `_handle_api_comments` (Bearer pull) ⟶ the
  discussion analogues `_handle_api_discussion_item`, `_handle_api_discussion_post`,
  `_handle_api_discussions`.
- **Serializers** (`relay/api.py`): `_comment` + `comments` on `serialize_project` /
  `serialize_report` ⟶ `_discussion_item` + `discussions`.
- **CLI** (`src/orion/cli.py`): `cmd_comments` ⟶ `cmd_discussions_pull` / `cmd_discussions_reply`.
- **Bot** (`orion.bot`): `post_comment` → `POST /api/comments`.
- **SPA** (`web/`): `components/CommentList.tsx` + `CommentComposer.tsx` (used on
  `routes/Report.tsx`) ⟶ `DiscussionList.tsx` + `DiscussionComposer.tsx`. Client:
  `api/client.ts::postComment` ⟶ `postDiscussion`. Types: `api/types.ts::Comment` ⟶
  `DiscussionItem`.
- **Contract:** [`docs/dashboard-api-contract.md`](dashboard-api-contract.md) documents both
  families today (the comment routes + the E2 Inc 5 discussion routes).
- **Precedent for retire-at-parity:** KI-23 (the `render.py` / server-rendered dashboard
  retirement). The Stage 1 + loop work lives in `docs/supervisor-interaction-loop-kickoff.md`
  and PR #74.

## Verification (build session)

- **Parity:** every existing comment appears as a discussion item after the migration, with
  the same body, author label, report linkage, and timestamp. A count + spot-check on a copy
  of the live DB before the real run.
- **Idempotency:** re-running the migration is a no-op (no duplicate rows).
- **Endpoints:** no comment route remains (a removed route is a clean 404); the report page's
  read carries report-tagged discussion messages.
- **SPA:** the report page renders the thread inert and correct **across all 3 themes** (the
  relay-serve-the-built-SPA local recipe used to verify the loop's Unit 4).
- **Suites green** (backend + web), and a **migration dry-run against a backup of production**
  before touching the live relay DB.

**Gotchas:** the live relay DB holds real comment data — back it up and dry-run first, and the
migration must be idempotent. Run backend tests with `PYTHONPATH=src` (the worktree
editable-install gotcha: agents import the main checkout's `src`).

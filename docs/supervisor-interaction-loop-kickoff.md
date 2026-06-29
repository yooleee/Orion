<!-- =========================================================================
docs/supervisor-interaction-loop-kickoff.md
---------------------------------------------------------------------------
Responsible for: The grounded kickoff for the NEXT major build after E2 Inc 4 —
                 a two-way SUPERVISOR-INTERACTION LOOP (an ongoing per-project
                 discussion between a supervisor and the developer, with Orion as
                 connective tissue + memory). This is the read-write / identity
                 watershed the project kept seams clean for.
Role in project: Read this at the START of the interaction-loop session, THEN do the
                 per-phase plan-mode pass and build. This captures the direction +
                 settled decisions + open items, NOT a finished design (the unit-by-unit
                 design is the build session's work). Model: docs/skills-comb-rework-kickoff.md.
                 Builds on: E2 Inc 4 COMPLETE; C3 identity (Inc 1 built); the existing
                 comment + observe-not-originate machinery.
========================================================================= -->

# Kickoff: Supervisor-interaction loop (Orion's two-way discussion surface)

## Context — why this, why now

E2 Inc 4 is complete: the dashboard's READ surfaces are rich (portfolio, projects, reports,
live checklist, scheduling, disciplines, skills comb). The founding intent — "one place to
see all projects, viewable by family who comment and give direction" — is realized on the
**viewing** side, but the **interaction** side is shallow: a supervisor can only leave a flat
comment on a single report, and the developer pulls replies via `orion comments`.

The agreed next build (decided 2026-06-28, sequenced **before** a dogfooding pause so Orion
reaches a "usable for a stretch without major feature builds" state first): a **proper
two-way discussion loop** between a supervisor (a family member) and the developer, with
Orion as connective tissue + memory. This is the **read-write / identity watershed** the
project has kept seams clean for (E5-adjacent, extends C3). Intended outcome: a supervisor
and developer hold an ongoing, attributable, per-project conversation that persists and
resurfaces, in depth beyond a report comment.

## Settled decisions (this session)

1. **Orion's role = medium + memory FIRST.** Orion carries the supervisor↔developer
   discussion, threads it per project, REMEMBERS directions over time (the append-only log
   is the memory), and can surface/summarize the observed state behind a thread. It
   **generates no replies of its own** in this phase. A "grounded responder" (Orion answers
   from observed data, labelled, never originating) is an explicit LATER rung, out of scope
   here. This keeps observe-not-originate airtight while we build the foundation.
2. **Anchor = one persistent per-project thread**, not per-report. The report is context
   *inside* the thread, not the anchor. A cross-project supervisor inbox is a later view
   over these threads.
3. **First-class identity NOW.** Each discussion entry stores `author_id` + a `role`
   (`supervisor | developer | orion`), and a **`supervisor` role** is added to
   `relay_users` (beyond today's `admin | viewer`). This is the clean modeling the project
   flagged as coming (supervisor-direction-drives-data-modeling) — built at the seam so
   per-supervisor routing stays additive, not a rework.

## What NOT to lose (permanent invariants)

- **Observe-not-originate.** Orion never authors forward facts or opinions, and never puts
  words in a human's mouth. In this phase Orion authors NOTHING. "Remembering" /
  "resurfacing directions" must be a **pure derivation** over the append-only thread
  (rebuildable, clearly labelled as Orion's view), never a new authored entry. The `orion`
  role on the item schema is reserved for the future grounded-responder rung.
- **Security / privacy.** Scope-filter FIRST + existence-hiding (out-of-scope project →
  404, identical to missing); CSRF on cookie writes; redaction on any producer lane; no
  secrets. Append-only — no edit/delete of anyone's words.
- **Identity attribution is server-derived**, never client-supplied (mirror
  `_handle_api_report_comment`: role/author come from the authenticated principal).

## The ladder (smallest reviewable units — exact split is the build session's plan pass)

- **Unit 1 — data model + role (backend).** New `relay_discussion_items` table
  (`id, project, author_id, author_name, role, body, created_at`; append-only,
  time-ordered, `IF NOT EXISTS` so no migration). Store fns mirroring `report_comments`
  (`add` / per-project read / `since_id` watermark). Add `supervisor` to the provisionable
  roles. *Flat per project in v1* — no `reply_to` nesting (additive seam later).
- **Unit 2 — write + read endpoints + contract.** `POST /api/discussions/:project/items`
  (cookie + CSRF, human write, attributed to principal) and the per-project read (new
  `GET /api/discussions/:project` or folded into `GET /api/projects/:name`). Fill the
  role-badge "gap 7" here. Document in `docs/dashboard-api-contract.md` (house style).
- **Unit 3 — producer loop (CLI), closing the developer's half.** Extend the pull/watermark
  pattern so the developer reads new supervisor messages from the terminal, and add a
  developer REPLY path (Bearer machine post, `role=developer`), so the loop works
  end-to-end without the dashboard. Mirror `pull_comments` + `cmd_comments` + the
  `(project, relay_url)` watermark.
- **Unit 4 — SPA discussion panel.** A per-project thread on the project page: read + post,
  with role badges (supervisor/developer). Mirror `CommentList.tsx` / `CommentComposer.tsx`
  (inert text rendering — the XSS guarantee). Eyes-on across all 3 themes.
- **Unit 5 (optional v1 polish) — "open directions" derivation.** A pure derived view
  (supervisor messages awaiting a developer reply) surfaced on the portfolio/project. No
  authored content. Can defer.

**Out of scope (explicit later rungs):** Orion grounded-responder; per-supervisor routing;
cross-project inbox; threaded replies; supervisor self-provisioning.

## Parallelization & coupling (carried thought-process)

- **Vertical slice, build coordinated** (like the Inc 2 checklist signal): store → relay →
  CLI → SPA, against the new `/api/discussions` wire shape. Not a relay-only change.
- **Unit 1 gates all.** Then Unit 2 (endpoints) and Unit 3 (CLI) share the wire shape and
  can proceed loosely in parallel once Unit 1 lands; Unit 4 (SPA) depends only on Unit 2's
  fixed wire shape (independent thereafter); Unit 5 derives over Unit 1.
- **Genuine coupling = the identity/role work touches existing C3 auth** (`relay_users`,
  `_authenticate`, `_allowed_projects`). The discussion table itself is additive (new
  table, no migration). Keep the role addition minimal (allowlist + attribution), not an
  auth rewrite.
- Update `docs/parallelization.md` when the analysis shifts as units land.

## Pointers (reuse these — verified at kickoff time)

- **Mirror for the write endpoint:** `relay/server.py::_handle_api_report_comment`
  (auth → CSRF → validate → scope-filter (404 existence-hiding) → attribute to
  `principal["name"]`/role → 201 in read shape). Bot/machine post pattern:
  `_handle_api_comment_post`.
- **Store patterns to mirror:** `report_comments` (`add_comment`, `comments_for`,
  `comments_for_project` + `since_id`) and the append-only/rebuildable
  `relay_observed_items` (`record_observations` / `observed_history`) for the memory model.
- **Identity/auth:** `relay_users` (role `admin|viewer`, `_PROVISIONABLE_ROLES`),
  `_authenticate` (principal = `{user_id, name, role}`, re-read from DB each request),
  `_allowed_projects` (scope → `allowed` set), `relay_user_projects` (default-deny scope),
  `revoke_user` / `bump_session_version`. Provisioning: `POST /api/users` (admin token) +
  `orion relay-user add/list/revoke`.
- **Producer loop:** `src/orion/delivery/relay.py::pull_comments` + `cli.py::cmd_comments`
  (watermark via `get/set_comment_watermark`, JSON + human output).
- **SPA:** `web/src/components/CommentList.tsx` + `CommentComposer.tsx`,
  `web/src/api/types.ts::Comment`, `web/src/api/client.ts::postComment`.
- **Contract house style:** `docs/dashboard-api-contract.md` (scope-filtered-first, ISO
  timestamps, semantic enums, existence-hiding 404). **Memory invariant:**
  [`docs/e2-inc3-kickoff.md`](e2-inc3-kickoff.md) (the observe-vs-originate line).
- **Roadmap placement (open item):** sits between **C3** (identity, Inc 1 built) and **E5**
  (read-write inflection). Label it **E2 Inc 5 / "Supervisor-interaction loop"** or a **C4**
  rung — settle when re-syncing the roadmap table at build time.

## Verification (build session)

- Backend: unit tests mirroring the comment tests — append + read + `since_id`; the write
  endpoint's auth/CSRF/scope (out-of-scope project → 404); role attribution is
  server-derived (a client-supplied role/author is ignored); supervisor-role provisioning.
- Producer: the CLI pull advances the watermark and the reply lands as `role=developer`.
- SPA: build/typecheck/tests green; eyes-on the per-project thread across 3 themes (the
  relay-serve local recipe), including XSS-inert rendering.
- **End-to-end loop:** supervisor posts (dashboard) → developer pulls + replies (CLI) →
  supervisor sees the reply (dashboard).

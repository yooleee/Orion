<!-- =========================================================================
docs/dashboard-api-contract.md
---------------------------------------------------------------------------
Responsible for: The read-only JSON API contract for the E2 Inc 4 dashboard
                 rebuild (the SPA <-> relay seam). One source of truth for the
                 endpoint set and response shapes the React/Vite SPA consumes.
Role in project: Defines the seam fixed in slice 4a.0 so the backend
                 serializers (relay/api.py) and the frontend types
                 (web/src/api/types.ts) are built against the same shapes.
                 Build plan: docs/e2-inc4-dashboard-rebuild-kickoff.md and the
                 approved 4a plan. Visual spec: design/README.md + screenshots.
========================================================================= -->

# Orion Dashboard JSON API Contract (slice 4a)

This is the seam between the React SPA and the relay. The relay becomes a read-only JSON API. Every
domain object is observed from external sources and read-only in the UI. The one user-authored surface is
the **supervisor-interaction discussion** thread (`POST /api/discussions/:project/items` + the Bearer
machine routes, below).

> **Consolidation note (KI-28, Stage 2):** comments and discussion were two overlapping conversation
> systems — the discussion loop is the identity-first, two-way successor to comments. Stage 1 (PR #74)
> made Discussion the project page's only conversation surface; **Stage 2 retired the comment routes
> outright** (no `report_id` tag — comments were folded into the discussion model and migrated at parity,
> the KI-23 / `render.py` precedent). The comment endpoints (`GET`/`POST /api/comments`,
> `POST /api/reports/:id/comments`), the `report_comments` store, and the `comments` response fields are
> gone; the discussion routes below are the single conversation contract.

## Conventions

- **Base path** `/api`. All responses are `application/json; charset=utf-8`.
- **Timestamps** are ISO 8601 UTC strings exactly as stored. The SPA formats relative time ("34m ago")
  client-side using the `display_tz` from `GET /api/me`. The server never ships pre-formatted
  human strings. This mirrors the existing progressive-enhancement approach (the old `_PAGE_JS`).
- **Dates** (deadlines) are ISO `YYYY-MM-DD` strings, passed through from the source.
- **Status enums** are semantic, not presentational. The server decides *which* state an item is in. The
  glyph, label, and colour for each state live frontend-side in `web/src/theme/status.ts`, so re-theming
  never touches the wire. The enum values:
  `not_started | in_progress | done | due_soon | overdue | upcoming | at_risk | slipping | on_track`.
  ("upcoming" is an open, dated deadline beyond the due-soon horizon: rendered as a neutral relative time,
  no glyph — used by `next_due`.)
  ("at_risk" is the union overdue-or-due-soon used for roll-up counts. "on_track" is a roll-up state for a
  milestone or a project row, not a per-item state.) The relay derives `state` as: `done` when finished;
  else `overdue` / `due_soon` from the deadline; else `in_progress` when the producer marked it so; else
  `not_started`. Deadline urgency leads (an overdue in-progress item reports `overdue`). The tracker's
  `in_progress` (gap 8, closed in the Tracker slice) now arrives as a first-class `status` field on the
  producer wire (see the `ChecklistItem` shape below), so the relay no longer parses the embedded status
  text. `submitted`/`closed` are done items whose `status` carries the finish-kind; both render via `done`.
- **Derivation runs server-side.** The serializers reuse `relay/derive.py` (`classify_item`, `milestones`,
  `slipping_item_keys`, and the new `bucket_counts` / `next_open_due`) so the at-risk / slipping / bucket
  math has one source of truth and the SPA stays thin.
- **Scoping.** Each handler runs: is the relay gated (`_auth_required`) -> resolve principal
  (`_authenticate`) -> if gated and no principal, return `401` -> resolve read scope
  (`_allowed_projects`, where `None` means unrestricted) -> filter or `404`. Out-of-scope projects and
  reports return `404`, identical to a genuinely missing one (existence-hiding preserved).
  **Exception:** `GET /api/me` is exempt from the `401` gate — it always returns `200` with the current
  state (including `authenticated:false`), because it is how the SPA learns it must log in.
- **Error model.** `401 {"error":"login required"}` when a gated relay has no valid session (the SPA
  routes to `/login`). `404 {"error":"not found"}` for missing or out-of-scope resources. No redirects
  from `/api/*`.

## Endpoints

### `GET /api/me`

Identity, scope, and server context. The SPA reads this once on boot to choose the full / scoped / empty
shell and to know whether to redirect to login.

```json
{
  "gated": true,
  "authenticated": true,
  "identity": { "name": "Teammate B", "role": "admin" },
  "scope": { "unrestricted": true, "projects": null },
  "display_tz": "America/Los_Angeles",
  "showcase_enabled": false
}
```

- `identity` is `null` when not authenticated. `role` is `"admin" | "viewer"`.
- `scope.unrestricted` is `true` for an admin or an open (ungated) relay, with `projects: null`. For a
  scoped viewer it is `false` with `projects` a sorted list of granted project names.
- On an open loopback relay: `gated:false, authenticated:false, identity:null, scope.unrestricted:true`.
- `showcase_enabled` reflects `server.showcase_enabled` (the relay's `--showcase` flag): `true`
  when the public Showcase surface is on, else `false`. The SPA shows the "Public showcase"
  sidebar link only when this is `true`. See `GET /api/showcase` below.
- Source: `_auth_required`, `_authenticate`, `_allowed_projects`, `server.display_tz`,
  `server.showcase_enabled`.

### `GET /api/portfolio`

The home dataset, scope-filtered, split into `projects` and `trackers` by `kind`.

The serializer ships derived **facts** per entry (counts, the nearest deadline). The SPA composes the
visible chips from those facts, so the ≤2-signal presentation (and the design's per-card nuances) stays
client-side.

```json
{
  "scope": { "unrestricted": false, "projects": ["orion"] },
  "projects": [
    {
      "name": "orion",
      "kind": "project",
      "headline": "Sectioned home in progress — splitting projects from to-dos.",
      "progress": { "done": 6, "total": 15, "pct": 40 },
      "at_risk": 2,
      "slipping": 0,
      "next_due": { "due_date": "2026-06-29", "state": "due_soon" },
      "updated_at": "2026-06-26T10:00:00+00:00",
      "report_id": 26
    }
  ],
  "trackers": [
    {
      "name": "applications",
      "kind": "tracker",
      "item_count": 15,
      "progress": { "done": 0, "total": 15, "pct": 0 },
      "segments": { "overdue": 1, "due_soon": 1, "remaining": 13, "done": 0 },
      "at_risk": 2,
      "slipping": 1,
      "next_due": { "due_date": "2026-07-02", "state": "due_soon" },
      "at_risk_items": [
        { "state": "overdue", "label": "Hack Your Summer", "due_date": "2026-06-24" },
        { "state": "due_soon", "label": "Claude Corps Fellow", "due_date": "2026-07-02" }
      ],
      "updated_at": "2026-06-26T09:00:00+00:00"
    }
  ]
}
```

- Source: `latest_report_per_project(conn, today=today_in_tz(display_tz))` for the counts + kind, plus
  `get_checklist` per project for the items the deadline / segment / chip derivations need. Filtered by
  scope exactly as the old `GET "/"` route. `headline` reuses the shared headline extraction.
  `progress.pct` is `round(done/total*100)`, or `null` when `total` is 0.
- **`kind`** comes from config (the explicit flag, see "kind" below). `projects` holds `kind == "project"`
  entries, `trackers` holds `kind == "tracker"`.
- Project facts: `at_risk` / `slipping` are counts (`checklist_at_risk` / `checklist_slipping`).
  `next_due` is the nearest open deadline (`derive.next_open_due`) with its state, or `null`. The SPA
  renders "△ N at risk", the deadline chip, or "✓ on track" when none.
- **Producer-merged at ≥2 producers (C3 Inc 2.5).** `progress`, `at_risk`, `slipping`, and `next_due`
  (and the project page's `stats`, milestones, scheduling, and report snapshot) are computed from the
  **effective checklist** — a done-OR union across active producers' per-producer checklists — when a
  project has **two or more** active identified producers; at 0–1 producers they come from the aggregate
  row unchanged. **Wire shapes are identical** either way; only the numbers reflect the merge.
- Tracker adds `segments` (the segmented bar, `derive.bucket_counts`: overdue / due_soon / remaining-open /
  done) and `at_risk_items` (every at-risk item, overdue-first then due_soon by date), each
  `{state, label, due_date}`. The SPA shows the first few chips and a "+N more".

### `GET /api/projects/:name`

Everything observed about one project. `404` when missing or out of scope.

```json
{
  "name": "orion",
  "kind": "project",
  "description": null,
  "stats": {
    "progress": { "done": 6, "total": 15, "pct": 40 },
    "next_due": { "due_date": "2026-06-29", "state": "due_soon" },
    "reports_count": 12
  },
  "milestones": [
    { "group": "Sectioned home", "done": 3, "total": 5, "at_risk": 0,
      "nearest_due": "2026-06-29", "slipping": false }
  ],
  "checklist": [
    { "text": "…", "done": false, "due_date": "2026-06-29", "key": "…",
      "group": "Sectioned home", "state": "due_soon", "status": null, "slipping": false }
  ],
  "producer_checklists": [
    { "author_name": "Teammate B", "progress": { "done": 3, "total": 5, "pct": 60 },
      "items": [ { "text": "…", "done": false, "due_date": null, "key": "…",
        "group": null, "state": "in_progress", "status": null, "slipping": false } ] }
  ],
  "reports": [
    { "id": 26, "title": "Orion progress update", "generated_at": "2026-06-26T10:00:00+00:00",
      "lane": "structured", "share_level": "high_level", "section_count": 4,
      "author_name": "Teammate B", "source_tags": [] }
  ],
  "discussions": [
    { "id": 1, "author_name": "Supervisor A", "role": "supervisor", "body": "How's the auth slice?",
      "created_at": "2026-06-26T09:00:00+00:00" },
    { "id": 2, "author_name": "Teammate B", "role": "developer", "body": "Landed.",
      "created_at": "2026-06-26T12:00:00+00:00" }
  ],
  "disciplines": {
    "cards": [
      { "title": "Observe & reframe, never originate",
        "why": "Plans and tasks are written in their own places; Orion reads and reframes them.",
        "source": "CLAUDE.md" }
    ],
    "updated_at": "2026-06-27T10:00:00+00:00"
  }
}
```

- Source: `history` (reports + count + nav), `get_checklist`, `observed_history` ->
  `slipping_item_keys`, `derive.milestones`, `classify_item` per item,
  `producer_checklists_for`, `discussion_items_for_project`, `project_disciplines`.
- `producer_checklists` (C3 Inc 2) is each **identified** producer's own live checklist —
  `{ author_name, progress, items }` per producer, ordered by name, the `items` in the SAME per-item
  shape as `checklist`. It is a dual-write beside the aggregate `checklist`. As of C3 Inc 2.5 the
  displayed `checklist`/`stats` derive from the **effective checklist** (a done-OR merge of these
  per-producer copies) at ≥2 producers, with the aggregate as the byte-identical <2-producer fallback; a
  legacy shared-token push writes the aggregate ONLY, so it leaves `producer_checklists` empty.
  `author_name` is server-derived and denormalized (survives revocation); `author_id` is **not** on the
  wire. The dashboard renders one card per producer only when there are **two or more** (a single
  producer's card would just duplicate the aggregate). Note (C3 Inc 2.5): each producer card's
  `items[].slipping` now reflects **that producer's own** observation stream (partitioned by `author_id`);
  the aggregate `checklist` rows, milestones, and scheduling use the project-wide union.
- `reports[].author_name` (C3 Inc 2) is the producer who pushed the report — a server-derived display
  name, or `null` for a legacy (shared-token) push or a report predating attribution. The name is
  denormalized at write time so it survives the user's later revocation; the internal `author_id` is
  **not** on the wire (same convention as `discussions`). The dashboard shows a "pushed by" only when it
  is non-null, so older reports render unchanged.
- `discussions` is the project's two-way **supervisor-interaction thread** (E2 Inc 5), oldest first —
  the persistent per-project conversation and, since KI-28 Stage 2, the **only** conversation surface.
  Each item carries a **real** `role` (`supervisor | developer`; `orion` reserved, unused) and a
  server-derived `author_name`. The internal `author_id` is **not** on the wire. Written via
  `POST /api/discussions/:project/items` (below).
- `disciplines` (Unit 5) is the project's **"Working agreements"** — the working principles Orion
  **observed** in this project's docs, or `null` when it never pushed any (an empty/cleared set also
  serializes to `null`, so the SPA simply omits the section). `cards[]` is `{title, why, source}` — the
  bold title, the "why" paragraph, and the repo-relative doc the `observed · <source>` footer cites;
  `updated_at` is the ISO time the relay last received them (the section's "updated `<date>`" line). **All**
  of the project's cards are emitted regardless of the model's `global`/`project` scope (the scope enum is
  consumed server-side and dropped from the wire card). `source` is **caller-stamped** by the producer,
  never model-chosen, so `observed · <source>` is literally true. Fed by `POST /disciplines` (below); the
  standalone cross-project `GET /api/disciplines` view retired in Unit 5.
- `stats.next_due` is the soonest open deadline across the checklist (`derive.next_open_due` + its state),
  or `null`. `milestones[].slipping` is `true` when any open item in the group is in the slipping set.
- `checklist[].state` is `done | overdue | due_soon | in_progress | not_started`: `done` when done, else
  `overdue` / `due_soon` from `classify_item`, else `in_progress` when `status == "in_progress"`, else
  `not_started`. `checklist[].status` is the raw producer status (`not_started | in_progress | submitted |
  closed`) or `null` for status-less items (e.g. table to-do rows). It rides alongside `state` so the
  tracker's circular indicator shows the in-progress arc and the submitted/closed label independently of
  the single derived state. `checklist[].slipping` is membership in `slipping_item_keys`.

### `GET /api/reports/:id`

One progress report in full. `404` when missing or out of scope (scope resolved via the report's project).

```json
{
  "id": 26,
  "project": "orion",
  "title": "Orion progress update",
  "sections": [["SHIPPED", "…"], ["DIRECTION", "…"], ["NOTES", "…"]],
  "body": "…",
  "lane": "structured",
  "share_level": "high_level",
  "generated_at": "2026-06-26T10:00:00+00:00",
  "ingested_at": "2026-06-26T10:01:00+00:00",
  "orion_version": "0.0.0",
  "author_name": "Teammate B",
  "participants": [ { "name": "Alex", "role": null }, { "name": "Sam", "role": null } ],
  "source_tags": [],
  "checklist_snapshot": {
    "done": 6, "total": 15,
    "rows": [ { "text": "…", "done": true, "state": "done", "due_date": null } ]
  },
  "nav": { "prev_id": 25, "next_id": null }
}
```

- Source: `get`, `get_checklist(report.project)` (the rail snapshot),
  `history(project)` for prev/next neighbours. `sections` is the stored `[title, body]` pairs.
- `author_name` (C3 Inc 2) is the producer who pushed the report, or `null` for a legacy/older report —
  same server-derived, revocation-durable, `author_id`-off-the-wire convention as the timeline entry above.
- The report carries **no** conversation of its own (KI-28 Stage 2 retired per-report comments); the
  project-level `discussions` thread on `GET /api/projects/:name` is the single conversation surface.
- `title` is the report's display title: the headline of `body` (the shared `_headline` helper, the body's
  first non-empty line, e.g. "Orion progress update"). The section labels (`SHIPPED`, `DIRECTION`, …) are
  the `sections` titles, distinct from this.
- `nav.prev_id` / `next_id` are the neighbouring report ids in `generated_at DESC, id DESC` order
  (the same ordering as `history`), `null` at the ends.

### `GET /api/scheduling`

The cross-project forward view: every **open, dated** deadline across all in-scope projects + trackers,
grouped into three time buckets, plus a summary. Scope-filtered identically to `/api/portfolio`.

```json
{
  "summary": { "overdue": 2, "due_this_week": 1, "slipping": 1 },
  "buckets": {
    "overdue": [
      { "state": "overdue", "label": "Introduction to Cooperative AI (course)",
        "due_date": "2026-06-12", "slipping": false,
        "source": { "name": "applications", "kind": "tracker" } }
    ],
    "this_week": [
      { "state": "due_soon", "label": "Wire comment writes", "due_date": "2026-07-01",
        "slipping": true, "source": { "name": "orion", "kind": "project" } }
    ],
    "later": [
      { "state": "upcoming", "label": "Claude Corps Fellow (job)", "due_date": "2026-07-17",
        "slipping": false, "source": { "name": "applications", "kind": "tracker" } }
    ]
  }
}
```

- **Only open, dated items appear.** Done items and items with no `due_date` are excluded — a timeline has
  no place for them. This is the honest reading of "every deadline."
- **Buckets** come from the same per-deadline classifier the rest of the dashboard uses
  (`_deadline_state` → `overdue` / `due_soon` / `upcoming`), mapped `overdue→overdue`,
  `due_soon→this_week`, `upcoming→later`. Each bucket is sorted by `due_date` ascending (soonest /
  most-overdue first). `LATER` has no horizon cap.
- **`label`** is `key ?? text` — the clean title (any embedded status stripped), the same rule the tracker
  page uses. **`source`** is `{name, kind}`; the tracker shows its project name (`applications`), since the
  "current focus" rename is an authoring surface (gap 2, held). The SPA renders `◇` for a project, `⊟` for
  a tracker.
- **`summary`** `{overdue, due_this_week, slipping}`: `overdue`/`due_this_week` are the bucket counts;
  `slipping` counts surfaced rows whose key is in `derive.slipping_item_keys` (the same set the project
  page uses). Each row also carries a `slipping` flag.
- **No `scope` block** (unlike `/api/portfolio`): the design has no scope banner here and the aggregation
  is already scope-filtered server-side. Additive if ever needed.
- Source: `latest_report_per_project(today)` (enumeration + `kind`, includes checklist-only trackers) +
  `get_checklist` + `observed_history` per project → `api.serialize_scheduling`. Pure read-only
  re-aggregation — no new derivation, no producer/wire/store change.

### `POST /disciplines` (producer push — machine, not the SPA)

A producer-side machine push (Bearer ingest token, like `POST /checklist` and `/ingest`) that sets a
project's observed disciplines as **current state** (full-state upsert, no report). Body
`{"project": "<name>", "disciplines": [{title, why, scope, source}, …]}`; each card's `title`/`why`/`source`
must be strings (non-empty title) and `scope` one of `global | project`. Returns `200 {"updated": "<name>",
"disciplines": <count>}`. An empty list clears the project's prior set. The producer reads the project's
own docs **unmodified** and reframes their stated principles via an **opt-in, cache-gated** LLM step
(`orion disciplines-push`); the docs are redacted before the model and the output redacted again before
the push. Stored in `relay_project_disciplines` (one row per project, replaced on each push) and read back
on `GET /api/projects/:name` as the `disciplines` field (the project page's "Working agreements" section).

### `GET /api/showcase`

The **public, no-login** curated surface — a small set of projects an operator has chosen to share
without a sign-in. Like `GET /api/me`, it is **exempt from the `401` gate** (a guest has no
session). It is **opt-in and default-deny**: disabled unless the relay was started with
`--showcase`, and it serves **only** the projects named with `--showcase-project` (the allowlist),
in allowlist order.

```json
{
  "projects": [
    { "name": "orion", "description": "A local-first tracker that observes & reframes.",
      "status": "active", "progress": { "done": 6, "total": 15, "pct": 40 }, "report_count": 12 },
    { "name": "sample-app", "description": "A sample project, shipped.",
      "status": "shipped", "progress": { "done": 4, "total": 4, "pct": 100 }, "report_count": 1 }
  ]
}
```

- **`404 {"error":"not found"}` when disabled** (`server.showcase_enabled` is false) — existence-
  hiding, identical to a genuinely missing route, so the surface is invisible when off.
- **Summary facts ONLY.** Each card carries exactly `name`, `description`, `status`, `progress`,
  `report_count` — and nothing else. No checklist items, reports, comments, or deadlines reach the
  anonymous viewer. The privacy boundary is the **shape** of the card (`api._showcase_card`), pinned
  by test; cards are **not** links (no public drill-down).
- **`description`** is the curated per-project blurb from `--showcase-project NAME:"blurb"`, falling
  back to the observed report `headline` when no blurb was set (then `""` if the project has no
  report body). **`status`** is derived from completion: `"shipped"` when `progress.pct == 100`,
  else `"active"`.
- **Curation is independent of viewer scope** (the guest is anonymous): the only access control is
  the allowlist. No `scope` block.
- Source: `latest_report_per_project(today)` filtered to the allowlist (in allowlist order) →
  `api.serialize_showcase`. Pure read-only re-aggregation of existing store data plus the config
  allowlist/blurbs — no producer/wire/store change. Config: relay `--showcase` +
  `--showcase-project` flags (the relay does not read `orion.toml`).

### `POST /api/login`

Body `{"key": "<access key>"}`. Verifies the key and, on success, sets a signed session cookie
(HttpOnly, SameSite=Lax, Secure when hosted) and returns the principal. The verify-and-mint logic is
the shared `_resolve_login` / `_mint_cookie` (kept factored out). The existing `_origin_error()` CSRF
check applies. This is the relay's only login surface — the legacy HTML form login retired with
`render.py` (KI-23).

```json
{ "ok": true, "user": { "name": "Teammate B", "role": "admin" } }
```

On a bad or revoked key: `401 {"ok": false}` (no cookie set).

### `POST /api/logout`

Clears the session cookie and returns `{"ok": true}`. The `_origin_error()` CSRF check applies.
`GET /logout` also remains (a 303 redirect that clears the cookie) and is unaffected by the
`render.py` retirement.

### `POST /api/discussions/:project/items`

Append one entry to a project's **supervisor-interaction thread** (E2 Inc 5) — the persistent, two-way
per-project conversation between a supervisor and the developer, and the **only user-authored write**
(KI-28 Stage 2 retired the comment write). Cookie session (not Bearer), JSON in/out, same-origin. The
thread anchor is the **project**, not a report.

Body `{"body": "<text>"}`. Returns `201` with the created item in the same shape the read path emits (the
`discussions[]` element above, so the SPA appends it without a refetch):

```json
{ "id": 12, "author_name": "Supervisor A", "role": "supervisor", "body": "How's the auth slice?",
  "created_at": "2026-06-27T01:00:00+00:00" }
```

- **Identity is fully server-derived.** `author_name`, `role`, and the stored `author_id` come from the
  authenticated principal, **never** the request body — a client-supplied `author`/`role`/`author_id` is
  silently ignored, so attribution is unforgeable. `role` maps from the principal's `relay_users` role:
  `supervisor → "supervisor"`, `admin → "developer"` (the developer/owner, including the legacy bootstrap
  admin). The `orion` item role is never producible by a human write (reserved for a later
  grounded-responder rung — observe-not-originate).
- **Auth is always required** — there is **no** open-loopback free-text-author path. An attributable
  thread needs identity, so the discussion loop requires a gated (C3) relay.
- **Guards, in order:** `401 {"error":"login required"}` when there is no session → `403 {"error":"origin
  check failed"}` on an Origin/Referer mismatch → `403 {"error":"not permitted"}` when the principal is a
  **viewer** (read-only, no thread standing) → `400` for a non-object body or a non-string/empty `body` /
  one over `MAX_COMMENT_BODY_CHARS` (4000) → `404 {"error":"not found"}` when the project is out of scope
  **or** does not exist (identical response — existence-hiding).
- **Append-only.** No edit or delete path; the thread is the memory.
- **XSS:** the body is stored verbatim and rendered as an inert React text node (never
  `dangerouslySetInnerHTML`), so a `<script>` body displays as literal text.
- **`MAX_COMMENT_BODY_CHARS` (4000) / `MAX_AUTHOR_CHARS` (200)** cap the body/author (the constants keep
  their historical names; they now bound the discussion write).
- Source: `_authenticate` / `_allowed_projects` / `_origin_error` / `history` / `get_checklist` /
  `add_discussion_item`.

#### `GET /api/discussions` + `POST /api/discussions` (developer CLI loop — machine, not the SPA)

The developer's terminal half of the loop (E2 Inc 5, Unit 3), **Bearer**-authed with the ingest token —
the machine siblings of the cookie write above. No cookie, no CSRF (a Bearer token is never
browser-auto-attached). Driven by `orion discussions pull` / `orion discussions reply`.

- **`GET /api/discussions?project=<name>&since_id=<int>`** — pull a project's thread for the terminal.
  `since_id` is optional (default 0 → all). Returns the **raw store rows** (not the SPA wire shape) plus a
  watermark, oldest first:

  ```json
  { "discussions": [ { "id": 7, "project": "orion", "author_id": 4, "author_name": "Supervisor A",
      "role": "supervisor", "body": "How's auth?", "created_at": "2026-06-26T09:00:00+00:00" } ],
    "latest_id": 7 }
  ```

  `latest_id` is the highest id returned, or `since_id` when nothing is newer (so the CLI advances its
  `(project, relay_url)` watermark unconditionally). An unknown project → `200` with `[]`. `400` on a
  missing `project` or a non-integer `since_id`; `401` (+ `WWW-Authenticate: Bearer`) on a bad token.

- **`POST /api/discussions`** — append the developer's reply. Body `{"project", "body", "author"?}`. The
  `author` is the CLI's optional `--as` display name, defaulting to the fixed label `"developer"`. Returns
  `201 {"id": <int>}`. **`role` is server-fixed to `"developer"` and `author_id` to `null`** — the ingest
  token authorizes "the developer" and nothing more, so this path can **never** forge a `supervisor` entry
  (a `role`/`author_id` in the body is ignored). `404` when the project does not exist (reports or a
  checklist), so a typo cannot spawn an orphan thread; `400` on an empty/oversized body; `401` on a bad
  token.

## The `kind` flag (projects vs trackers)

The home splits real software projects from general trackers (e.g. the applications tracker). This fact is
recorded explicitly in config rather than inferred. `orion.toml [projects.<name>]` gains
`kind = "project" | "tracker"` (default `"project"`). It threads through the producer wire (the ingest
blob and the checklist-push payload), is stored on the relay (a nullable column on
`relay_project_checklists`, defaulting to `"project"` when absent), and is surfaced by the portfolio and
project serializers. This keeps the projects-vs-trackers distinction an observed property of the user's own
config, consistent with observe-not-originate.

## Data gaps (design needs the current store does not yet carry)

These degrade gracefully in 4a and close later by extending the producer/wire (out of the read-only 4a
backend scope). Each is shown above with its 4a value.

1. **Tracker segmented bar** — added in 4a via the pure `derive.bucket_counts`. In scope.
2. **Tracker display name** ("current focus" in the design) — the relay has only the project name
   (`applications`). 4a shows the project name. An editable rename is an authoring surface, held.
3. **Recipient roles** ("Alex · supervisor") — `participants` are plain name strings. 4a ships
   `role: null`. Closing it needs richer participants on the blob.
4. **Report / project `source_tags`** ("git history · checklist · session") — the relay does not store the
   originating collector set. 4a ships `source_tags: []`. Closing it needs `collectors` on the blob.
5. **Project `description`** — none stored. 4a ships `description: null`.
6. **Report `title`** — no separate title field. 4a uses the first section title, else the body headline.
7. **Conversation author `role`** — **CLOSED** (E2 Inc 5 + KI-28 Stage 2). The one conversation surface
   is now the discussion thread, whose `discussions[]` (on `GET /api/projects/:name`) and
   `POST /api/discussions/:project/items` response carry a real, server-derived `role`
   (`supervisor | developer`) — discussion items have first-class identity. The former free-text,
   `role: null` comment surface was retired outright, so no `role: null` conversation author remains.
8. **Embedded item status** (`in_progress` / `submitted`, the tracker's circular indicators) — **CLOSED**
   (Tracker slice, E2 Inc 4). The producer now ships a first-class `status` field (`not_started |
   in_progress | submitted | closed`, the semantic form of its canonical status) alongside the still-present
   text embed (so existing stored reports are untouched). The relay folds `in_progress` into `state` and
   passes the raw `status` through; the SPA renders the circular in-progress arc and the submitted/closed
   label from it. We chose the producer-field path over a relay-side text parse so status is a clean
   observed property end-to-end (the foundation later supervisor-side features build on), not a string the
   relay reverse-engineers across the process boundary.

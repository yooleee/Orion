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
domain object is observed from external sources and read-only in the UI. The only user-authored content is
comments; the SPA write path is `POST /api/reports/:id/comments` (below). (4a originally shipped the
composer inert; the comment-writes slice wired it.)

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
  "identity": { "name": "Yusuf", "role": "admin" },
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
  "reports": [
    { "id": 26, "title": "Orion progress update", "generated_at": "2026-06-26T10:00:00+00:00",
      "lane": "structured", "share_level": "high_level", "section_count": 4, "source_tags": [] }
  ],
  "comments": [
    { "id": 1, "author": "Alex", "role": null, "body": "…",
      "created_at": "2026-06-25T09:00:00+00:00" }
  ]
}
```

- Source: `history` (reports + count + nav), `get_checklist`, `observed_history` ->
  `slipping_item_keys`, `derive.milestones`, `classify_item` per item, `comments_for_project`.
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
  "participants": [ { "name": "Alex", "role": null }, { "name": "Sam", "role": null } ],
  "source_tags": [],
  "checklist_snapshot": {
    "done": 6, "total": 15,
    "rows": [ { "text": "…", "done": true, "state": "done", "due_date": null } ]
  },
  "comments": [
    { "id": 1, "author": "Alex", "role": null, "body": "…",
      "created_at": "2026-06-25T09:00:00+00:00" }
  ],
  "nav": { "prev_id": 25, "next_id": null }
}
```

- Source: `get`, `get_checklist(report.project)` (the rail snapshot), `comments_for(id)`,
  `history(project)` for prev/next neighbours. `sections` is the stored `[title, body]` pairs.
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

### `GET /api/disciplines`

The **Disciplines & directions** section (E2 Inc 4 slice 4b): the working principles Orion **observed**
in the user's own docs, split into a **Global** group (conventions across all projects) and per-project
groups. Scope-filtered like `/api/portfolio`. Each card is `{title, why, source}` — a bold title, a
"why" paragraph, and the repo-relative doc the `observed · <source>` footer cites.

```json
{
  "scope": { "unrestricted": false, "projects": ["orion"] },
  "global": [
    { "title": "Untrusted text is inert",
      "why": "Commit messages and task names are always rendered as plain text — a hard security rule.",
      "source": "design/README.md" }
  ],
  "projects": [
    { "name": "orion",
      "principles": [
        { "title": "Observe & reframe, never originate",
          "why": "Plans and tasks are written in their own places; Orion reads and reframes them.",
          "source": "CLAUDE.md" }
      ] }
  ]
}
```

- **`global`** are the `scope == "global"` cards across all in-scope projects, **deduped by normalized
  title** (a global convention may be stated in several projects' docs), the source picked
  deterministically (the lexicographically-first `(project, source)`), sorted by title.
- **`projects`** are `scope == "project"` cards grouped under their project, projects sorted by name and
  cards by title. A project with no project-scope cards is **omitted** (no empty group).
- **`scope`** is the same `{unrestricted, projects}` block `/api/portfolio` ships. **Scope-filtered FIRST:**
  a global principle declared only in an out-of-scope project never reaches a scoped viewer (its presence
  and source path would leak that project's existence) — existence-hiding, like the other routes.
- **Honest extraction.** `source` is **caller-stamped** by the producer collector (the repo-relative doc),
  never model-chosen, so `observed · <source>` is literally true. The `scope` enum is consumed by the
  grouping and dropped from the wire card.
- Source: `disciplines_projects` (enumerate projects that have pushed disciplines — NOT
  `latest_report_per_project`, so a disciplines-only project is not missed) + `get_disciplines` per
  in-scope project → `api.serialize_disciplines`.

#### `POST /disciplines` (producer push — machine, not the SPA)

A producer-side machine push (Bearer ingest token, like `POST /checklist` and `/ingest`) that sets a
project's observed disciplines as **current state** (full-state upsert, no report). Body
`{"project": "<name>", "disciplines": [{title, why, scope, source}, …]}`; each card's `title`/`why`/`source`
must be strings (non-empty title) and `scope` one of `global | project`. Returns `200 {"updated": "<name>",
"disciplines": <count>}`. An empty list clears the project's prior set. The producer reads the project's
own docs **unmodified** and reframes their stated principles via an **opt-in, cache-gated** LLM step
(`orion disciplines-push`); the docs are redacted before the model and the output redacted again before
the push. Stored in `relay_project_disciplines` (one row per project, replaced on each push).

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
    { "name": "barebones-ai-village", "description": "A structural detection layer — shipped.",
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
{ "ok": true, "user": { "name": "Yusuf", "role": "admin" } }
```

On a bad or revoked key: `401 {"ok": false}` (no cookie set).

### `POST /api/logout`

Clears the session cookie and returns `{"ok": true}`. The `_origin_error()` CSRF check applies.
`GET /logout` also remains (a 303 redirect that clears the cookie) and is unaffected by the
`render.py` retirement.

### `POST /api/reports/:id/comments`

Post a comment on a report — the **only user-authored write**. Cookie session (not Bearer), JSON in/out.
The SPA's `apiFetch` sends it same-origin with credentials (like `/api/login`), so the cookie rides
automatically and the CSRF check passes.

Body `{"body": "<text>", "author"?: "<name>"}`. Returns `201` with the created comment in the same shape
the read path emits (so the SPA appends it without a refetch):

```json
{ "id": 12, "author": "Yusuf", "role": null, "body": "Looks great.", "created_at": "2026-06-27T01:00:00+00:00" }
```

- **Guards, in order** (reusing the shared auth/scope/origin helpers):
  `401 {"error":"login required"}` when gated + no session → `403 {"error":"origin check failed"}` on an
  Origin/Referer mismatch (`_origin_error`) → `400` for a non-object body, a non-string/empty `body`, or
  `body`/`author` over `MAX_COMMENT_BODY_CHARS` (4000) / `MAX_AUTHOR_CHARS` (200) → `404 {"error":"not
  found"}` when the report is missing **or** out of the viewer's scope (identical response —
  existence-hiding).
- **Identity:** when authenticated, the comment is attributed to the **session identity** —
  the client-supplied `author` is ignored (no posting under another name). On an open loopback relay (not
  gated) the typed `author` stands, or `""` → rendered "Anonymous".
- **XSS:** the body is stored verbatim and rendered as an inert React text node (never
  `dangerouslySetInnerHTML`), so a `<script>` body displays as literal text.
- This is **distinct** from the machine `POST /api/comments` (Bearer-authed, for bots — no cookie, no
  CSRF). It is the **only** browser comment write — the legacy form route `POST /report/:id/comment`
  retired with `render.py` (KI-23).
- Source: `_authenticate` / `_allowed_projects` / `_origin_error` / `get` / `add_comment` (all reused).

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
7. **Comment author `role`** — free-text author name only. 4a ships `role: null`.
8. **Embedded item status** (`in_progress` / `submitted`, the tracker's circular indicators) — **CLOSED**
   (Tracker slice, E2 Inc 4). The producer now ships a first-class `status` field (`not_started |
   in_progress | submitted | closed`, the semantic form of its canonical status) alongside the still-present
   text embed (so existing stored reports are untouched). The relay folds `in_progress` into `state` and
   passes the raw `status` through; the SPA renders the circular in-progress arc and the submitted/closed
   label from it. We chose the producer-field path over a relay-side text parse so status is a clean
   observed property end-to-end (the foundation later supervisor-side features build on), not a string the
   relay reverse-engineers across the process boundary.

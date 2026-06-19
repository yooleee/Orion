<!-- =========================================================================
c2-kickoff.md
---------------------------------------------------------------------------
Responsible for: The scoped kickoff for Horizon C2 (bidirectional) — the design
                 settled in the 2026-06-19 C2 design pass, ready to BUILD in a
                 fresh session. No code written yet; this is the plan-before-code
                 artifact.
Role in project: C2 is Orion's first INBOUND surface. This doc scopes the smallest
                 first slice (dashboard comments on the deployed relay) and pins the
                 decisions + the inbound-security checklist that is the slice's
                 center of gravity. See plans/orion-plan.md (C2 row, "Horizon-C
                 direction settled") and docs/orion-strategy.md.
Companion: docs/deployment.md (the relay is live on Fly — the always-on host this
           rides on); the C1 kickoff is in docs/archive/phase-c1-kickoff.md.
========================================================================= -->

# C2 kickoff — bidirectional, first slice: dashboard comments

## Why this slice, and why now

C2 makes the loop **two-way**: supervisors act back on reports. It is the project's
**first inbound surface** — which is why it's the biggest architectural + security shift since
the ingest endpoint (an always-on process, *untrusted input*, an authorization story).

**Deploying C1 collapsed the hard part.** The relay is now live on Fly 24/7, so the always-on
process C2 needs **already exists**. That makes **dashboard comments** the smallest possible first
slice — it *extends the running relay*, with no bot, no new infrastructure, and no per-platform
OAuth. It also validates the core hypothesis — *do supervisors actually reply?* — **before** paying
the bot build-and-maintain tax. Native Discord/Slack replies stay the deferred richer add-on.

## Settled decisions (2026-06-19 design pass)

| # | Decision | Choice |
| - | -------- | ------ |
| D1 | First slice | **Dashboard comments on the deployed relay.** Bots (native Discord/Slack threads) deferred as the richer add-on. |
| D2 | Authorization + identity | **Reuse the dashboard view secret** to gate commenting (read = comment), plus an **optional self-entered display name** per comment (free text, not authenticated). Authenticated per-person identity is **C3 / KI-17**, not now. |
| D3 | Loop scope | **Dashboard-only this slice.** Comments live on the dashboard; you read them by opening it (you hold the view secret). Surfacing comments back into your workflow (an `orion comments` pull / the session skill) is the **next increment**, deferred. |
| D4 | Comment model | **Append-only, flat** (no threading, edit, or delete). **Plain text only**, `_esc`-escaped on render. v1 simplicity. |

## The center of gravity: inbound = untrusted input + authorization

A comment endpoint is the first **write-from-a-browser** surface. This checklist *is* the slice — treat
it with more rigor than the feature itself:

- **Auth.** Gate the comment POST behind the **view secret** exactly as the GET dashboard is: when a
  view secret is configured, require HTTP Basic; when not (loopback dev), open — consistent with reads.
- **Stored XSS (non-negotiable).** Comment author + body are user text rendered on the dashboard →
  **every** field goes through the existing `_esc` escape path. The classic comment-section vuln; the
  relay's "every dynamic value is escaped" discipline (pinned by render tests) extends to comments.
- **CSRF.** Basic-auth means the browser auto-sends credentials, so a malicious page could forge a
  comment POST. Mitigate with an **Origin-header check**: require `Origin` present and its host equal
  to the request `Host`; reject mismatches with 403. (On Fly the browser Origin is
  `https://<app>.fly.dev`, matching the proxied Host — works behind the proxy.)
- **Validation.** Cap the body length (e.g. a few KB) and the author length; require a non-empty body;
  reject oversized/empty with 400. Mirror the ingest endpoint's "never trust a client-sent length".
- **Not redaction-scanned, deliberately.** Redaction is an *outbound*-leak control (the developer's
  secrets); a supervisor's inbound comment isn't that, and it's shown only on the access-controlled
  dashboard. XSS-escaping is the relevant control here, not redaction.

## Data model (relay/store.py) — additive, auto-migrating

```sql
CREATE TABLE IF NOT EXISTS report_comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id   INTEGER NOT NULL,          -- the relay_reports.id this hangs off
    author      TEXT NOT NULL,             -- self-entered display name, or "" when omitted
    body        TEXT NOT NULL,             -- plain text; escaped on render
    created_at  TEXT NOT NULL              -- ISO-8601 UTC, when the relay received it
);
CREATE INDEX IF NOT EXISTS idx_report_comments_report ON report_comments(report_id);
```
`open_relay_store` already runs `CREATE TABLE IF NOT EXISTS`, so the **deployed** relay's volume DB
gains this table automatically on the next startup — no manual migration. New functions:
`add_comment(conn, report_id, author, body, created_at) -> int` and `comments_for(conn, report_id)`.

## Endpoint + render design

- **`POST /report/<id>/comment`** (new route in `do_POST`, which today only handles `/ingest`):
  auth → Origin/CSRF check → validate (non-empty body, length caps) → confirm the report exists
  (`get()`; 404 if not) → `add_comment` → **303 redirect to `/report/<id>`** (POST-redirect-GET, so a
  refresh doesn't resubmit).
- **`GET /report/<id>`** gains a **Comments** section: the escaped list (author · `_format_ts` · body)
  followed by a small `<form method="post" action="/report/<id>/comment">` — an optional name input, a
  body textarea, submit. All dynamic values via `_esc`.

## File-by-file (for the build session)

- `relay/store.py` — the `report_comments` table + `add_comment` / `comments_for`.
- `relay/server.py` — `POST /report/<id>/comment` (auth + Origin/CSRF + validate + store + 303);
  pass comments into `render_report` on GET.
- `relay/render.py` — the comments list + comment form on the report detail page (escaped).
- `tests/test_relay_store.py` — comment round-trip.
- `tests/test_relay_server.py` — comment POST: authed-store, no-auth-401, bad-Origin-403,
  missing-report-404, empty/oversized-400.
- `tests/test_relay_render.py` — comments render escaped (a `<script>` body is neutralized); the form
  renders.
- `plans/orion-plan.md` (C2 row) + `CHANGELOG.md` — on completion.

## Deferred (record; do not pull in)

- **Push comments back to the developer** (`orion comments` pull / session-skill surfacing) — the
  next increment.
- **Native Discord/Slack replies (bots)** — the richer add-on (Gateway / Socket Mode; the
  build-and-maintain cost).
- **Authenticated per-person identity** — C3 / KI-17 (the self-entered name is the lightweight stand-in).
- Threading, edit/delete, markdown, rate limiting.

## Verification

- Unit/integration tests above; `pytest` stays green.
- **Security proof points (tests):** a `<script>` comment is escaped on render; a POST without Basic
  creds (view secret set) is 401; a cross-origin POST is 403.
- **End to end against the deployed relay:** after building + testing locally, **`fly deploy`** to
  update the live relay, then comment on a report through the real dashboard and confirm it persists
  (the volume DB auto-creates the new table).

## The C2 → C3 boundary (kept clean)

Comments-with-a-self-entered-name is the *lightweight* identity stand-in; **authenticated** identity
(who really said it), per-supervisor routing, and access control are **C3** (KI-17 is the same theme).
The portable blob and the relay's "validate required fields without rejecting extras" seam keep an
`author`/identity upgrade additive — no rewrite.

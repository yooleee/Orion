<!-- =========================================================================
phase-c2-kickoff.md
---------------------------------------------------------------------------
Responsible for: The scoped kickoff for the C2 comment PULL-BACK increment —
                 surfacing supervisor comments from the relay back into the
                 developer's workflow. Plan-before-code artifact for a fresh
                 session; no code written yet.
Role in project: The C2 first slice (dashboard comments) made the loop two-way ON
                 the dashboard; this increment closes the loop into the developer's
                 workflow. Pins the decisions + surfaces the open ones for the build
                 session's plan-mode pass. See plans/orion-plan.md (C2 row) and
                 docs/orion-strategy.md.
Companion: the first-slice kickoff is docs/c2-kickoff.md (dashboard comments,
           built + deployed 2026-06-19); the C1 kickoff is in
           docs/archive/phase-c1-kickoff.md.
========================================================================= -->

# Phase C2 kickoff — comment pull-back (`orion comments` + skill + unread tracking)

> Read this first, then do a plan-mode pass per `CLAUDE.md` (plan before code every phase)
> to confirm the open decisions below before writing code.

## Context

C2 made the loop two-way **on the dashboard** — supervisors comment, and you read them by
opening the dashboard (the deliberate D3 scope of the first slice). This increment **closes
the loop into your workflow**: supervisor replies are pulled back to your machine so you see
them where you work, without opening the dashboard. It is the kickoff-named "next increment"
(`docs/c2-kickoff.md` → Deferred → "Push comments back to the developer").

**Scope chosen (2026-06-19): the full version** — a new `orion comments` CLI command, **unread
tracking** (show only what's new since last check), **and** session-skill surfacing (replies
appear in your Claude Code session after you report). All stdlib; **no new dependencies**.

North-star fit: a supervisor replying to *your* reports — the loop getting richer, not
multi-party. Authenticated per-person identity stays **C3 / KI-17**; the self-entered comment
`author` is the lightweight stand-in, carried through unchanged.

## The key fact that shapes the design

Comments live on the relay keyed by the relay's `report_comments.id` / `report_id`, but the
**local side never recorded those ids** (the push discards the `{"id"}` the relay returns). So
the natural pull is **by project**, not by an id the client doesn't hold — which also matches
how you think (in projects). Unread tracking then needs a **watermark** the client persists.

## Settled decisions (confirm in the build session's plan-mode pass)

- **Pull by project**, via a new relay read endpoint + a `comments_for_project` store query.
- **Auth = the Bearer ingest token** the local client already holds (`RelayConfig.token_env_var`).
  One relay secret, same owner principal (whoever can push your reports can read their replies).
  *Alternative surfaced:* the Basic view token — rejected because the local config doesn't model
  it and it's the browser scheme.
- **Unread = an id watermark.** Client persists `last_seen_comment_id` per project; the endpoint
  filters `WHERE c.id > ?`. Ids are a monotonic autoincrement → robust, no clock/precision/tie
  issues (preferred over a `created_at`-since filter). Default run shows new + advances the
  watermark; `--all` shows everything without advancing.
- **Machine-JSON namespace, separate from the browser dashboard.** New route under `/api/…`
  (Bearer + JSON), leaving the existing `/project/<name>` HTML routes (Basic-gated) untouched —
  so the two consumers keep distinct auth schemes cleanly. *Alternative:* `/project/<name>/comments`
  (same prefix, but then do_GET must branch auth by route — messier).
- **`--json` output** for the skill to consume; human-readable (author · Pacific time · body)
  is the CLI default.
- **Stateless relay, watermark local.** The relay stays a dumb append-only store; "unread" is a
  per-developer local notion (correct — two readers shouldn't share one read-cursor).

## Design

**Relay read endpoint** — `GET /api/comments?project=<name>&since_id=<n>` (Bearer-authed):
- Parse the query string (note: `do_GET` currently discards it — this route reads
  `urllib.parse.urlparse(self.path).query` via `parse_qs`).
- Validate: `project` required (non-empty); `since_id` optional, must be a non-negative int
  (reject non-numeric → 400; default 0). "Never trust client input," mirroring `_read_raw_body`.
- New store query `comments_for_project(conn, project, since_id=0) -> list[dict]`:
  `SELECT c.* FROM report_comments c JOIN relay_reports r ON c.report_id = r.id
   WHERE r.project = ? AND c.id > ? ORDER BY c.id ASC` (the comment→project join the schema
  currently can't do directly).
- Respond `_send_json(200, {"comments": [...], "latest_id": <max id or since_id>})` so the client
  can advance its watermark even when the page renders differently. Empty project → `{"comments":
  [], "latest_id": since_id}` (200, not 404 — matches the dashboard's empty-state philosophy).
- **Auth branch:** in `do_GET`, detect the `/api/` route and apply the **Bearer** `_auth_error()`
  gate (not the Basic view gate that fronts the HTML routes). Reuse `_send_json` for the 401.

**Local pull client** — extend `src/orion/delivery/relay.py` (or a sibling) with
`pull_comments(base_url, token, project, since_id, *, timeout=10.0) -> dict`: a GET mirroring
`push()` (urllib + `Authorization: Bearer`, `DeliveryError` on HTTP/URL error). **Build-time check:**
confirm whether `RelayConfig.url` is the base or the `/ingest` URL and derive the `/api/comments`
URL accordingly (reuse however `push` derives its target).

**Local watermark** — `src/orion/state.py`: a small dedicated table
`comment_watermark(project TEXT, relay_url TEXT, last_seen_comment_id INTEGER, updated_at TEXT)`
with `get_comment_watermark(project, relay_url)` / `set_comment_watermark(...)`. Keyed by
(project, relay_url) so changing relays doesn't silently reuse a stale cursor. *(Alternative:
overload the `collector_markers` get/set pattern — rejected as semantically muddy.)*

**CLI** — `cmd_comments(project, config_path, *, as_json, show_all) -> int` registered as a new
`comments` subparser (mirror `cmd_intake` / `cmd_projects`): load config + secrets, read the
watermark (unless `--all`), `pull_comments(...)`, print (human or `--json`), then advance the
watermark to `latest_id` (unless `--all`). Errors name the missing secret like the push path.

**Skill** — `~/.claude/skills/orion-session/SKILL.md`: after a successful `orion intake`, run
`orion comments <project> --json`, and if any replies came back, surface them to the user
("Since your last report, <supervisor> said: …"). Closing the loop inside the session.

## File-by-file & checkpoints (stop for review after each)

1. **Relay read side** — `comments_for_project` (`relay/store.py`) + `GET /api/comments`
   (`relay/server.py`, Bearer + query-parse + validate + JSON) + tests
   (`tests/test_relay_store.py`, `tests/test_relay_server.py`): project filter, `since_id`
   incremental, Bearer-required (401), bad `since_id` (400), empty project (200 empty).
2. **Local pull client** — `pull_comments` in `delivery/relay.py` + tests (reuse the relay
   test-server harness; auth header, error mapping).
3. **Local watermark** — `comment_watermark` table + get/set in `state.py` + tests
   (round-trip, per-(project,relay) scoping, default 0).
4. **CLI command** — `cmd_comments` + `--json`/`--all` flags + dispatch wiring + `tests/test_cli.py`
   (new-only default, `--all`, watermark advance, `--json` shape, missing-secret error).
5. **Skill** — update `SKILL.md` to fetch + surface replies after intake (prose + the
   `orion comments --json` step).
6. **Docs** — `plans/orion-plan.md` C2 row (loop closed into workflow), `CHANGELOG.md`; close the
   `docs/c2-kickoff.md` "Deferred → push comments back" item.

## Security & seams

- **New authed relay READ endpoint.** Comments aren't the developer's secrets (they're inbound
  supervisor text on an access-controlled relay — *not* redaction-scanned, same reasoning as the
  comment POST), but the endpoint is still Bearer-gated; auth is checked before any query.
- **Input validation** on `project` (parameterized query — no injection) and `since_id` (int or
  400). Mirrors the ingest endpoint's "never trust a client-sent value."
- **C2→C3 seam intact:** the pull returns the self-entered `author` (lightweight identity);
  authenticated identity is C3/KI-17. The `/api/` namespace + "validate required params, ignore
  extras" keeps an identity/field upgrade additive.
- **Cross-platform:** all stdlib (`urllib`, `sqlite3`, `argparse`); no new deps; no OS-specific
  paths. Holds the open-source-simplicity bar.

## Verification

- Unit/integration tests above; `pytest` stays green across the matrix. ✅ **Done:** 304 tests
  pass across py3.11–3.13 × macOS/Ubuntu/Windows (PR #10, merged 2026-06-19).
- **Security proof points (tests):** `/api/comments` without Bearer → 401; a bad `since_id` → 400.
  ✅ **Done** (both covered in `tests/test_relay_server.py`).
- **Local loopback end-to-end:** `relay-serve` on `127.0.0.1`; `intake` a report; comment on it
  via the dashboard; run `orion comments <project>` → the comment appears; run again → nothing new
  (watermark advanced); `--all` → shows it again. Then exercise the skill flow.
- **Against the deployed relay:** `fly deploy`, then `orion comments` against the live relay pulls a
  real dashboard comment back. ✅ **Done (2026-06-19):** deployed; ran `orion comments orion` plain,
  `--json`, and `--all` against the live relay (`orion-relay-horizon-c.fly.dev`) and pulled real
  dashboard comments back. Then exercised the **full `orion-session` skill flow** end to end —
  drafted a summary, delivered to both supervisors (Discord + Slack), pushed to the dashboard, and
  the skill's step-6 pull surfaced a fresh supervisor reply back into the session. Loop closed and
  dogfooded.

## Out of scope (record; do not pull in)

Native Discord/Slack bot replies (the C2 bots gate — better settled *after* the hackathon dogfood
read); authenticated per-person identity (C3/KI-17); editing/deleting comments; the message-
formatter→Pacific alignment (KI-20, do when enriching delivery); a Content-Security-Policy (KI-19).

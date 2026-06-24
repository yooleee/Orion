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
see [`plans/orion-plan.md`](plans/orion-plan.md); for open issues and cross-phase concerns,
see [`docs/known-issues.md`](docs/known-issues.md).

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
  run" — see [`plans/orion-plan.md`](plans/orion-plan.md) "D1". Dependency-free (stdlib has no TOML
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

### Changed

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

The next Horizon-C slice ([`docs/phase-c2-bots-kickoff.md`](docs/archive/phase-c2-bots-kickoff.md)): an
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
new dependencies**. `pytest`: **304**. See [`docs/phase-c2-kickoff.md`](docs/archive/phase-c2-kickoff.md).

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
[`plans/orion-plan.md`](plans/orion-plan.md) (Phase B5 status) and
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

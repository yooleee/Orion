# Orion command cheat sheet

A quick reference for every `orion` command. Run any command with `--help` for full options.
Every command takes `--config PATH` (default `orion.toml`, or set `$ORION_CONFIG`).

> Setup: copy `orion.toml.example` → `orion.toml` and `.env.example` → `.env`, then edit.
> Full setup walkthrough: [`docs/new-project-setup.md`](new-project-setup.md).

## Typical dogfood loop

```bash
orion check                    # validate config + .env before anything
orion baseline myproject       # mark "now" as already-reported (skip the whole back-history)
# ... do some work, make commits ...
orion report myproject         # build a report, PREVIEW it, confirm, send
orion discussions pull myproject   # pull supervisor messages back to your machine
```

## Daily commands

| Command | What it does |
|---|---|
| `orion report <project>` | Collect activity (git/tasks/notes/incubator), summarize, **preview**, then send. The main command. |
| `orion report --all --yes` | Report every project, non-interactive (for schedulers; only sends projects with `auto_send=true`). |
| `orion intake <project> -m "…"` | Send a hand-written update (no LLM). Omit `-m` to type it on stdin. |
| `orion discussions pull <project>` | Pull a project's two-way supervisor↔developer thread from the relay (only new ones; advances your unread marker). `--all` re-reads everything without moving the marker. |
| `orion discussions reply <project> "<text>"` | Post your reply into the project's thread (lands as role `developer`). `--as "<name>"` sets the display label. |

## Setup & inspection

| Command | What it does |
|---|---|
| `orion check` | Validate the config and send-readiness (`.env` secrets present). Read-only — sends nothing. |
| `orion status` | Show which projects have **unreported activity** across the config. Read-only — sends nothing. |
| `orion projects` | List every project defined in the config. |
| `orion show <project>` | Show one project's resolved config (paths, share level, collectors, recipients). |
| `orion add-project [name]` | Register a new project in `orion.toml` (**the only command that writes config**). Name defaults to the repo directory. `--recipient "Name:channel:ENV_VAR"` (repeatable), `--like <project>` to copy recipients, `--repo-path`, `--share-level`, `--collectors git,tasks,notes`, `--tasks-file`/`--notes-file`; `--print` to preview the stanza, `--yes` to skip confirmation. When `tasks` is enabled and `--tasks-file` is omitted, it defaults to `<repo>/TODO.md` and **creates a starter checklist** there (preview-gated, never overwriting); pass an explicit `--tasks-file` to keep config-only. |
| `orion baseline <project>` | Mark current state as already-reported **without sending** — so the next report covers only new activity (avoids dumping full history). |
| `orion install-hook <project>` | Install a git hook so a push auto-reports. `--hook pre-push` (default) or `post-commit`; `--print` to review first, `--force` to overwrite. |
| `orion graduate-idea "<idea>"` | Register a **graduated** incubator idea as a new project (delegates to `add-project`; name slugified from the title). `--force` to graduate a non-graduated idea, `--name` to override, `--incubator-file` to point at the index directly. |

## Two-way: the relay + the Slack bot

| Command | What it does |
|---|---|
| `orion relay-serve` | Run the relay locally (ingest endpoint + read-only dashboard) on `127.0.0.1:8787`. Needs `ORION_RELAY_TOKEN` in `.env`. Blocks until Ctrl-C. |
| `orion checklist-push <project>` | Push the project's **current checklist** to the relay dashboard **without a report** (needs `checklist = true` + a `tasks` or `tracker` source + an enabled `[relay]`). Add `--watch` for near-real-time: it polls the checklist source (`tasks_file` and/or `tracker_file`) and pushes on every change until Ctrl-C (`--interval` seconds, default 3). |
| `orion bot` | **PARKED** (KI-28 Stage 2): the bot's write path (relay comments) retired, so `orion bot` prints a parked notice and exits. It is revived — repointed at the discussion write with honest supervisor attribution — once per-user keys land in the follow-on slice. |

**`orion bot` is parked.** Its comment write target was retired in KI-28 Stage 2; pointing it at the
discussion write must wait for per-user keys (that Bearer path stamps role `developer`, but a chat reply
is supervisor speech). The pure decision core and the Slack shell are kept as the revival seam. Full
context: [`docs/slack-bot.md`](slack-bot.md).

## Dashboard user management (`relay-user`)

Provision and manage who can log into the relay dashboard. These talk to a running relay's
`/api/users` endpoint over HTTP, authenticated with the **separate** admin token
(`admin_token_env_var` in `[relay]`, e.g. `ORION_RELAY_ADMIN_TOKEN` in `.env`) — never the
ingest token. Full auth model: [`docs/dashboard-auth.md`](dashboard-auth.md).

| Command | What it does |
|---|---|
| `orion relay-user add <name>` | Provision a user and print their access key **once** (it's never retrievable later, only revocable). `--role viewer` (default) or `--role admin`. |
| `orion relay-user add <name> --role viewer --project a --project b` | A **viewer** scoped to specific projects (`--project` repeatable). A viewer with no projects sees nothing. |
| `orion relay-user add <name> --role admin` | An **admin** sees **all** projects (present and future). The role does not grant user-provisioning — that stays gated on the admin token. |
| `orion relay-user list` | List users with role, status (active/revoked), scope, and last login. Shows **no** credential material. |
| `orion relay-user revoke <name>` | Revoke a user: deactivate their key and force-log-out any live session. |

## Handy flags

- `--yes` / `-y` — skip the preview (only sends projects with `auto_send=true`). For schedulers and
  the session skill; **without it, every run previews first.**
- `--config PATH` — point at a non-default config (or set `$ORION_CONFIG` once).
- `--json` (on `discussions pull`) — raw JSON instead of the human listing (used by the session skill).

## Exit codes (for scripts/schedulers)

`0` = success **or** a routine no-op (nothing new to send, user aborted the preview). `1` = a real
failure (bad config, missing secret, send failed). `2` = usage error (e.g. neither `<project>` nor
`--all`).

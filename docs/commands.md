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
| `orion add-project [name]` | Register a new project in `orion.toml` (**the only command that writes config**). Name defaults to the repo directory. `--recipient "Name:channel:ENV_VAR"` (repeatable), `--like <project>` to copy recipients, `--repo-path`, `--share-level`, `--collectors git,tasks,notes`, `--tasks-file`/`--notes-file`; `--print` to preview the stanza, `--yes` to skip confirmation. When `tasks` is enabled and `--tasks-file` is omitted, it defaults to `<repo>/TODO.md` and **creates a starter checklist** there (preview-gated, never overwriting); pass an explicit `--tasks-file` to keep config-only. `--grant <account>` also widens that relay account's push scope to the new project (the KI-36 forward-fix — without a grant, the project's first reports 404 off the dashboard); interactive runs with a provisioning-configured relay offer the same as an opt-in prompt, scripted runs are never prompted. |
| `orion baseline <project>` | Mark current state as already-reported **without sending** — so the next report covers only new activity (avoids dumping full history). |
| `orion install-hook <project>` | Install a git hook so a push auto-reports. `--hook pre-push` (default) or `post-commit`; `--print` to review first, `--force` to overwrite. |

## Two-way: the relay

| Command | What it does |
|---|---|
| `orion relay-serve` | Run the relay locally (ingest endpoint + read-only dashboard) on `127.0.0.1:8787`. Needs `ORION_RELAY_TOKEN` in `.env`. Blocks until Ctrl-C. |
| `orion checklist-push <project>` | Push the project's **current checklist** to the relay dashboard **without a report** (needs `checklist = true` + a `tasks` or `tracker` source + an enabled `[relay]`). Add `--watch` for near-real-time: it polls the checklist source (`tasks_file` and/or `tracker_file`) and pushes on every change until Ctrl-C (`--interval` seconds, default 3). `--watch` is single-project. |
| `orion checklist-push --all [--due]` | Push **every** checklist-enabled project (fail-soft). Add `--due` for the scheduled form: push only projects **due** under their `cadence`, skipping a due card whose content is **unchanged** since its last push (so an unattended run never re-stamps an untouched card). Relay-only, no `auto_send` gate — often the primary scheduled line (see [Scheduling](../docs/scheduling.md)). Manual `--all` without `--due` pushes unconditionally. |
| `orion checklist-push <project> --clear-due-soon-days` | Explicitly **clear** a project's stored "due soon" horizon back to the 7-day default. Project settings are **set-only** — a push that omits a value leaves it alone rather than wiping it (KI-35) — so this flag is the only way to unset one. It bypasses the unchanged-content gate, since a clear must send even when the checklist itself did not change. Repeating it is harmless. |
| `orion checklist-push <project> --clear-about` | Explicitly **clear** a project's stored **About** line (the dashboard "what this project is" band). Same set-only rule as `--clear-due-soon-days`: removing `about_file` from config never clears the stored About on its own, so this flag is the only way to unset it. Single-project, one-shot. |

A project that sets `about_file` but does **not** enable `checklist` pushes its About on its
own: `orion checklist-push <project>` sends the About line with no checklist on the wire,
leaving any stored checklist untouched. That is how a project with no task list gets an About
band. (`checklist = true` with no `tasks_file` or `tracker_file` is still an error — that
combination is a misconfiguration, not an About-only push. `--watch` needs a checklist source
to poll, so it does not apply here either.)
| `orion relay-backfill <project> --generated-at <iso>` | Push an **already-sent** report onto the relay dashboard, **relay-only (no chat)** — for reports sent before the relay was scoped in (KI-36). Body from `--body-file` or stdin; `--generated-at` is the original send time (read off the delivered message). One report per invocation; two-pass redaction still runs; preview/confirm unless `--yes`. |

> The parked native Slack bot was **removed** in the CS-O overhaul (command, module, and
> the `orion[slack-bot]` extra — git history keeps the code). Its blocker was delegation:
> a chat reply is *supervisor* speech, and the Bearer discussion path stamps role
> `developer`, so a bot must be able to write **as** a supervisor. A future revival is
> additive and starts from that delegation seam, not from the removed shell.

## Dashboard user management (`relay-user`)

Provision and manage who can log into the relay dashboard. These talk to a running relay's
`/api/users` endpoint over HTTP, authenticated with the **separate** admin token
(`admin_token_env_var` in `[relay]`, e.g. `ORION_RELAY_ADMIN_TOKEN` in `.env`) — never the
ingest token. Full auth model: [`docs/dashboard-auth.md`](dashboard-auth.md).

An **account** is the durable identity; **credentials** are N revocable things beneath it.
People sign in with a name and password; machines hold keys. The two never overlap.

### Accounts

| Command | What it does |
|---|---|
| `orion relay-user add <name>` | Provision an account and print its access key **once** (never retrievable later, only revocable). `--role` is one of `viewer` (default), `supervisor`, `member`, `admin`, `contributor`. `--key-only` prints just the raw key, for scripts (`KEY=$(orion relay-user add ci --role contributor --key-only)`). |
| `orion relay-user add <name> --role viewer --project a --project b` | A **viewer** scoped to specific projects (`--project` repeatable). A viewer with no projects sees nothing. |
| `orion relay-user add <name> --role member` | A **member**: read-only org insider. Sees every **org-visible** project with no grants at all, plus any grants on top. Can write nothing. |
| `orion relay-user add <name> --role admin` | An **admin** sees **all** projects on the dashboard. Note its *keys* are still contributor-bounded to its grants — no machine credential ever carries unrestricted push. |
| `orion relay-user add <name> --kind agent --operated-by <human>` | An **agent**: a machine acting on a person's behalf. Must be `--role contributor`; the operator must be an active human. |
| `orion relay-user list` | The roster: role, kind, operator, status, scope, last login. Shows **no** credential material. |
| `orion relay-user grant <name> --project p` | Widen an account's project scope in place. |
| `orion relay-user ungrant <name> --project p` | Narrow an account's project scope in place (grant's inverse). Idempotent: names the account does not hold are reported, not errors. For a **member**, an ungranted org-visible project stays readable (visibility is a floor) and the command says so. |
| `orion relay-user role <name> <role>` | Change a role. Bumps the session version, so live sessions are logged out. |
| `orion relay-user rename <name> <new>` | Rename an account. Already-recorded history keeps the name it was written with. |
| `orion relay-user set-operator <agent> <human>` | Repoint an agent at a different operator. Moves display grouping only; provenance is untouched. |
| `orion relay-user deactivate <name>` | Immediate cutoff: deactivates the account **and all its credentials**, and force-logs-out live sessions. Keeps the name (renamed from `revoke` in the CS-O overhaul; credential-level `key revoke` keeps its name). |
| `orion relay-user delete <name>` | Hard-delete, freeing the `UNIQUE` name to reuse. Blocked while the account still operates active agents. |

### Credentials

| Command | What it does |
|---|---|
| `orion relay-user password set <name>` | Set a human's password — prompts twice, hidden. `--generate` mints a strong one and prints it **once**. Never accepts a password as an argument. |
| `orion relay-user password unlock <name>` | Clear a login lockout without changing a password the person still knows. |
| `orion relay-user key add <name> [--label <l>]` | Attach **another** key to an account; existing keys keep working. `--label` defaults to `key`; labels are unique among an account's active keys, so name each additional one. `--key-only` prints just the raw key for scripts. |
| `orion relay-user key list <name>` | Credential ids, labels, and last-used. Never key material. |
| `orion relay-user key revoke <name> --id <n>` | Revoke **one** credential. The account and its other keys live on, and the human's session is not disturbed. |

Replacing a machine key is **`key add` → deploy → verify → `key revoke`**, so the two overlap
and no scheduled push silently starts failing. (This replaced a one-shot `rotate` command.)

## Project settings (`relay-project`)

| Command | What it does |
|---|---|
| `orion relay-project visibility <project> org` | Make a project **org-visible**: any `member` account can read it with no per-project grant. |
| `orion relay-project visibility <project> restricted` | Back to **grant-only** — the default every project is born with. |
| `orion relay-project lifecycle <project> past` | Mark a project **finished**: it groups into the dashboard's "Past projects" section and drops out of every deadline view. |
| `orion relay-project lifecycle <project> active` | Back to **still running** — the default every project is born with. Fully reversible. |

Viewers and supervisors are unaffected by visibility either way: they always see only their
explicit grants.

A project is past because someone **says** so, never because it went quiet. Quiet is not
finished. Marking one past changes nothing about its record (reports, checklist, About and
discussion all stay), only how the dashboard frames it: it stops appearing in due-soon,
at-risk, slipping and Scheduling, so a wrapped-up project can never read as overdue. The flag
lives on the relay, not in `orion.toml`, so it keeps holding after the project stops being
produced and leaves your config entirely. There is deliberately no producer path for it.

## Handy flags

- `--yes` / `-y` — skip the preview (only sends projects with `auto_send=true`). For schedulers and
  the session skill; **without it, every run previews first.**
- `--config PATH` — point at a non-default config (or set `$ORION_CONFIG` once).
- `--json` (on `discussions pull`) — raw JSON instead of the human listing (used by the session skill).

## Exit codes (for scripts/schedulers)

`0` = success **or** a routine no-op (nothing new to send, user aborted the preview). `1` = a real
failure (bad config, missing secret, send failed). `2` = usage error (e.g. neither `<project>` nor
`--all`).

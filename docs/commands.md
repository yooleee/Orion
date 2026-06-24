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
orion comments myproject       # pull supervisor replies back to your machine
```

## Daily commands

| Command | What it does |
|---|---|
| `orion report <project>` | Collect activity (git/tasks/notes/incubator), summarize, **preview**, then send. The main command. |
| `orion report --all --yes` | Report every project, non-interactive (for schedulers; only sends projects with `auto_send=true`). |
| `orion intake <project> -m "…"` | Send a hand-written update (no LLM). Omit `-m` to type it on stdin. |
| `orion comments <project>` | Pull supervisor replies from the relay (only new ones; advances your unread marker). |
| `orion comments <project> --all` | Show **all** replies without moving the unread marker (re-read). |

## Setup & inspection

| Command | What it does |
|---|---|
| `orion check` | Validate the config and send-readiness (`.env` secrets present). Read-only — sends nothing. |
| `orion projects` | List every project defined in the config. |
| `orion show <project>` | Show one project's resolved config (paths, share level, collectors, recipients). |
| `orion baseline <project>` | Mark current state as already-reported **without sending** — so the next report covers only new activity (avoids dumping full history). |
| `orion install-hook <project>` | Install a git hook so a push auto-reports. `--hook pre-push` (default) or `post-commit`; `--print` to review first, `--force` to overwrite. |

## Two-way: the relay + the Slack bot

| Command | What it does |
|---|---|
| `orion relay-serve` | Run the relay locally (ingest endpoint + read-only dashboard) on `127.0.0.1:8787`. Needs `ORION_RELAY_TOKEN` in `.env`. Blocks until Ctrl-C. |
| `orion bot` | Run the always-on **Slack bot**: a reply in a mapped channel becomes a comment on that project's latest report. Blocks until Ctrl-C. |

**`orion bot` prerequisites:** `pip install orion[slack-bot]`, an enabled `[relay]` **and** `[bot]` in
`orion.toml`, and the Slack tokens in `.env`. Full setup: [`docs/slack-bot.md`](slack-bot.md).

## Handy flags

- `--yes` / `-y` — skip the preview (only sends projects with `auto_send=true`). For schedulers and
  the session skill; **without it, every run previews first.**
- `--config PATH` — point at a non-default config (or set `$ORION_CONFIG` once).
- `--json` (on `comments`) — raw JSON instead of the human listing (used by the session skill).

## Exit codes (for scripts/schedulers)

`0` = success **or** a routine no-op (nothing new to send, user aborted the preview). `1` = a real
failure (bad config, missing secret, send failed). `2` = usage error (e.g. neither `<project>` nor
`--all`).

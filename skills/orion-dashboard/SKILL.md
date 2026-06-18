---
name: orion-dashboard
description: >-
  Start the local Orion relay (if it isn't already running) and open its
  read-only web dashboard in the browser. Use when the user asks to open, show,
  launch, or look at the Orion dashboard (or the relay). The user must have Orion
  installed with an orion.toml and an ORION_RELAY_TOKEN in the sibling .env. This
  is a LOCAL-ONLY convenience: the dashboard binds loopback (127.0.0.1).
---

# Orion — open the local dashboard

This skill launches Orion's local **relay** (the small server that stores pushed
reports and serves a read-only web dashboard) and opens it in the user's browser,
so they don't have to run `orion relay-serve` and navigate there by hand.

> **Stage note (read first).** Auto-starting a server is a **local-only**
> convenience: the dashboard currently binds `127.0.0.1`, so it is for the user's
> own machine. Once Orion gains a *hosted* dashboard, "open the dashboard" becomes
> simply opening a URL — there is nothing to start — and this skill becomes a thin
> bookmark or is retired. Don't build remote/hosting behavior into it.

Follow these steps in order.

## 1. Identify the config (and anchor the data)

You need the **path to the user's `orion.toml`** — for two reasons: the relay reads
the ingest token from the `.env` beside it, and the dashboard's data store is
anchored beside it. If you don't already know the path from this session, **ask**
for the absolute path (commonly `~/orion/orion.toml`); don't guess.

Derive two absolute paths from it:

- **Config dir** = the directory containing `orion.toml`.
- **Relay store** = `<config dir>/orion-relay.sqlite3`. Always pass this exact
  `--db` so every launch reads the **same** store (mirrors how `state_db` lives
  beside the config). Using a different `--db` per launch would make the dashboard
  look empty or inconsistent.

Defaults (only change if the user's `[relay]` config differs): host `127.0.0.1`,
port `8787`, so the dashboard URL is **`http://127.0.0.1:8787`**. The port MUST
match the port in the user's `[relay] url` (that's where their reports push to).

## 2. Is it already running?

Check whether the dashboard already answers, so you never start a second server on
the same port:

```sh
curl -s -o /dev/null --max-time 2 http://127.0.0.1:8787/
```

- **Exit 0 (it answers):** it's already up — skip to step 4 and just open the browser.
- **Non-zero (no answer):** continue to step 3 to start it.

## 3. Start the relay in the background

Start `orion relay-serve` **as a background / detached process** so it keeps
serving after this step returns — do **not** run it in the foreground (it blocks
until stopped). Use the config path and the anchored store from step 1:

```sh
orion relay-serve --config <abs path to orion.toml> --db <config dir>/orion-relay.sqlite3 --host 127.0.0.1 --port 8787
```

- If `orion` is not on PATH, use the venv's Python:
  `<orion>/.venv/bin/python -m orion relay-serve …` (Windows: `…\.venv\Scripts\python.exe`).
- Then **wait until the port answers** before opening the browser, e.g.
  `curl -s --retry 30 --retry-connrefused --retry-delay 1 --max-time 60 -o /dev/null http://127.0.0.1:8787/`.
- **If it exits immediately with `Error: Required secret 'ORION_RELAY_TOKEN' …`**,
  the token isn't set: tell the user to add `ORION_RELAY_TOKEN` to the `.env` beside
  their `orion.toml` (a long random value, e.g.
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`), then retry. Do
  not invent or echo a token value.

## 4. Open the browser

Open the dashboard in the user's default browser — `webbrowser` is in the Python
standard library and works on macOS, Linux, and Windows:

```sh
python -m webbrowser -t http://127.0.0.1:8787
```

(If `python` isn't the right name, use `python3`, or the venv Python. Native
fallbacks if needed: `open <url>` on macOS, `xdg-open <url>` on Linux, `start <url>`
on Windows.)

## 5. Report

Tell the user:

- The dashboard is open at **http://127.0.0.1:8787** (projects → history → a report).
- The relay is **running in the background** and will keep accepting pushes; to stop
  it, they can kill that process (or just ask you to stop it).
- **If the dashboard looks empty:** it only shows what has been pushed. They need
  `[relay]` **enabled** in `orion.toml` (with `url` pointing at this same
  host/port) and at least one `orion report`/`orion intake` run since enabling it.
  The push is fail-soft, so a report still delivers even if the relay is down.

## Notes

- **One server per port.** If something already answers on the port (step 2), reuse
  it — don't start a second one (it would fail to bind anyway).
- **The dashboard is read-only and loopback-only.** It is for the user's own
  machine; their supervisors still see the Discord/Slack delivery, not this, until
  Orion gains a hosted dashboard.
- **Hand-run alternative.** This skill is pure convenience: the user can always run
  `orion relay-serve` in one terminal and open `http://127.0.0.1:8787` themselves.

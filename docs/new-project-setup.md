# Setting up a new project (a hands-off recipe)

This is the shortest path from a clone to a delivered report, plus the optional
**web dashboard** (the local relay). It assumes you have **Python 3.11+** and **git**
on your PATH. Commands are shown for macOS/Linux; on native Windows use
`.venv\Scripts\` instead of `.venv/bin/` and `py -3` for Python.

## 1. Install

```bash
git clone <your-orion-remote> orion
cd orion
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e .                # 3 runtime deps: anthropic, python-dotenv, tzdata
```

> After activating the venv, `orion ...` is on your PATH for this shell. `python -m orion ...`
> is the identical, portable form used elsewhere in these docs.

## 2. Create your config and secrets

Orion reads a **config** (`orion.toml`, paths + recipients, never secrets) and a
gitignored **`.env`** (the secrets).

**Quickest path: let Orion scaffold the config.** From the `orion` directory, register the repo
you want to track:

```bash
orion add-project myapp --repo-path /absolute/path/to/your/repo \
  --recipient "Alex (supervisor):discord:ORION_DISCORD_WEBHOOK_ALEX"
```

It shows the stanza it will write, then appends it to `orion.toml` (creating the file if it does
not exist). Each `--recipient` is `"Name:channel:ENV_VAR"`, the last field naming a `.env`
variable. Run it from *inside* the target repo (passing `--config /path/to/orion/orion.toml`) and
the name and repo path are inferred; use `--like <existing-project>` to copy another project's
recipients. Full options: [Adding a project](../README.md#adding-a-project-add-project).

**Or write it by hand.** Copy the example and edit `orion.toml`:

```bash
cp orion.toml.example orion.toml
```

```toml
[projects.myapp]
repo_path = "/absolute/path/to/your/repo"
share_level = "high_level"           # safest: commit messages + diffstat, NO code diff
collectors = ["git"]                 # add "tasks"/"notes" later if you want

  [[projects.myapp.recipients]]
  name = "Alex (supervisor)"
  channel = "discord"                # or "slack"
  webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
```

The recipient names a `.env` variable (`webhook_env_var`). The webhook URL itself goes in `.env`,
so the config stays shareable.

**Either way, put the secrets in `.env`** (the config never holds them):

```bash
cp .env.example .env
```

```
ANTHROPIC_API_KEY=sk-ant-...
ORION_DISCORD_WEBHOOK_ALEX=https://discord.com/api/webhooks/XXXX/YYYY
```

> Using a **local** model instead? See [Summarizer backend](../README.md#summarizer-backend) —
> no Anthropic key needed.

## 3. Confirm you're ready

`check` validates the config and reports send-readiness — by variable **name**,
never value:

```bash
orion check
```

Fix anything it flags as `MISSING`, then:

## 4. Send your first report

```bash
orion report myapp
```

You'll see a **preview** of exactly what will be sent; confirm with `y` to deliver.
Re-running immediately reports "no new activity" — each report only covers what's
new since the last one.

Run `orion status` any time to see, across all your projects, which have new activity waiting to
report.

> **Tip — skip a giant first report.** On an existing repo, that first `report` covers the
> project's *entire* git history. To start tracking from **now** instead, run
> `orion baseline myapp` once first — it records the current state as already-reported
> **without sending anything**, so your first real report covers only new activity.

To send a hand-written update instead of collected git activity:

```bash
orion intake myapp -m "Shipped the login flow; starting on settings next."
```

That's the whole core loop. Scheduling and git-hook triggers are optional add-ons —
see [Scheduling](../README.md#scheduling) and
[Event-driven reports](../README.md#event-driven-reports-git-hooks).

---

## Optional: the web dashboard (local relay)

Orion can **also** push each report to a small local **relay** that stores it and
serves a **read-only web dashboard** — *in addition to* your Discord/Slack delivery,
which is unchanged. This is opt-in and additive: with no `[relay]` table, nothing
about Orion changes.

At this stage the relay is **loopback-only** (it binds `127.0.0.1`), so the
dashboard is for **your own machine** — your supervisors still see the Discord/Slack
delivery, not the dashboard. (A hosted/shareable dashboard is a later step.)

### 1. Add a relay token to `.env`

The push is authenticated with a shared Bearer token. Generate one and add it:

```bash
python -c "import secrets; print('ORION_RELAY_TOKEN=' + secrets.token_urlsafe(32))" >> .env
```

### 2. Enable `[relay]` in `orion.toml`

```toml
[relay]
enabled       = true
url           = "http://127.0.0.1:8787/ingest"
token_env_var = "ORION_RELAY_TOKEN"
```

`orion check` will now also confirm the relay token is set (a missing token is a
warning, not a failure — the relay is fail-soft, so your report still sends).

### 3. Run the relay, then report

In one terminal, start the relay (it serves the dashboard and the ingest endpoint;
it reads the same `ORION_RELAY_TOKEN` from `.env`):

```bash
orion relay-serve
# [relay] listening on http://127.0.0.1:8787  (db: orion-relay.sqlite3)
```

In another terminal, run a report or an intake as usual:

```bash
orion report myapp          # or: orion intake myapp -m "..."
# ...
# Also pushed to relay: http://127.0.0.1:8787/ingest
```

Open **http://127.0.0.1:8787** in your browser: the dashboard lists your projects →
each project's report history → each report's sections and metadata. A relay being
down (or a wrong token) never blocks a delivered report — the push is fail-soft and
the failure is just reported.

> `relay-serve` flags: `--host` (default `127.0.0.1`), `--port` (`8787`), `--db`
> (`orion-relay.sqlite3`), `--token-env` (`ORION_RELAY_TOKEN`). The relay's store is
> its own SQLite file, separate from Orion's state db.

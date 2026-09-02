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
collectors = ["git"]                 # add "tasks"/"notes"/"incubator"/"tracker" later if you want

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

This section sets up a **loopback** relay (it binds `127.0.0.1`), which is the right
starting point: the dashboard is for your own machine while you try it out. Deploying it
so supervisors can reach a real URL is a separate, additive step — see
[`deployment.md`](deployment.md).

### 1. Add the relay's secrets to `.env`

The relay authenticates every push with a **per-producer contributor key** it mints itself, so
it needs two secrets of its own before it can provision one: the pepper that makes stored keys
verifiable, and the admin token that gates provisioning (which in turn needs the session signing
key). Generate each as its own random value:

```bash
python -c "import secrets; print('ORION_RELAY_USER_PEPPER=' + secrets.token_urlsafe(32))" >> .env
python -c "import secrets; print('ORION_RELAY_SESSION_KEY=' + secrets.token_urlsafe(32))" >> .env
python -c "import secrets; print('ORION_RELAY_ADMIN_TOKEN=' + secrets.token_urlsafe(32))" >> .env
```

Your own machine's key is provisioned in step 4 below and lands in `.env` as `ORION_RELAY_TOKEN`.

> **Every** producer — each machine, person, or agent that pushes into a project — gets its own
> push-only key under this same `ORION_RELAY_TOKEN` variable on that machine, no `orion.toml`
> change needed. Reports then show who pushed them; there is no shared token to fall back on.
>
> ```bash
> orion relay-user add mac --role contributor --project my-app          # a machine
> orion relay-user key add mac --label wsl2                             # a SECOND machine, same identity
> orion add-project other-app --grant mac                               # register a LATER project and
>                                                                       # widen mac's scope in one step

> orion relay-user add claude-mac --role contributor --kind agent \
>     --operated-by yoo --project my-app                                # an agent acting for you
> ```
>
> For a **person** who needs to view the dashboard, provision an interactive account instead and
> give them a password — they never handle key material:
>
> ```bash
> orion relay-user add supervisor-a --role viewer --project my-app
> orion relay-user password set supervisor-a
> ```
>
> See [`dashboard-auth.md`](dashboard-auth.md).

### 2. Enable `[relay]` in `orion.toml`

```toml
[relay]
enabled             = true
url                 = "http://127.0.0.1:8787/ingest"
token_env_var       = "ORION_RELAY_TOKEN"
admin_token_env_var = "ORION_RELAY_ADMIN_TOKEN"
```

`orion check` will now also confirm the relay key is set (a missing key is a
warning, not a failure — the relay is fail-soft, so your report still sends).

### 3. Run the relay

In one terminal, start the relay (it serves the dashboard and the ingest endpoint; it
reads its own secrets from the same `.env`):

```bash
orion relay-serve
# [relay] listening on http://127.0.0.1:8787  (db: orion-relay.sqlite3)
```

### 4. Provision your machine's key, then report

Mint a push-only key for this machine, scoped to your project, and store it in `.env` under the
name step 2 configured (`--key-only` prints just the key, so it can be captured directly):

```bash
echo "ORION_RELAY_TOKEN=$(orion relay-user add mac --role contributor --project myapp --key-only)" >> .env
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
> (`orion-relay.sqlite3`). The relay's store is its own SQLite file, separate from Orion's
> state db. Its secrets are read under fixed names from `.env` (see step 1).

## Optional: bringing a finished project onto the dashboard

A project that wrapped up before you started using Orion can still go on the dashboard as
part of the record. There is no special machinery for this. You use the paths that already
exist, then declare the result finished.

1. **Add it to your config** with `orion add-project <name>`, pointing at the repo (or at
   nothing much, if the work lives elsewhere).
2. **Get its content onto the relay.** Whatever it has is what it gets: a checklist push
   (`orion checklist-push <name>`) carries the checklist and the About line, and
   `orion intake <name> --relay-only --generated-at <iso> --body-file <file>` carries a
   historical report body at the date you give it (the mode formerly named
   `relay-backfill`). Run it once per report you want on the timeline.

   A finished project often has no task list left. That is fine: set `about_file` and leave
   `checklist` off, and `orion checklist-push <name>` sends just the About line.
3. **Mark it finished:**

   ```bash
   orion relay-project lifecycle <name> past
   ```

The project now sits in the dashboard's collapsed "Past projects" section, badged, with its
full record intact and none of its old deadlines counted against you. If you got the call
wrong, `orion relay-project lifecycle <name> active` puts it straight back.

Two things worth knowing. The lifecycle flag is admin-only and lives on the relay, so it
keeps holding even after you remove the project from `orion.toml` entirely. And a project is
past because you said so, never because it went quiet — Orion will not infer this, since a
paused project and a finished one are different things.

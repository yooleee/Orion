<!-- =========================================================================
docs/deployment.md
---------------------------------------------------------------------------
Responsible for: The runbook for deploying the Orion relay BEYOND loopback — the
                 "C1 second slice" hosted half (ingest endpoint + read-only
                 dashboard), on Path B (self-host). Host-agnostic: a Dockerfile
                 that runs anywhere, plus a Caddy reverse-proxy recipe, plus the
                 secrets / TLS / persistence rules and per-target notes.
Role in project: Local collection stays on your machine; only the relay moves
                 hosted, along the portable report/intake blob seam. Hosting is
                 settled as Path B (see plans/orion-plan.md "Hosting decision").
Companion: the local-relay basics (running it on loopback) live in
           docs/new-project-setup.md and the README's "Web dashboard" section.
========================================================================= -->

# Deploying the Orion relay (beyond loopback)

The relay is the **hosted half** of Orion's dashboard: it receives pushed reports and
serves a read-only web view. Your machine still does all the collecting/redacting and
keeps pushing to Discord/Slack as before — deploying the relay just gives a *supervisor*
a real URL to look at. You point your local `[relay].url` at the deployed HTTPS endpoint;
nothing else in the local pipeline changes.

## Security model (what protects what)

- **Ingest (`POST /ingest`)** — authenticated by a shared **Bearer token**
  (`ORION_RELAY_TOKEN`), constant-time compared. This is the push credential your local
  side sends.
- **Dashboard (GET routes + commenting)** — authenticated by a **per-user login session**.
  Each person signs in at `/login` with their own name and password and receives a signed,
  `HttpOnly` session cookie (`Secure` when hosted). The cookie carries only an id, a
  version, and an expiry. The viewer's role and project scope are re-read from the
  database on every request, and a revoked user is rejected on their next request. HTTP
  Basic auth (the earlier C2 model) is gone.
- **Provisioning** — `relay-user add`/`list`/`revoke` talk to the relay's admin API,
  authenticated by a **separate** admin token (`ORION_RELAY_ADMIN_TOKEN`). The admin token
  is independent of the ingest token on purpose: whoever can push reports must not be able
  to create users.
- **Bootstrap admin** — while no users exist yet, `ORION_RELAY_VIEW_TOKEN` doubles as a
  one-key admin login so a fresh deploy is not locked out before it provisions anyone. Once
  any user is provisioned it stops working, unless you pass `--allow-legacy-admin`.
- **The fail-closed guard** — the relay **refuses to start** if you bind a non-loopback
  host without `ORION_RELAY_VIEW_TOKEN`, so you cannot accidentally serve an open dashboard.
  It also refuses to start an access-gated dashboard when the session secrets are missing.
- **TLS is mandatory** and is terminated by your platform or a reverse proxy *in front
  of* the relay — **never** expose plain HTTP to the internet (passwords and session
  cookies would travel in clear text).

### Generate the secrets

```
python -c "import secrets; print(secrets.token_urlsafe(32))"   # run once per secret
```

The relay's `.env` needs these, each its own independent random value:

| Variable                  | Protects                                    | Required when                       |
| ------------------------- | ------------------------------------------- | ----------------------------------- |
| `ORION_RELAY_TOKEN`       | Ingest (the report push)                    | Always                              |
| `ORION_RELAY_VIEW_TOKEN`  | The bootstrap-admin login + the bind guard  | Any non-loopback bind               |
| `ORION_RELAY_SESSION_KEY` | Signing session cookies                     | Whenever the dashboard is gated     |
| `ORION_RELAY_USER_PEPPER` | Hashing stored **key** verifiers (not passwords) | Whenever the dashboard is gated     |
| `ORION_RELAY_ADMIN_TOKEN` | The provisioning API (`relay-user`)         | To create/manage users              |

The ingest token must match what your **local** `[relay].token_env_var` resolves to. The
admin token is named by your local `[relay].admin_token_env_var`. Keep all five independent
(do not reuse one value for another) — they bound separate blast radii and rotate separately.

**Also set `ORION_RELAY_PUBLIC_ORIGIN` when you deploy behind a TLS proxy (Fly, Caddy, any
reverse proxy).** This is NOT a secret. It is the canonical public URL the dashboard is
reached at, for example `https://project-orion.fly.dev`. The relay uses it for the
comment form's CSRF origin check: with it set, the check compares the browser's Origin (or
Referer) against this exact value, instead of falling back to the request's `Host` header,
which a proxy can rewrite. Because it is not a secret it can go straight in `fly.toml` under
`[env]` (it already is in this repo's `fly.toml`), or be passed with `-e` on `docker run`.
If you leave it unset, comments still work on a direct loopback bind, but a proxied deploy is
more robust with it set. (The comment CSRF guard also accepts a same-origin `Referer` when a
browser omits `Origin`, so omitting this no longer breaks comments on browsers like Safari.)

### Provision the first users

After the relay is up and reachable, create accounts from your local machine (the
`relay-user` commands read your `[relay].url` and `admin_token_env_var`):

```bash
# People — provision, then give them a password. They never handle key material.
orion relay-user add supervisor-a --role viewer --project my-app
orion relay-user password set supervisor-a                     # prompts twice, hidden
orion relay-user add teammate-b --role member                  # reads every org-visible project
orion relay-user password set teammate-b --generate            # relay mints one, printed ONCE

# Machines — provision, then hand the printed key to that machine's .env.
orion relay-user add mac --role contributor --project my-app
orion relay-user key add mac --label wsl2                      # a SECOND key on the same account
orion relay-user key list mac                                  # ids + labels (never key material)
orion relay-user key revoke mac --id 2                         # kill one credential only

# Agents — a machine acting on a person's behalf.
orion relay-user add claude-mac --role contributor --kind agent --operated-by yoo --project my-app

# Lifecycle.
orion relay-user list                                          # roster: role, kind, operator, scope
orion relay-user grant mac --project other-app                 # widen an existing account's scope
orion relay-user role supervisor-a supervisor                  # change a role (logs out live sessions)
orion relay-user rename mac mac-mini                           # rename (history keeps the old name)
orion relay-user revoke supervisor-a                           # cutoff (keeps the name)
orion relay-user delete supervisor-a                           # hard-delete (frees the name)

# Project visibility — who inside the org may read a project.
orion relay-project visibility my-app org                      # any member can read it
orion relay-project visibility my-app restricted               # back to grant-only (the default)
```

**People get passwords; machines get keys.** A printed key goes to a machine's `.env`, never to
a person. Both a key and a generated password are shown exactly once and cannot be retrieved
later (only a verifier is stored).

Roles: a `viewer` sees only the projects you grant; a `supervisor` is a scoped viewer that may
also post to a discussion thread; a `member` is a read-only org insider that sees every
**org-visible** project with no grants at all; an `admin` sees everything on the dashboard and
can provision. `grant` widens scope; `revoke` cuts off but keeps the name; `delete` frees the
name to reuse (past reports/replies keep their recorded author).

> **A `member` you just provisioned will see an empty dashboard.** Every project is
> `restricted` by default, so nothing is org-visible until you flip it. That is deliberate —
> opening a project up is an explicit act — but it is the first thing that looks broken. Run
> `orion relay-project visibility <project> org` for each project the org should share.

> **Replacing a machine key is `key add` → deploy → verify → `key revoke`.** Add the new
> credential, install it, confirm a push works, and only then revoke the old one. The two keys
> overlap, so no scheduled push starts silently 401ing and you cannot strand yourself if the
> new key is lost in transit. For a compromise, reverse it: revoke immediately, add when ready.
> (This replaced a one-shot `rotate` command, which had both of those failure modes.)

Full detail: [`dashboard-auth.md`](dashboard-auth.md).

**Multi-producer push (C3 Increment 2).** When more than one machine or person pushes into the
same project, provision each producer a `contributor` (push-only) key and put it in that
machine's own `.env` as `ORION_RELAY_TOKEN` — no `orion.toml` change. Reports then show who
pushed them, and each producer keeps its own checklist. The **legacy shared ingest token keeps
working (anonymously)** until you retire it by starting the relay with
`orion relay-serve --disable-legacy-ingest`; from then on only named per-user keys can push.
This cutover is deliberate — a machine credential should not silently expire — so flip the flag
only once every producer has its own key (each legacy use logs a line so you can watch it go
quiet). Full detail: [`dashboard-auth.md`](dashboard-auth.md).

## The relay dependency (password login)

The relay installs the **`relay` extra**, which adds `argon2-cffi` for password hashing:

```bash
pip install '.[relay]'     # the Dockerfile already does this
```

It is relay-only by construction — hashing happens exclusively on the relay, so the local
producer install stays at three runtime dependencies and never imports it. Prebuilt manylinux
wheels cover the Docker image, so no compiler is needed.

If a relay starts **without** it and someone tries a password login, the login fails closed with
a clear operator error in the logs rather than degrading to a weaker scheme. Key login is
unaffected, so a relay missing the extra is still usable — it just cannot do passwords.

**Memory.** Argon2id is memory-hard on purpose (~19 MiB per verification at the configured
parameters). Concurrent verifications are capped so peak hashing memory stays around 76 MiB,
which fits a 512 MB VM alongside the Python process. If you shrink the VM below 512 MB, check
that headroom before assuming logins still work under load.

## Persistence (do not skip)

The relay stores reports in a SQLite file. On ephemeral hosts (containers, Fly, Render's
default disk) that file is **wiped on every restart/redeploy** unless it lives on a
**mounted volume**. The Dockerfile writes it to `/data/orion-relay.sqlite3` and declares
`/data` a volume — mount real storage there.

### WAL-safe backups (before any schema surgery)

The store runs in **WAL mode**, so recently-committed rows can still live in the sidecar
`-wal` file, not yet folded into the main `.sqlite3`. A plain `cp` of the `.sqlite3` alone can
therefore miss committed data — **do not back up a live relay with `cp`.** Take a
*consistent* copy instead:

- **From inside the container / on the volume** — use SQLite's online backup, which is
  consistent under WAL:
  ```bash
  # via the sqlite3 CLI (atomic .backup):
  sqlite3 /data/orion-relay.sqlite3 ".backup '/data/orion-relay.backup.sqlite3'"
  ```
- **Pull a copy to your machine** — the preferred form is below (it does not write to the
  volume and does not modify the live database at all).

**Always take one before a destructive maintenance step** (a one-time migration or table
drop). The repo's maintenance tools bake this in: both `relay.migrate_comments` and
`relay.drop_retired_tables` default to a **dry-run**, and the drop tool takes its **own**
`sqlite3.Connection.backup()` copy *before* it drops anything (`--drop` writes
`<db>.before-skills-drop.bak` and refuses to overwrite an existing one). Keep an operator-side
copy as well — belt and braces.

### Pull a backup to your machine

Two commands. The first makes a consistent copy inside the container, the second brings it
down with a dated name:

```bash
fly ssh console -a project-orion -C "python3 -c \"import sqlite3; s=sqlite3.connect('file:/data/orion-relay.sqlite3?mode=ro', uri=True); d=sqlite3.connect('/tmp/orion-pull.sqlite3'); s.backup(d); d.close(); s.close(); print('BACKUP_OK')\""
fly sftp get /tmp/orion-pull.sqlite3 ~/orion-backups/orion-relay.$(date +%Y%m%d).bak -a project-orion
fly ssh console -a project-orion -C "rm -f /tmp/orion-pull.sqlite3"
```

Three deliberate choices here, each avoiding a way the obvious version is worse:

- **`mode=ro` on the source.** SQLite's online backup API reads a consistent snapshot
  including the WAL, so opening the live database **read-only** is enough. That makes the
  pull provably incapable of altering production. The older advice of running
  `PRAGMA wal_checkpoint(TRUNCATE)` first is a *write* to the live database, taken purely to
  make a plain file copy safe. With the backup API you do not need it, so do not do it.
- **`/tmp`, not `/data`.** `/tmp` is the container's ephemeral layer; `/data` is the mounted
  volume. Writing the temp copy to the volume would consume the same 1 GB the store lives on
  and leave a stale duplicate of every project's data sitting in production.
- **A dated filename in `~/orion-backups/`.** Never a session scratch directory. The one time
  a set of backups was written somewhere temporary, they did not survive the session, which
  is the failure this whole section exists to prevent.

**`fly ssh console` does NOT wake a stopped machine.** It fails with *"app <name> has no
started VMs."* Since this app scales to zero, that is its normal state, so send one request
first to start it — `curl -sf https://project-orion.fly.dev/healthz > /dev/null` is the
cheapest wake (no auth, no DB read), and `curl` returns only once the cold start has finished.
The machine stops again on its own afterwards. `relay-backup.sh` does this for you; the manual
commands above assume you have woken it.

### The two backup layers, and the actual RPO

**Layer 1 — Fly volume snapshots (the platform's, free, already on).** Fly snapshots the
volume daily. Verified 2026-07-29 on `orion_data`: five snapshots present, newest 20 hours
old, **retention 5 days**.

```bash
fly volumes list -a project-orion                    # find the volume id
fly volumes snapshots list <volume-id>               # dates + RETENTION DAYS
```

**Layer 2 — your own weekly pull (the schedule below).** This is the layer that exists
because layer 1 has a 5-day horizon and lives in the same account as the thing it protects.

So, stated plainly:

| Failure | Worst-case data loss |
| --- | --- |
| Volume lost, noticed within 5 days | **≤ 24 hours** (restore the newest snapshot) |
| Noticed after 5 days, or the Fly account itself is lost | **≤ 7 days** (your newest weekly pull) |

If a 7-day outer window is too loose for you, change `Weekday` in the schedule below to run
daily — it is a one-line edit. Retention needs no pruning policy: a backup is well under a
megabyte, so a weekly pull kept forever costs on the order of 30 MB a year. Revisit that if
the store ever grows by orders of magnitude.

### Restore (tested 2026-07-29, against real production data)

Restoring is a file copy. There is no import step, no schema step, and nothing to
re-provision, because the backup *is* the database.

```bash
# 1. Take the relay down (or deploy over it afterwards). Then put the file in place.
cp ~/orion-backups/orion-relay.20260729.bak ./orion-relay.sqlite3

# 2. Prove the copy before trusting it.
python3 -c "import sqlite3; print(sqlite3.connect('file:orion-relay.sqlite3?mode=ro', uri=True).execute('PRAGMA integrity_check').fetchone()[0])"

# 3. Serve it locally and read it back before pushing it anywhere near production.
orion relay-serve --db ./orion-relay.sqlite3 --web-dir web/dist --port 8793
curl -s http://127.0.0.1:8793/healthz

# 4. To put it back on Fly, upload it to the volume and restart:
fly sftp shell -a project-orion      # then: put orion-relay.sqlite3 /data/orion-relay.sqlite3
fly apps restart project-orion
```

Alternatively, restore layer 1 directly: `fly volumes snapshots list <volume-id>`, then
create a new volume from a snapshot and attach it. That is the faster path for a genuine
volume failure, and the weekly pull is the fallback for everything the 5-day window misses.

**What "tested" means here.** This procedure was walked end to end on 2026-07-29 against a
real production pull (not a synthetic fixture): the copy passed `integrity_check`, a relay
started against it, a login succeeded, the portfolio read back all 5 projects/trackers with
their lifecycle states intact, and an individual report fetched with its per-project number
and prev/next navigation working. Step 4 (writing back to the volume) is the one step **not**
exercised, because doing so would have meant overwriting live production data to test a
backup. It is the documented inverse of the `fly sftp get` above.

### Schedule the weekly pull (macOS / launchd)

The pull logic lives in **`relay-backup.sh`** at the repo root, so the scheduler entry stays a
one-liner and the logic is version-controlled and reviewable. Create
`~/Library/LaunchAgents/com.orion.relay-backup.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.orion.relay-backup</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>/abs/path/to/orion/relay-backup.sh</string>
  </array>
  <!-- launchd jobs get a minimal PATH, so `fly` is not on it. This is the classic reason a
       scheduled job works in a terminal and not here. Passed as an env var rather than
       hardcoded in the script, so the script stays portable. -->
  <key>EnvironmentVariables</key>
  <dict>
    <key>FLY_BIN</key>
    <string>/opt/homebrew/bin/fly</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key><integer>0</integer>
    <key>Hour</key><integer>9</integer>
    <key>Minute</key><integer>30</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>/abs/path/to/orion/orion-backup-launchd.log</string>
  <key>StandardErrorPath</key>
  <string>/abs/path/to/orion/orion-backup-launchd.log</string>
</dict>
</plist>
```

```bash
mkdir -p ~/orion-backups
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.orion.relay-backup.plist
launchctl kickstart -p gui/$(id -u)/com.orion.relay-backup   # run it once now, don't wait a week
launchctl print gui/$(id -u)/com.orion.relay-backup          # status; check `last exit code`
# launchctl bootout gui/$(id -u)/com.orion.relay-backup      # to unload
```

**`launchctl bootstrap` needs the domain target** (`gui/$(id -u)`) before the path. Given the
path alone it answers "Unrecognized target specifier."

Linux and Windows: point their native scheduler at the same script — see
[`docs/scheduling.md`](scheduling.md) for the `cron` / systemd-timer / Task Scheduler idioms.
The script is POSIX `sh` and takes its settings from the environment
(`ORION_FLY_APP`, `ORION_HEALTH_URL`, `ORION_BACKUP_DIR`, `FLY_BIN`).

**Two things the script handles that a naive version gets wrong** — both found by actually
running it, not by reading it:

1. **It wakes the machine first.** `fly ssh console` does **not** auto-start a stopped machine;
   it fails outright with *"app <name> has no started VMs."* Because this app runs
   `min_machines_running = 0`, the machine is stopped most of the time, so an unattended job
   without a wake step fails on nearly every run. The script curls `/healthz` first — the
   cheapest wake target, and `curl` returns only once the cold start finishes.
2. **It pulls to a `.part` file and then moves it into place.** `fly sftp get` **refuses to
   overwrite an existing file** ("doesn't override existing files for safety"), so a same-day
   manual pull, or any retry, would otherwise make the run fail. More importantly, a partial
   transfer must never clobber a known-good backup — the `mv` runs only after a successful
   pull, so the previous file survives any failure.

It also re-opens each finished backup and runs `integrity_check` before reporting success,
because a backup that has never been read back is a guess.

**A `fly` token that does not expire** with your interactive login is the remaining
prerequisite for a truly unattended run (`fly tokens create deploy`, then add `FLY_API_TOKEN`
to the plist's `EnvironmentVariables` if a scheduled run starts failing on auth).

**Verified working 2026-07-29:** bootstrapped, `kickstart`ed, `last exit code = 0`, and the
dated file in `~/orion-backups/` was rewritten by launchd itself with
`integrity_check ok, 74 reports`. A schedule you have never seen succeed is not a backup — run
the `kickstart` line and confirm the same.

---

## Test the image locally first (smoke test)

Build and exercise the container on your own machine before deploying. This verifies the image,
the dashboard login, and the **fail-closed guard** end to end over plain HTTP on loopback —
*before* TLS and hosting are involved. (It does **not** exercise TLS or the reverse-proxy
topology — for that, and the `--require-view-auth` switch it needs, see Option B.)

```bash
# 1) Throwaway secrets, kept in your shell for the commands below. The dashboard needs
#    the session signing key + user pepper as well as the view token (the bootstrap admin).
export ORION_RELAY_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
export ORION_RELAY_VIEW_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
export ORION_RELAY_SESSION_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
export ORION_RELAY_USER_PEPPER=$(python -c "import secrets; print(secrets.token_urlsafe(32))")

# 2) Build.
docker build -t orion-relay .

# 3) Negative test — the fail-closed guard: NO view secret => must refuse to start (exit 1).
docker run --rm -e ORION_RELAY_TOKEN=anything orion-relay
#   expect: "Error: refusing to bind non-loopback host '0.0.0.0' ..."  and exit code 1

# 4) Real run (the relay secrets + a volume + the port).
docker run -d --name orion-relay-test -p 8787:8787 -v orion-test-data:/data \
  -e ORION_RELAY_TOKEN="$ORION_RELAY_TOKEN" \
  -e ORION_RELAY_VIEW_TOKEN="$ORION_RELAY_VIEW_TOKEN" \
  -e ORION_RELAY_SESSION_KEY="$ORION_RELAY_SESSION_KEY" \
  -e ORION_RELAY_USER_PEPPER="$ORION_RELAY_USER_PEPPER" \
  orion-relay
docker logs orion-relay-test            # expect: "dashboard: login required"

# 5) Dashboard gate: with no session, the dashboard redirects to /login (303).
curl -s -o /dev/null -w "no-session:   HTTP %{http_code}\n" http://localhost:8787/
#   expect: 303

# 6) Log in as the bootstrap admin — the view token works while no users exist yet — and
#    save the session cookie; then the dashboard serves (200).
curl -s -c cookies.txt -o /dev/null --data-urlencode "key=$ORION_RELAY_VIEW_TOKEN" \
  http://localhost:8787/login
curl -s -o /dev/null -w "with-session: HTTP %{http_code}\n" -b cookies.txt http://localhost:8787/
#   expect: 200

# 7) Ingest a report with the Bearer token => 201 {"id": 1}.
curl -s -w "  HTTP %{http_code}\n" -X POST http://localhost:8787/ingest \
  -H "Authorization: Bearer $ORION_RELAY_TOKEN" -H "Content-Type: application/json" \
  -d '{"project":"smoke-test","share_level":"high_level","lane":"raw","body":"Hello.","generated_at":"2026-06-18T12:00:00+00:00","orion_version":"0.0.0","participants":["Alex"],"sections":[["Code activity","Verified the image."]]}'

# 8) Confirm it renders, and survives a restart (volume persistence). Re-login if the
#    saved session expired while the container was down.
docker restart orion-relay-test
curl -s -b cookies.txt http://localhost:8787/ | grep smoke-test

# 9) Cleanup.
docker rm -f orion-relay-test && docker volume rm orion-test-data && rm -f cookies.txt
```

---

## Option A — Docker, platform terminates TLS (Fly, Render, any Docker host)

Here the relay binds `0.0.0.0` inside the container and the platform gives you HTTPS. The
fail-closed guard **enforces** the view secret automatically (non-loopback bind).

```
docker build -t orion-relay --build-arg ORION_BUILD_SHA=$(git describe --always --dirty) .
docker run -d --name orion-relay \
  -p 8787:8787 \
  -v orion-data:/data \
  -e ORION_RELAY_TOKEN=<ingest-token> \
  -e ORION_RELAY_VIEW_TOKEN=<view-secret> \
  -e ORION_RELAY_SESSION_KEY=<session-key> \
  -e ORION_RELAY_USER_PEPPER=<user-pepper> \
  -e ORION_RELAY_ADMIN_TOKEN=<admin-token> \
  orion-relay
```

The session key + user pepper are required for the login the gated dashboard runs; the admin
token enables `relay-user` provisioning (omit it only if you will never create users). See
"Generate the secrets" above.

**Fly.io** (warm ~$2/mo, or scale-to-zero for near-free): the repo ships a **`fly.toml`** with the
volume mount, `internal_port = 8787`, and `force_https` already set — edit its `app` to a
globally-unique name, then:
```
fly launch --no-deploy        # uses the committed fly.toml; decline the Postgres/Redis/Tigris add-ons
fly volumes create orion_data --size 1 --region <your-region>   # ONE volume — see Gotchas below
fly secrets set ORION_RELAY_TOKEN=… ORION_RELAY_VIEW_TOKEN=… \
  ORION_RELAY_SESSION_KEY=… ORION_RELAY_USER_PEPPER=… ORION_RELAY_ADMIN_TOKEN=…  # private terminal
fly deploy --build-arg ORION_BUILD_SHA=$(git describe --always --dirty)
```
Fly terminates TLS; the dashboard is at `https://<app>.fly.dev`.

### Deploy convention: stamp the build, tag the deploy

Two habits, both cheap, that together answer "what code is running?" after the fact.

**Pass the build stamp on every deploy.** `--build-arg ORION_BUILD_SHA=$(git describe --always --dirty)`
bakes the stamp into the image, and the relay serves it at `GET /healthz` and logs it at startup.
The `--dirty` suffix is not decoration: `fly deploy` ships your **working tree**, not a commit, so
on a dirty tree a bare SHA would name code that never shipped. Omitting the flag is safe but
lossy, `/healthz` then reports `"version": "unknown"`. That is deliberate. An absent answer is
recoverable, a confidently wrong one is not.

**Tag the release.** After a deploy that you would ever want to identify again, tag it:
`git tag v27 && git push --tags`, incrementing from the last one. Fly's own release numbers
(`fly releases`) are the platform's counter, the tag is yours, and the two line up through the
stamp `/healthz` reports.

### Health check

`GET /healthz` is unauthenticated and returns `{"status": "ok", "version": "<build stamp>"}`. It
touches neither the auth spine nor SQLite, so it answers even when the store is the broken part.
That makes it a **liveness** check, not a readiness one, which is the right shape for the failure
this exists to catch (a wedged process still accepting traffic). It carries no project names, no
counts, and no account facts, so it is safe to leave open.

`fly.toml` ships an `[[http_service.checks]]` block pointing at it.

**Checks and scale-to-zero (verified, because Fly's docs do not say).** None of Fly's
configuration reference, autostop/autostart page, or health-checks reference states whether a
check runs against a stopped machine, can wake one, or counts as the traffic that defeats
autostop. Measured on release v27, 2026-07-29: with the checks block deployed and the app left
idle for about 26 minutes, the machine **returned to `stopped` on its own**. Checks do not defeat
scale-to-zero.

If you want to re-confirm it after a Fly platform change, the method is: deploy, send one request
to wake the machine, then leave it alone and poll `fly status` (a control-plane call, so it does
not itself count as traffic and will not reset the idle timer).

> **Do not wire alerting to this check on a scaled-to-zero app.** On a stopped machine Fly
> reports the check as `warning`, with output "the machine hasn't started" — it neither fails nor
> wakes the machine. So idle-and-healthy is indistinguishable from genuinely broken if you are
> only watching check state. What the check *does* tell you is whether a **running** machine is
> actually serving, which is the wedged-process case it was added for. A real uptime alarm would
> need to probe `/healthz` from outside and accept the cold-start latency as a normal response.

### Reading the logs

The relay logs to stderr, which is what `fly logs` streams. Lines look like:

```
2026-07-29 14:51:35 [relay] INFO listening on http://0.0.0.0:8787  (db: /data/orion-relay.sqlite3; dashboard: login required; frontend: SPA from /app/web/dist; build: cd30bd2)
2026-07-29 14:51:39 [relay] INFO 127.0.0.1 "GET /healthz HTTP/1.1" 200 -
2026-07-29 14:51:39 [relay] WARNING 127.0.0.1 "POST /ingest HTTP/1.1" 401 -
```

INFO covers the access log (one line per request, with the status) plus ingest, checklist, and
disciplines writes. WARNING and above covers request failures, use of the retired shared ingest
token, and password-hashing problems. Set `ORION_RELAY_LOG_LEVEL` (for example `WARNING` to quiet
the access log, `DEBUG` to widen it) if the default is wrong for your host. An unrecognized value
falls back to INFO rather than silencing the log.

**Render**: a Docker service; add a **Disk** mounted at `/data`; set the relay env vars (the table above); Render
provides HTTPS at `https://<service>.onrender.com`. (Free instances sleep — fine for
checking in, rough for a live demo.)

---

## Option B — Reverse proxy you run (VPS, Raspberry Pi, Jetson Orin Nano)

Here **you** terminate TLS with [Caddy](https://caddyserver.com) (auto Let's Encrypt). The
relay binds **loopback** (`127.0.0.1:8787`); only Caddy reaches it.

> ### ⚠ Behind a proxy, force read-auth on
> The fail-closed guard keys off the **bind host**. Behind a proxy the relay binds
> *loopback*, so the host-based guard alone can't tell the dashboard is publicly
> reachable. **Pass `--require-view-auth`** in this topology: the guard then demands the
> view secret even on a loopback bind, so a forgotten secret **fails closed** (the relay
> won't start) instead of serving a world-readable dashboard. (Closes **KI-18**.)
> Optionally add Caddy `basic_auth` as a second layer — see `Caddyfile.example`.

1. Run the relay on loopback **with `--require-view-auth`** (Docker with
   `--host 127.0.0.1 --require-view-auth`, or via `orion relay-serve` under a systemd
   unit). Set the relay secrets in its environment (the table in "Security model": ingest +
   view + session key + pepper, plus the admin token if you will provision users).
2. Put `Caddyfile.example` → `Caddyfile`, set your domain, point the domain's DNS at the
   box, and `caddy run`. Caddy fetches a cert and reverse-proxies `127.0.0.1:8787`.

A **Pi / Jetson Orin Nano on your own network** is the purest own-your-data option (the
data never leaves hardware you control); it needs a domain + dynamic DNS (or a tunnel) so
Let's Encrypt can validate and supervisors can reach it.

---

## Common gotchas (learned from the first deploy)

- **`token_env_var` is a variable NAME, not the token.** In `orion.toml`, set
  `token_env_var = "ORION_RELAY_TOKEN"` — the *name* of a `.env` variable. The secret VALUE goes in
  `.env` (and on Fly via `fly secrets`), never in `orion.toml`. Pasting the value here used to fail
  with a confusing "secret '<value>' is not set" (and echoed the secret); config validation now
  rejects it at load with a clear message. Same rule for recipients' `webhook_env_var`.
- **Say *yes* to a single volume.** `fly volumes create` warns that one volume isn't highly
  available and suggests two or more — but the relay is a single-writer SQLite service, so **two
  volumes = two divergent databases** (split-brain). One volume is the correct shape; the accepted
  tradeoff is brief downtime if that host has a rare outage (the data persists on the volume).
- **Scale-to-zero means a cold start.** With `min_machines_running = 0`, the first request after idle
  wakes the machine (a few seconds). Local Orion's relay push is fail-soft, so a cold-start timeout
  just skips that push (the report still delivers); re-run, or set `min_machines_running = 1` to stay
  warm for a live demo.

## Point local Orion at the deployed relay

In the project's `orion.toml`:

```toml
[relay]
enabled       = true
url           = "https://relay.example.com/ingest"   # your deployed HTTPS endpoint
token_env_var = "ORION_RELAY_TOKEN"
```

Set `ORION_RELAY_TOKEN` (the **same** ingest value) in your **local** `.env`. The view
secret lives only on the **server**, never in your project config.

## Verify end to end

0. `curl https://relay.example.com/healthz` — the relay is up, and the `version` it reports is
   the build you meant to deploy. No credential needed.
1. `orion check <project>` — confirms the local ingest token is set.
2. `orion report <project>` (or `orion intake …`) — pushes a real report.
3. Open `https://relay.example.com` — the browser prompts for the dashboard login; enter
   any username and the view secret; confirm the report appears, served over HTTPS.

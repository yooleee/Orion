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
  Each viewer signs in at `/login` with their own access key and receives a signed,
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
  of* the relay — **never** expose plain HTTP to the internet (login keys and session
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
| `ORION_RELAY_USER_PEPPER` | Hashing stored login-key verifiers          | Whenever the dashboard is gated     |
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
orion relay-user add alex --role viewer --project my-app   # prints a one-time access key
orion relay-user list
orion relay-user revoke alex
```

Give each person their printed key over a secure channel. The key is shown once and cannot
be retrieved later (only a verifier is stored). A `viewer` sees only the projects you grant;
an `admin` sees everything and can provision.

## Persistence (do not skip)

The relay stores reports in a SQLite file. On ephemeral hosts (containers, Fly, Render's
default disk) that file is **wiped on every restart/redeploy** unless it lives on a
**mounted volume**. The Dockerfile writes it to `/data/orion-relay.sqlite3` and declares
`/data` a volume — mount real storage there.

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
docker build -t orion-relay .
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
fly deploy
```
Fly terminates TLS; the dashboard is at `https://<app>.fly.dev`.

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

1. `orion check <project>` — confirms the local ingest token is set.
2. `orion report <project>` (or `orion intake …`) — pushes a real report.
3. Open `https://relay.example.com` — the browser prompts for the dashboard login; enter
   any username and the view secret; confirm the report appears, served over HTTPS.

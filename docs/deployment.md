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
  (`ORION_RELAY_TOKEN`), constant-time compared.
- **Dashboard (GET routes)** — authenticated by **HTTP Basic auth**
  (`ORION_RELAY_VIEW_TOKEN`, any username + this as the password).
- **The fail-closed guard** — the relay **refuses to start** if you bind a non-loopback
  host without a view secret, so you cannot accidentally serve an open dashboard.
- **TLS is mandatory** and is terminated by your platform or a reverse proxy *in front
  of* the relay — **never** expose plain HTTP to the internet (Basic-auth credentials
  would travel in clear text).

### Generate the two secrets

```
python -c "import secrets; print(secrets.token_urlsafe(32))"   # run twice
```

Use one value for `ORION_RELAY_TOKEN` (ingest) and one for `ORION_RELAY_VIEW_TOKEN`
(dashboard read). The ingest token must match what your **local** `[relay].token_env_var`
resolves to.

## Persistence (do not skip)

The relay stores reports in a SQLite file. On ephemeral hosts (containers, Fly, Render's
default disk) that file is **wiped on every restart/redeploy** unless it lives on a
**mounted volume**. The Dockerfile writes it to `/data/orion-relay.sqlite3` and declares
`/data` a volume — mount real storage there.

---

## Test the image locally first (smoke test)

Build and exercise the container on your own machine before deploying. This verifies the image,
the dashboard read-auth, and the **fail-closed guard** end to end over plain HTTP on loopback —
*before* TLS and hosting are involved. (It does **not** exercise TLS or the reverse-proxy
topology; for that see Option B and KI-18.)

```bash
# 1) Two throwaway secrets, kept in your shell for the commands below.
export ORION_RELAY_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
export ORION_RELAY_VIEW_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")

# 2) Build.
docker build -t orion-relay .

# 3) Negative test — the fail-closed guard: NO view secret => must refuse to start (exit 1).
docker run --rm -e ORION_RELAY_TOKEN=anything orion-relay
#   expect: "Error: refusing to bind non-loopback host '0.0.0.0' ..."  and exit code 1

# 4) Real run (both secrets + a volume + the port).
docker run -d --name orion-relay-test -p 8787:8787 -v orion-test-data:/data \
  -e ORION_RELAY_TOKEN="$ORION_RELAY_TOKEN" \
  -e ORION_RELAY_VIEW_TOKEN="$ORION_RELAY_VIEW_TOKEN" \
  orion-relay
docker logs orion-relay-test            # expect: "dashboard: Basic-auth required"

# 5) Dashboard auth: no creds => 401; correct creds (any username) => 200.
curl -s -o /dev/null -w "no-creds:   HTTP %{http_code}\n" http://localhost:8787/
curl -s -o /dev/null -w "with-creds: HTTP %{http_code}\n" -u orion:"$ORION_RELAY_VIEW_TOKEN" http://localhost:8787/

# 6) Ingest a report with the Bearer token => 201 {"id": 1}.
curl -s -w "  HTTP %{http_code}\n" -X POST http://localhost:8787/ingest \
  -H "Authorization: Bearer $ORION_RELAY_TOKEN" -H "Content-Type: application/json" \
  -d '{"project":"smoke-test","share_level":"high_level","lane":"raw","body":"Hello.","generated_at":"2026-06-18T12:00:00+00:00","orion_version":"0.0.0","participants":["Alex"],"sections":[["Code activity","Verified the image."]]}'

# 7) Confirm it renders, and survives a restart (volume persistence).
docker restart orion-relay-test
curl -s -u orion:"$ORION_RELAY_VIEW_TOKEN" http://localhost:8787/ | grep smoke-test

# 8) Cleanup.
docker rm -f orion-relay-test && docker volume rm orion-test-data
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
  orion-relay
```

**Fly.io** (warm, ~$2/mo): `fly launch` (it detects the Dockerfile), then
`fly volumes create orion_data --size 1`, mount it at `/data` in `fly.toml`, set
`internal_port = 8787` with `force_https = true`, and
`fly secrets set ORION_RELAY_TOKEN=… ORION_RELAY_VIEW_TOKEN=…`. Fly terminates TLS.

**Render**: a Docker service; add a **Disk** mounted at `/data`; set both env vars; Render
provides HTTPS at `https://<service>.onrender.com`. (Free instances sleep — fine for
checking in, rough for a live demo.)

---

## Option B — Reverse proxy you run (VPS, Raspberry Pi, Jetson Orin Nano)

Here **you** terminate TLS with [Caddy](https://caddyserver.com) (auto Let's Encrypt). The
relay binds **loopback** (`127.0.0.1:8787`); only Caddy reaches it.

> ### ⚠ The one thing the guard can't do for you
> The fail-closed guard keys off the **bind host**. Behind a proxy the relay binds
> *loopback*, so the guard sees "safe" and **will not force the view secret** — even
> though Caddy exposes the dashboard to the world. **You MUST set
> `ORION_RELAY_VIEW_TOKEN` yourself in this topology**, or the dashboard is
> world-readable. (Tracked as **KI-18**.) Optionally add Caddy `basic_auth` as a second
> layer — see `Caddyfile.example`.

1. Run the relay on loopback (Docker with `--host 127.0.0.1`, or via `orion relay-serve`
   under a systemd unit). Set **both** secrets in its environment.
2. Put `Caddyfile.example` → `Caddyfile`, set your domain, point the domain's DNS at the
   box, and `caddy run`. Caddy fetches a cert and reverse-proxies `127.0.0.1:8787`.

A **Pi / Jetson Orin Nano on your own network** is the purest own-your-data option (the
data never leaves hardware you control); it needs a domain + dynamic DNS (or a tunnel) so
Let's Encrypt can validate and supervisors can reach it.

---

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

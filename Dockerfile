# =============================================================================
# Dockerfile — the Orion relay (the hosted half of C1)
# -----------------------------------------------------------------------------
# Responsible for: Packaging the separately-deployable relay — the ingest endpoint
#                  + read-only dashboard — into a container. The local Orion core
#                  (collectors, summarizer, delivery) is NOT in here; only the relay
#                  and the `orion` CLI that launches it.
# Role in project: Path B (self-host) deployment. The same image runs on any Docker
#                  host (Fly, Render, a VPS, a Raspberry Pi / Jetson box). See
#                  docs/deployment.md for the full runbook and the per-host notes.
# Build (from the repo root):  docker build -t orion-relay .
# =============================================================================

# --- Stage 1: build the SPA (E2 Inc 4) ---------------------------------------
# The dashboard is a React/Vite app under web/. We build it in a Node stage and copy
# only the static output (web/dist) into the final Python image — so Node is NOT in the
# runtime image, just the built assets the relay serves single-host (--web-dir).
FROM node:20-slim AS web-build
WORKDIR /web
# Copy manifests first so the dependency layer caches unless they change.
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# --- Stage 2: the relay runtime ----------------------------------------------
FROM python:3.12-slim

# Run as a non-root user — this is a network-facing service, so we drop privileges.
RUN useradd --create-home --uid 10001 orion
WORKDIR /app

# Install the orion package (gives the `orion` CLI + its 2 runtime deps). Copy
# pyproject + src first so this layer caches unless the package itself changes.
COPY pyproject.toml ./
COPY src/ ./src/
# The relay extra adds argon2-cffi for password login (relay-side only; the
# producer never imports it). Prebuilt manylinux wheels cover this image, so
# no compiler is needed in the final stage.
RUN pip install --no-cache-dir '.[relay]'

# The relay/ package is a SEPARATE top-level package (not under src/, so not part
# of the installed wheel). Copy it alongside and put /app on PYTHONPATH so
# `orion relay-serve` can import it — the same mechanism the test suite uses.
COPY relay/ ./relay/
ENV PYTHONPATH=/app

# The built SPA from stage 1 — served single-host by the relay (--web-dir below). Only the
# static output is copied; no Node toolchain reaches the runtime image.
COPY --from=web-build /web/dist ./web/dist

# The sqlite store must persist across restarts/redeploys — keep it on a mounted
# volume, not the container's ephemeral layer. Owned by the non-root user.
RUN mkdir -p /data && chown orion:orion /data
VOLUME /data
USER orion
EXPOSE 8787

# Bind 0.0.0.0 so the container is reachable. The fail-closed guard then REQUIRES
# ORION_RELAY_VIEW_TOKEN to be set (a non-loopback bind), so a misconfigured deploy
# refuses to start rather than serving an open dashboard. Pass both secrets as env:
#   ORION_RELAY_TOKEN       — the ingest Bearer token (must match your [relay] config)
#   ORION_RELAY_VIEW_TOKEN  — the dashboard read password (HTTP Basic)
# TLS is terminated by your platform/proxy in front of this container (see
# docs/deployment.md) — never expose plain HTTP to the internet.
# --web-dir serves the React SPA single-host (built in stage 1). Remove it to fall back to
# the legacy server-rendered HTML.
# --showcase enables the public, no-login Showcase; --showcase-project allowlists a project
# (default-deny) with its curated public blurb. Edit/extend this list to curate the guest
# surface; drop both flags to take it offline (GET /api/showcase then 404s).
# --disable-legacy-ingest (C3 Inc 2, cutover 2026-07-09): retires the shared ingest token on
# the push path — only named per-user contributor keys can push. Flipped after every producer
# migrated to its own key (the Mac's `macos`, verified via the dogfood). Remove this flag to
# re-enable the shared token.
ENTRYPOINT ["orion", "relay-serve", "--host", "0.0.0.0", "--db", "/data/orion-relay.sqlite3", "--web-dir", "/app/web/dist", "--disable-legacy-ingest", "--showcase", "--showcase-project", "orion:A local-first knowledge base that observes your real project activity and reframes it into readable progress."]

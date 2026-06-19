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
FROM python:3.12-slim

# Run as a non-root user — this is a network-facing service, so we drop privileges.
RUN useradd --create-home --uid 10001 orion
WORKDIR /app

# Install the orion package (gives the `orion` CLI + its 2 runtime deps). Copy
# pyproject + src first so this layer caches unless the package itself changes.
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# The relay/ package is a SEPARATE top-level package (not under src/, so not part
# of the installed wheel). Copy it alongside and put /app on PYTHONPATH so
# `orion relay-serve` can import it — the same mechanism the test suite uses.
COPY relay/ ./relay/
ENV PYTHONPATH=/app

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
ENTRYPOINT ["orion", "relay-serve", "--host", "0.0.0.0", "--db", "/data/orion-relay.sqlite3"]

#!/bin/sh
# =============================================================================
# relay-backup.sh
# -----------------------------------------------------------------------------
# Responsible for: Pulling a consistent backup of the deployed relay's SQLite
#                  store to this machine, with a dated filename.
# Role in project: The operator-owned half of the two-layer backup posture in
#                  docs/deployment.md (layer 1 is Fly's own volume snapshots,
#                  daily with 5-day retention). This is the layer that survives
#                  past that horizon and outside the Fly account.
#                  Invoked unattended by a scheduler — on macOS the launchd agent
#                  com.orion.relay-backup; see docs/scheduling.md for the Linux
#                  (cron / systemd timer) and Windows (Task Scheduler) idioms.
# Assumptions: `fly` is installed and holds a non-expiring token (see the runbook);
#              $HOME/orion-backups exists; the app scales to zero.
# Lives at the repo root alongside the other deploy artifacts (Dockerfile,
# fly.toml, Caddyfile.example) rather than in a scripts/ directory, matching the
# convention this repo already uses for ops files.
# =============================================================================
set -eu

APP="${ORION_FLY_APP:-project-orion}"
HEALTH_URL="${ORION_HEALTH_URL:-https://project-orion.fly.dev/healthz}"
DEST_DIR="${ORION_BACKUP_DIR:-$HOME/orion-backups}"
FLY="${FLY_BIN:-fly}"

DEST="$DEST_DIR/orion-relay.$(date +%Y%m%d).bak"
PART="$DEST.part"
REMOTE_TMP=/tmp/orion-pull.sqlite3

mkdir -p "$DEST_DIR"

# Failure marker (KI-49). This job's only failure signals used to be a non-zero
# exit in `launchctl print` and lines in a log — nothing an operator routinely
# sees, which is how three weeks of failed runs went unnoticed. On any failure,
# drop a dated FAILED-<date>.txt INTO the backup directory: the operator looks
# there anyway, and the marker sorts next to the backups it interrupts. A later
# successful run removes the markers, because their meaning is "your newest
# backup is older than it should be" — once a fresh backup lands that is no
# longer true (the failure history stays in the log). Deliberately NOT alerting
# through the relay: the thing being backed up must not report on its own backups.
# This covers a run that starts and fails; a job that never runs at all (unloaded
# plist, machine off) still needs the staleness check / runbook line KI-49 lists.
on_exit() {
    status=$?
    if [ "$status" -ne 0 ]; then
        printf 'relay-backup FAILED on %s (exit %s). See orion-backup-launchd.log; newest good backup is older than scheduled.\n' \
            "$(date '+%Y-%m-%d %H:%M:%S')" "$status" > "$DEST_DIR/FAILED-$(date +%Y%m%d).txt"
    fi
}
trap on_exit EXIT

# 1. Wake the machine. REQUIRED, not optional: `fly ssh console` does NOT auto-start a
#    stopped machine — it fails with "app <name> has no started VMs". Since this app runs
#    min_machines_running = 0, the machine is stopped most of the time, so an unattended
#    job without this step would fail nearly every run. /healthz is the cheapest wake
#    target (no auth, no DB read) and curl returns only once the cold start has completed.
curl -sf "$HEALTH_URL" > /dev/null

# 2. Take a consistent copy INSIDE the container, using SQLite's online backup API with a
#    read-only source. Consistent under WAL, and provably incapable of altering production
#    — unlike the `PRAGMA wal_checkpoint(TRUNCATE)` + file-copy approach, which writes to
#    the live database. Written to /tmp (ephemeral) rather than /data, so it never consumes
#    the volume the store itself lives on.
"$FLY" ssh console -a "$APP" -C "python3 -c \"import sqlite3; s=sqlite3.connect('file:/data/orion-relay.sqlite3?mode=ro', uri=True); d=sqlite3.connect('$REMOTE_TMP'); s.backup(d); d.close(); s.close()\""

# 3. Pull it down to a .part file, then move it into place.
#    Two reasons this is not a direct `fly sftp get` to $DEST. First, `fly sftp get`
#    REFUSES to overwrite an existing file ("doesn't override existing files for safety"),
#    so a same-day manual pull, or any retry, would make the scheduled run fail. Second,
#    and more important: a partial or failed transfer must never clobber a known-good
#    backup. The `mv` only runs if the pull succeeded (set -e), so the previous file
#    survives any failure.
rm -f "$PART"
"$FLY" sftp get "$REMOTE_TMP" "$PART" -a "$APP"
mv -f "$PART" "$DEST"

# 4. Don't leave a copy of every project's data sitting in the container.
"$FLY" ssh console -a "$APP" -C "rm -f $REMOTE_TMP"

# 5. Prove the file before reporting success — a backup that has never been read back is
#    a guess. Cheap enough to do every run.
python3 - "$DEST" <<'PY'
import sqlite3, sys
path = sys.argv[1]
conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
result = conn.execute("PRAGMA integrity_check").fetchone()[0]
reports = conn.execute("SELECT COUNT(*) FROM relay_reports").fetchone()[0]
conn.close()
if result != "ok":
    sys.exit(f"integrity_check FAILED on {path}: {result}")
print(f"{path}: integrity_check ok, {reports} reports")
PY

# 6. This run produced a verified backup, so any standing FAILED markers no longer
#    describe the current state — clear them (see the marker comment above).
rm -f "$DEST_DIR"/FAILED-*.txt

<!-- =========================================================================
docs/c3-inc2.5-dogfood.md
---------------------------------------------------------------------------
Responsible for: The two-machine end-to-end verification runbook for C3 Inc 2.5
                 (per-producer consolidation). Follow it after the slice is
                 merged to main and redeployed, to confirm the visible units
                 (effective checklist, per-producer slippage) on the live
                 dashboard and the storage-only units (per-producer skills,
                 disciplines) in the relay store.
Role in project: Operational aid, not product doc. Pairs with the kickoff
                 (docs/per-producer-consolidation-kickoff.md) verification
                 section and the CHANGELOG entry for the slice.
Assumptions: The relay is deployed on Fly as app "project-orion" with the DB on
                 the /data volume at /data/orion-relay.sqlite3, the legacy shared
                 ingest token is disabled, and each machine pushes with its own
                 contributor key.
========================================================================= -->

# C3 Inc 2.5 — two-machine dogfood runbook

## What this proves, and what it can't

Two of the four code units are **dashboard-visible**. The other two are **storage-only** by design (KI-32, "storage now, display later"), so they have no dashboard surface yet and are verified in the store.

| Unit | Verifiable by | Where |
|---|---|---|
| 1.1 effective checklist (KI-30) | eyes on the dashboard | badge / stats / scheduling |
| 1.2 per-producer slippage | eyes on the dashboard | producer cards + slipping count |
| 1.3 per-producer disciplines | DB inspection only | `relay_producer_disciplines` table |
| 1.4 per-producer skills | DB inspection only | `relay_producer_skills` table |

The dashboard will look identical for skills and disciplines (the comb and the Disciplines section still show the last writer). That is correct, not a bug. Their proof is that the rows land per author in the store.

## Step 0 — prerequisites

**Both machines must be on the merged `main`.** The WSL2 machine has not pulled since the previous two-person dogfood, so update it first:

```
git checkout main && git pull --ff-only
```

The per-producer behavior itself is all relay-side (the Inc 2.5 code lives in `relay/` and is redeployed in Step 1), so the CLI version is not what drives the merge. Pulling still matters. It brings WSL2 current with the contributor-lifecycle and legacy-ingest changes that landed since the last dogfood, and the relay now rejects the old shared token, so WSL2 must push with a real per-user key.

Both machines must push as **distinct active contributors** granted the `orion` project. The merge only engages at two or more producers, and the legacy shared token is off, so this is already required for either machine to push at all. Confirm from either machine:

```
ORION_RELAY_ADMIN_TOKEN=<admin> orion relay-user list
```

You want two active rows (for example `macos` and `wsl2`), each granted `orion`. If the second machine has no identity yet, create one and put its printed key in that machine's `.env` as `ORION_RELAY_TOKEN`:

```
ORION_RELAY_ADMIN_TOKEN=<admin> orion relay-user add wsl2 --role contributor --project orion
```

The distinguishing fact is simply that each machine's `ORION_RELAY_TOKEN` is a different contributor key. The relay derives `author_id` from the key, so identity is unforgeable and automatic.

## Step 1 — redeploy and confirm the self-migration

From the repo root on `main`:

```
fly deploy -a project-orion
```

The change is additive (two `CREATE TABLE IF NOT EXISTS`, zero ALTERs), so a deployed DB gains the tables on first open with no migration step. Confirm the three per-producer tables exist:

```
fly ssh console -a project-orion
# inside the container:
python3 - <<'PY'
import sqlite3
db = sqlite3.connect("/data/orion-relay.sqlite3")
t = [r[0] for r in db.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'relay_producer_%'")]
print("per-producer tables:", sorted(t))
PY
```

Expect `['relay_producer_checklists', 'relay_producer_disciplines', 'relay_producer_skills']`. Also skim `fly logs -a project-orion` around the restart. There should be no "no such column" or ALTER error, because there is no ALTER.

## Step 2 — effective checklist (1.1), dashboard-visible

Push the same project from both machines with a divergent done-state on one item.

```
# Mac  (ORION_RELAY_TOKEN = macos key): mark item X done in the tasks_file, then
orion checklist-push orion

# WSL2 (ORION_RELAY_TOKEN = wsl2 key):  leave item X open, then
orion checklist-push orion
```

Open `https://project-orion.fly.dev`, log in, and look at the `orion` card on the home and its project page.

- **Expect:** the badge, progress, and `stats` count item X as **done** (done = OR across producers), regardless of which machine pushed last. Each producer card shows its own copy (Mac done, WSL2 open). Scheduling drops item X if it had a deadline, because the merged item is done.
- **Pre-slice contrast:** the badge used to flip to whoever pushed last, so a WSL2-last push would have shown X open. If it holds steady at done, KI-30 is fixed live.

## Step 3 — per-producer slippage (1.2), dashboard-visible

Slippage needs at least two observations in one machine's own stream, so push twice from one machine with the deadline moved later.

```
# Mac: item Y due 2026-08-01, push; then move Y to 2026-09-01 and push again
orion checklist-push orion    # twice, deadline moved later the second time

# WSL2: item Y with a stable deadline, pushed once or twice, never moved
orion checklist-push orion
```

On the `orion` project page:

- **Expect:** Mac's producer card marks Y **slipping** (its own stream postponed). WSL2's card does not. The aggregate checklist row and the portfolio slipping count mark Y slipping (the union), and the count stays stable as the two machines interleave.
- **Pre-slice contrast:** interleaving two machines' pushes into one stream used to fabricate a "postponed" (one machine's earlier date followed by the other's later date) or inflate the lingering count. A stable, correctly-attributed count is the fix.

## Step 4 — per-producer skills and disciplines (1.3 / 1.4), DB-only

```
# Mac:
orion skills-sync
orion disciplines-push orion

# WSL2 (a different skills-enabled portfolio):
orion skills-sync
orion disciplines-push orion
```

The dashboard will not change (aggregate last-writer, by design). Confirm the per-author rows coexist in the store:

```
fly ssh console -a project-orion
python3 - <<'PY'
import sqlite3
db = sqlite3.connect("/data/orion-relay.sqlite3")
for t in ("relay_producer_skills", "relay_producer_disciplines"):
    print(f"\n{t}:")
    for r in db.execute(
        f"SELECT project, author_name, updated_at FROM {t} ORDER BY project, author_name"):
        print("  ", tuple(r))
PY
```

- **Expect:** for the shared `orion` project, rows under **both** author names, side by side. The aggregate `relay_project_skills` / `relay_project_disciplines` still holds only the last writer. That is KI-32, deferred on purpose.
- **Batch-trap check (optional):** confirm each contributor's `skills-sync` only pruned its own rows. After both sync, neither machine's rows for `orion` should have been wiped by the other's batch.

## Step 5 — single-producer parity (regression)

Look at a project only one machine pushes.

- **Expect:** byte-identical to before this slice. Its badge is that producer's numbers, and it shows no per-producer cards (cards appear only at two or more producers). This confirms the fewer-than-two-producer fallback did not disturb existing single-writer deployments.

## If something looks wrong

The slice is additive with no data migration, so a rollback is safe. Redeploy the previous image (`fly releases`, then `fly deploy --image <prior>`). The two new tables simply sit unused on the older code, and no aggregate data was touched.

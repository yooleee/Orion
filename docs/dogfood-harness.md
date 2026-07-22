# The dogfood harness

A disposable environment for exercising Orion's real code paths against real projects
without touching anything live. Built for the **DF1 sweep** (`plans/orion-plan.md`) and
kept because the next sweep, and any slice that wants eyes-on verification, needs the
same thing.

The problem it solves: most of Orion's interesting behaviour only appears when you
actually run it, but running it for real advances markers in `orion.sqlite3`, pushes to
the production relay, and sends messages to supervisors. The harness keeps every input
real and redirects every output.

| What | Real or redirected |
|---|---|
| Repos, `tasks.md`, `to_do.md`, `CLAUDE.md` | **Real** — the point is real data |
| Anthropic summarizer / extractor | **Real** — real API calls, real cost |
| State store (`state_db`) | Redirected to a scratch sqlite |
| Recipients (Discord/Slack webhooks) | Redirected to a local sink |
| Relay | Redirected to a locally-run relay with its own DB |

## Layout

Everything lives in one throwaway directory, referred to below as `$SB`:

```
$SB/
  orion-sandbox.toml    the project registry (real repo paths, redirected outputs)
  .env                  sandbox-only secrets (see "Secrets" below)
  sink.py               the fake webhook endpoint
  requests.jsonl        every request the sink received (the evidence file)
  sandbox.sqlite3       the producer's state store
  relay.sqlite3         the local relay's store
```

## 1. The webhook sink

Delivery does not validate the webhook URL — it POSTs whatever it is given
(`src/orion/delivery/discord.py`) — so a loopback server exercises the real sender byte
for byte. Write a stdlib `http.server` that logs each request as one JSON line and picks
its status code from the URL **path**, so a single process can serve several recipients
with different behaviours:

| Path | Behaviour | Used for |
|---|---|---|
| `/ok`, `/ok2` | 204 (what Discord returns) | ordinary delivery |
| `/same` | 204, configured for two recipients | reproducing a double-post |
| `/fail500` | 500 | partial-failure policy |
| `/hang` | sleeps past the client timeout | timeout handling |

Run it on a spare port (the sweep used 8799).

## 2. The sandbox config

Copy the live `orion.toml`, keep every `repo_path` / `tasks_file` / `tracker_file` /
`discipline_docs` pointing at the **real** files, and change only:

- `state_db` → a path inside `$SB`
- every recipient's `webhook_env_var` → a sandbox name resolving to a sink URL
- `[relay]` → `url = "http://127.0.0.1:8788/ingest"`, and rename `token_env_var` /
  `admin_token_env_var` to `ORION_SB_*` names

**Rename the relay env vars — this is not cosmetic.** Commands run from the repo
directory pick up the production `.env` through dotenv's working-directory search. If the
sandbox config named `ORION_RELAY_TOKEN`, a production token could end up authenticating
against the local relay. Distinct names make that impossible rather than unlikely.

Pick a project whose shape differs from `orion`'s. `applications` is the useful one: no
git collector (so no LLM lane), `kind = "tracker"`, a `cadence`, and a checklist.
Single-config assumptions are what hide bugs, so a second shape is where they surface.

## 3. The local relay

```
orion relay-serve --port 8788 --db $SB/relay.sqlite3 \
  --token-env ORION_SB_RELAY_TOKEN \
  --web-dir web/dist \
  --config $SB/orion-sandbox.toml
```

`--config` matters: it is how the relay finds the sandbox `.env`. Note that the relay
reads three secrets under **fixed** names it will not let you rename
(`ORION_RELAY_ADMIN_TOKEN`, `ORION_RELAY_SESSION_KEY`, `ORION_RELAY_USER_PEPPER`), so the
sandbox `.env` must define those too. Provision accounts with `relay-user`, pointed at the
sandbox config.

## Secrets

No production secret is copied into `$SB`. The sandbox `.env` holds only freshly generated
local values plus the sink URLs. `load_secrets` uses `override=False`
(`src/orion/secrets.py`), so anything already exported wins, and the config-relative `.env`
is loaded first — which is why the name separation above is sufficient. The one real secret
in play is `ANTHROPIC_API_KEY`, which commands run from the repo root pick up from the
project `.env` on their own.

## Gotchas found the hard way

- **`relay-user key add` requires `--label`**, and prints the key on the *third* line, not
  the last. Scrape it with a pattern (`grep -oE '^ {4}[A-Za-z0-9_-]{40,}$'`), not `tail -1`
  — grabbing the wrong line yields a confusing 401 rather than an obvious error.
- **`POST /api/login` enforces a same-origin CSRF check.** A script that omits an `Origin`
  header gets 403 for every request, which looks exactly like a throttle or an auth failure.
  During the sweep this produced a *false* reproduction of KI-38 that had to be retracted.
  Always send `Origin: http://127.0.0.1:<port>`.
- **The login throttle is in-memory.** Tripping it (50 failures in 5 minutes, relay-wide)
  locks out *your own* subsequent testing. Restarting the relay clears it instantly.
- **A relative `discipline_docs` path resolves next to the CONFIG file, not the repo.** A
  sandbox config living outside the repo therefore reads nothing. This is how the
  `disciplines-push` empty-clobber bug was found, so it is a useful accident, but know that
  it is happening.
- **Sqlite connections are thread-bound.** Concurrency probes must open *and* close inside
  the worker thread.
- **`git checkout` between branches** while iterating means a fix you are relying on may
  silently vanish from the working tree. Keep discovery on `main` and each fix on its own
  branch.

## Teardown and verification

Delete `$SB` and kill the two servers. Then confirm nothing live moved:

- `git status` in the repo is clean
- `orion.sqlite3`'s mtime is unchanged
- `$SB/requests.jsonl` is the *only* record of outbound POSTs — if a real webhook had been
  hit, it would not appear there

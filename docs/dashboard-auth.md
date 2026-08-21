<!-- =========================================================================
docs/dashboard-auth.md
---------------------------------------------------------------------------
Responsible for: Explaining how the relay dashboard's multi-party access works —
                 accounts and credentials, password login, roles, per-project scope
                 and visibility, agents, provisioning, and the security model.
Role in project: The reference a self-hoster or contributor reads to understand
                 who can see what on a shared dashboard and why. Operational deploy
                 steps live in docs/deployment.md; this file explains the model.
Companion: docs/deployment.md (deploy + secrets), the README "Web dashboard"
           section (the quick tour), relay/server.py (the implementation).
========================================================================= -->

# Dashboard access: identity, login, roles, and scope

The relay dashboard used to be guarded by a single shared password over HTTP Basic auth.
Anyone who had it saw everything. That is fine for one person, but it cannot say "Alex may
see project X but not project Y," and it cannot be revoked for one person without changing
the password for everyone.

The dashboard now has real per-user access, built from three distinct layers. Keeping them
separate is the standard way to reason about access control.

- **Identity** is who you are. Each person or machine is an **account** (a row in
  `relay_users`) with a name, a role, and a kind. The account is durable and outlives any
  credential attached to it.
- **Authentication (authN)** is proving who you are. A person logs in with a **name and
  password**; a machine presents a **key**. Both resolve to an account.
- **Authorization (authZ)** is what you may see. Your role, your project grants, and each
  project's visibility decide what the dashboard shows you.

The only third-party dependency in any of this is `argon2-cffi`, used on the relay to hash
passwords. There is no OAuth and no external auth service.

## What a user experiences

1. They open the dashboard. With no session they are redirected to `/login` (HTTP 303).
2. They enter their **name and password** and submit. (An account that has no password yet
   can still sign in with its access key — see the transition note under Passwords.)
3. The server verifies the password and, if it matches an active interactive account, sets a
   **session cookie** and redirects them to the dashboard.
4. They browse, seeing only what their role and scope allow. An admin sees every project. A
   viewer or supervisor sees only the projects granted to them. A member sees every
   org-visible project plus its own grants. Anything else returns a plain "not found."
5. They can post to a project's discussion thread, if their role permits it. The message is
   attributed to their account name and role, not to anything they type.
6. `GET /logout` clears the session.

## Authentication: credentials and the session cookie

This is session-based authentication, the pattern most web apps use. Several credentials are
in play, and it helps to keep them separate.

### The access key (machines)

An access key is a machine credential: server-generated and high entropy (a 256-bit random
value). The server never stores the key itself. It stores only a **verifier**, computed as
`HMAC-SHA256(user_pepper, key)`. HMAC is a keyed hash, and the "pepper" is a server-side
secret that lives only in the relay's environment, never in the database.

Two properties fall out of this. A database leak alone does not reveal anyone's key, because
only the verifier is stored. And a database leak alone does not even let an attacker test
candidate keys, because computing a verifier also needs the pepper. On a push the server
recomputes the verifier from the key presented and looks for a matching active credential.

A slow hash (bcrypt, argon2) is deliberately **not** used for keys. Slow hashes defend against
brute-forcing low-entropy human secrets; these keys are server-minted and 256-bit random, so
there is nothing to brute-force and the cost would buy nothing. Passwords are the opposite
case — a human picked them — which is exactly why they *do* get Argon2id. Same reasoning,
opposite conclusion, because the threat differs.

### The session cookie (short-lived)

After login you carry a session cookie named `orion_session`. It is a **signed, stateless
token**. Signed means the server attaches an HMAC over the contents with a separate signing
key (`ORION_RELAY_SESSION_KEY`), so the browser cannot forge or alter it. Stateless means the
server keeps no session table. Everything needed to validate the cookie is the cookie plus
the signing key.

The cookie carries only a format version, your user id, an issued-at time, an expiry, and a
session-version number. It deliberately does **not** carry your role or your project scope.
That is the rule "trust the database, not the cookie." On every request the server re-reads
your role and scope fresh from `relay_users`, so a change to your access takes effect on your
next request, and a privilege can never ride in a stale or forged cookie.

Expiry is enforced from the signed payload, not just the cookie's `Max-Age`, so a browser
that holds the cookie past its lifetime still cannot use it. The session length is configurable
with `relay-serve --session-days` (default 30).

## Revocation without a server-side session store

Because there is no session table, "log this person out everywhere, now" needs a mechanism.
Each user row has a `session_version` integer, and the cookie carries the value it was minted
with. To revoke someone the server bumps their `session_version`. On the next request the
cookie's version no longer matches the database and is rejected. Revoking an **account** also
sets `active = 0` and deactivates **all** of its credentials, so neither its password nor any
of its keys work afterwards. Both happen in one update, so there is no window where one took
effect but not the other.

This is why a revoke is instant. Every credential stops working, and any cookie already in a
browser dies on its next request, with no session bookkeeping to clean up.

Credential lifecycle and session lifecycle are deliberately **separate**, though. Revoking one
key does *not* bump `session_version` — losing a machine key should not log the human out
everywhere. Setting or resetting a password *does*, because that is a change to how the human
themselves authenticates.

## Authorization: roles and per-project scope

Five roles exist today. The `role` column is an open enum, so more are additive later. The
first four are **interactive** (they log into the dashboard); the fifth is **push-only** (it
authenticates the machine ingest path and cannot log in — see below).

- **admin** sees every project and can provision and revoke users.
- **viewer** is scoped to the projects an admin granted, listed in `relay_user_projects`.
- **supervisor** is a scoped participant like a viewer, and may additionally post to a
  project's discussion thread (E2 Inc 5).
- **member** is the read-only **org insider**: it sees every **org-visible** project with no
  per-project grant at all, plus any explicit grants on top. It can write nothing, anywhere —
  not a push, not a discussion reply. This is the role a company-wide knowledge base is built
  on, because it makes "everyone here can read our projects" possible without hand-granting
  every person every project.
- **contributor** (C3 Increment 2) is a **push-only producer identity**: it authenticates the
  machine ingest endpoints with its own key, scoped to its granted projects, but is deliberately
  barred from logging into the dashboard — one credential never spans both auth worlds, so a
  stolen push machine's `.env` cannot grant a human dashboard access. A person who both produces
  and wants dashboard eyes gets a separate interactive identity.

Note the distinction between the two scoped read roles, because it is easy to miss: a
**viewer/supervisor is an outside participant** (a family member, a mentor) and sees only what
was explicitly shared with them — org-visibility does not widen their view. A **member is
inside the org** and sees its shared work by default. Making a project org-visible therefore
opens it to members, never to an external supervisor.

### Project visibility

Every project is either **`restricted`** (grant-only — the birth state, and the behavior every
project had before this existed) or **`org`** (any member may read it). Flipping one is a
deliberate admin act:

```bash
orion relay-project visibility my-app org         # any member can now read it
orion relay-project visibility my-app restricted  # back to grant-only
```

Default-deny is what **absence** means, not merely a column default: a project with no stored
visibility, a NULL value, and an explicit `restricted` all read as restricted. Nothing becomes
readable because a row was never written or a migration did not run. A flip takes effect
immediately in both directions for sessions already open, because scope is re-read on every
request rather than cached in the cookie.

### Existence-hiding

The scope check runs on **every route**, not only the project index. Anyone who requests a
project or report outside their scope receives a 404 that is byte-for-byte identical to a
genuinely missing one. This is existence-hiding, chosen because the audience can include
outsiders. A 403 "forbidden" would leak that the resource exists and reveal its name. A 404
reveals nothing — and that has to hold for a restricted project, a report id belonging to one,
and case or Unicode variants of a real name alike, or the status code itself would enumerate
the org's project list. The model is also default-deny: a new viewer with no grants sees an
empty dashboard until an admin grants them a project.

## Provisioning: the admin API and the `relay-user` CLI

Users are created and managed through a small admin API on the relay (`POST /api/users`,
`GET /api/users`, `POST /api/users/revoke`), driven by the CLI:

```bash
orion relay-user add supervisor-a --role viewer --project my-app  # a dashboard viewer
orion relay-user add teammate-b --role member                     # reads every org-visible project
orion relay-user add mac  --role contributor --project my-app     # a push-only producer (a machine)
orion relay-user list                                             # the roster (no key material)
orion relay-user grant mac --project other-app                    # add a project to an existing user
orion relay-user ungrant mac --project other-app                  # take one back (grant's inverse)
orion relay-user role supervisor-a supervisor                     # change a role (logs out live sessions)
orion relay-user rename mac mac-mini                              # rename (history keeps the old name)
orion relay-user deactivate mac                                   # instant cutoff (keeps the name)
orion relay-user delete mac                                       # hard-delete: frees the name to reuse
```

### Passwords: how humans sign in

Interactive accounts (`admin`, `viewer`, `supervisor`, `member`) log in with a **name and password**.
Machines hold keys. The two do not overlap — a password is rejected on the push path, and a key
is rejected at login once its account has a password.

```bash
orion relay-user password set dad          # prompts twice, hidden; never takes it as an argument
orion relay-user password set dad --generate   # relay mints one and prints it ONCE
orion relay-user password unlock dad       # clear a lockout without changing the password
```

Stated plainly: a password is **weaker per-credential** than a 256-bit key. The gain is
compartmentalisation and human factors — a person can hold a password in their head and stops
handling stored key material entirely. That trade is why login is throttled.

**Setting a password is a one-way door for that account's key login.** From that moment the key
no longer signs in (it keeps working for machine pushes). Until a password is set, key login
still works, so an existing account is never locked out mid-transition.

**Throttling.** Five failed attempts lock an account for fifteen minutes, and a relay-wide
rolling bound catches attempts sprayed thinly across many names. Any admin can clear a lockout
instantly with `password unlock` — which matters, because anyone who knows an account name can
trigger a lockout on it. There is no per-IP limit: behind a proxy that means trusting a
forwarded header, and trusting it wrongly would let an attacker forge it and evade the limit
entirely.

**Every failure looks identical** — same status, same body, same headers, and the same amount of
time. Unknown name, wrong password, no password set, and locked-out all perform one Argon2
verification (against a dummy hash where there is no real one), so response timing cannot be
used to discover which accounts exist.

**Operational note.** Argon2id is memory-hard by design, so concurrent verifications are capped
to bound peak memory on a small VM. Under a burst of login attempts, logins queue rather than
the relay running out of memory.

### Credentials: an account can hold several keys

An **account** is the durable identity; a **credential** is one of N revocable things beneath it. That
is what lets one person hold a key on their Mac and another on a WSL2 box under a single identity,
instead of needing two identities (or re-keying one machine whenever the other changes).

```bash
orion relay-user key add mac --label wsl2   # attach ANOTHER key; existing keys keep working
orion relay-user key list mac               # ids, labels, and last-used (never key material)
orion relay-user key revoke mac --id 2      # kill ONE credential; the account and its others live on
```

Each `add` (account or key) prints a one-time access key. For a `contributor`, that key is what the
producing machine puts in its own `.env` under `ORION_RELAY_TOKEN` (the same variable name the shared
ingest token used), so no `orion.toml` change is needed — each machine simply carries its own key.

**Replacing a machine key is `add` → deploy → verify → `revoke`.** Add the new credential, install it,
confirm a push works, and only then revoke the old one. The two keys overlap, so there is no window
where a scheduled push starts failing, and no way to strand yourself if the new key is lost in transit.
Compromise response is the same sequence with the order reversed: `revoke` immediately, `add` when you
are ready. (This replaced a one-shot `rotate` command, which killed the old key the instant the new one
was minted and had both of those failure modes.)

The full lifecycle: **`grant`** widens a user's project scope in place; **`role`** changes what an
account is allowed to do; **`rename`** changes its label; **`key add`/`key revoke`** manage credentials
independently of identity; **`revoke`** is an immediate cutoff that keeps the name; **`delete`** removes
the account and frees the `UNIQUE` name to be reused, while their past reports and discussion replies
keep the author name already recorded on them.

### Agents: machines that act on a person's behalf

An **agent** (Claude Code, a CI job, a research runner) is a first-class account of kind
`agent` with its own key, tied to the human it acts for:

```bash
orion relay-user add claude-mac --role contributor --kind agent --operated-by yoo --project my-app
orion relay-user set-operator claude-mac someone-else   # reparent it to a different human
```

The rules are enforced, not conventional. An operator must be an **active human** account;
agent-to-agent chains and self-reference are refused, because the whole point of the link is
that it terminates at an accountable person. An agent is pinned to role `contributor` and
cannot be promoted to an interactive role, so a machine account never gains a dashboard login.
Deleting an account that still operates active agents is **blocked** — reassign them first
(that is what `set-operator` is for). Revoking an operator does *not* revoke its agents: each
credential is revoked individually, so retiring a person does not silently stop scheduled work
nobody asked to stop.

Two things follow, and they pull in opposite directions on purpose:

- **Attribution keeps the agent.** A report pushed by an agent is badged as agent work and
  names the human it acted for ("operated by …"). Provenance is never lost — you can always
  see that a machine did a given piece of work and on whose behalf.
- **Work-tracking folds into the operator.** For per-contributor checklist cards and producer
  streams, an agent groups under its human: you plus your two agents is *one* contributor, not
  three. An agent has no separate checklist of its own, because it is executing your work, not
  proposing its own.

One subtlety worth stating, since it is the easiest thing to get wrong: folding is a **display
grouping applied after every derivation**, never before. Slippage is still derived on each raw
producer's own observation stream and only unioned under the operator for display. Merging the
histories first would interleave a human's and an agent's observations of the same item and
manufacture a "postponed" signal that never happened.

> **Watch out when demoting an admin.** Admins bypass project scope, so an admin account usually has
> **no grants at all**. Changing it to a scoped role (`viewer`, `supervisor`, `member`) makes
> default-deny apply immediately, and the account will see **nothing** until you `grant` it projects.
> The CLI warns when a role change leaves an account with an empty scope.

Two security points matter here.

- The admin API is gated by a **separate** admin token (`ORION_RELAY_ADMIN_TOKEN`), independent
  of the ingest token your local Orion uses to push reports. Whoever can push reports must not
  automatically be able to mint dashboard users. This bounds the blast radius if one secret
  leaks.
- The access key is shown exactly once, at creation. Only the verifier is stored, so a lost key
  cannot be recovered, only replaced — `key add` a new credential, install it, verify a push,
  then `key revoke` the old one. The account and its other credentials are untouched.

`relay-user` needs only a `[relay]` table in your `orion.toml` (an `admin_token_env_var` plus
the `url`), not a list of local projects, so an admin who runs the relay but reports from
elsewhere can still provision.

## The push (ingest) path: producer identity and the legacy cutover

The machine ingest endpoints (report push, checklist/disciplines push, the CLI
discussion pull/reply) authenticate with a **Bearer** token, not a login cookie. Two kinds of
credential are accepted:

- An **account's own key credential** (C3 Increment 2; credential-based since the auth revamp).
  The relay resolves the presented key to a credential, then to its owning account, server-side —
  exactly as a login resolves an interactive user. Reports it pushes are attributed ("pushed by
  <name>"), its CLI discussion replies carry its real name (any `--as` is ignored), and it keeps
  its own per-producer checklist.

  **A Bearer key is always contributor-bounded — including on an admin account.** Whatever role
  the owning account holds, a key authenticates with contributor authority scoped to that
  account's explicit project grants, and an out-of-scope push gets a 404 identical to a missing
  project. An admin account with no grants can therefore push *nowhere*. This is deliberate and
  permanent: once an account can hold several credentials, the natural thing to do is attach a
  machine key to your own (admin) account — and under the old "admin sees everything" rule that
  machine would silently gain push access to every project, making compartmentalization *worse*
  than one key per identity. Admin authority still applies fully to the human's dashboard session;
  it is only the machine credential that is bounded. Provisioning is unaffected — it uses the
  separate admin token — and the legacy shared token below is unchanged.
- The **legacy shared ingest token** (`ORION_RELAY_TOKEN` on the relay side). It still works for
  backward compatibility, but its pushes are **anonymous** (no author is ever mapped to it — any
  holder could impersonate a person). Every use logs a line so an operator can watch it go quiet
  as producers migrate to their own keys.

Every Bearer failure returns **one generic 401** (now that named contributor keys exist, a
specific message would help an attacker enumerate them).

**Retiring the shared token (the deliberate cutover).** Once every producer has its own
contributor key, an operator disables the shared token by starting the relay with
`relay-serve --disable-legacy-ingest`. From then on the shared token 401s and only named
per-user keys can push. This is **operator-driven on purpose**: a machine credential must not
silently expire (the failure mode would be a silently-401ing cron push, not a human at a login
form), so the shared token keeps working until the operator flips the flag.

## The discussion write carries real identity

When a logged-in principal posts to a project's discussion thread, the server attaches their
authenticated account name **and role** and ignores anything they send in the body, so nobody can
post under someone else's name or claim a role they do not hold. Auth is always required — there is
no free-text-author fallback (the retired comment write had one; the discussion write does not). The
Bearer machine path (`POST /api/discussions`, the developer's CLI reply) is always fixed to role
`developer`. (KI-28 Stage 2 retired the earlier `report_comments` comment write, which this
supersedes.)

## The security invariants

A few rules are permanent, not stage-appropriate conveniences.

- **Independent secrets.** The cookie signing key, the user pepper, and the admin token are
  each their own value, never derived from one another or from the push tokens. They bound
  separate failure domains and rotate independently.
- **Fail-closed.** If the dashboard is access-gated but the session secrets are missing, the
  relay refuses to start rather than serving a login that could never work. It also refuses to
  bind a non-loopback address without a view secret.
- **CSRF defense on writes.** The cookie is auto-sent by the browser, so a malicious page could
  try to forge a cookie-authed write (the discussion post, login, logout). The server checks the
  request `Origin` against the configured canonical origin (`ORION_RELAY_PUBLIC_ORIGIN`), and the
  cookie is `SameSite=Lax`, so a cross-site POST is blocked two ways.
- **Cookie hygiene.** Cookies are `HttpOnly` (JavaScript cannot read them, which neutralizes
  theft via XSS) and `Secure` when the relay is HTTPS-exposed (the browser sends them only over
  HTTPS).
- **Hardening headers.** Every dashboard HTML response carries a hash-based
  `Content-Security-Policy` (only same-origin resources, plus the SHA-256 of the page's own inline
  style/script, so no `unsafe-inline` is needed and nothing external loads) and `X-Frame-Options:
  DENY`. All responses carry `X-Content-Type-Options: nosniff` and `Referrer-Policy: same-origin`,
  and `Strict-Transport-Security` when the relay is HTTPS-exposed. (`Referrer-Policy` is
  `same-origin`, not `no-referrer`: under `no-referrer` a browser sends a cookie-authed POST with
  `Origin: null` and no Referer, which the origin CSRF check then 403s — a real bug (originally hit on
  the comment write) fixed by switching to `same-origin`, which still leaks no referrer to other
  origins.) This is defense-in-depth behind
  the `_esc` escaping discipline: a CSP would neutralize an injected script even if an escaping bug
  slipped through.

## The legacy view token

`ORION_RELAY_VIEW_TOKEN` still exists, but its role changed. It is no longer an HTTP Basic
password. It is now the **bootstrap-admin login key**, usable to log in only while no users
have been provisioned yet, so a fresh deploy is not locked out before it creates its first
admin. Once any user exists it stops working, unless the operator passes
`relay-serve --allow-legacy-admin`. It also still satisfies the fail-closed guard that a
non-loopback bind must carry a view secret.

## Environment secrets (relay side)

These live in the relay's environment, set on the machine that runs `orion relay-serve`. See
docs/deployment.md for generating and setting them.

| Variable                  | Protects                                   | Required when                   |
| ------------------------- | ------------------------------------------ | ------------------------------- |
| `ORION_RELAY_TOKEN`       | Ingest (the report push)                   | Always                          |
| `ORION_RELAY_VIEW_TOKEN`  | Bootstrap-admin login + the bind guard     | Any non-loopback bind           |
| `ORION_RELAY_SESSION_KEY` | Signing session cookies                    | Whenever the dashboard is gated |
| `ORION_RELAY_USER_PEPPER` | Hashing stored **key** verifiers (not passwords) | Whenever the dashboard is gated |
| `ORION_RELAY_ADMIN_TOKEN` | The provisioning API (`relay-user`)        | To create or manage users       |

`ORION_RELAY_PUBLIC_ORIGIN` (the deployed `https://...` URL) is recommended in production for
the canonical-Origin CSRF check.

Password hashing needs `argon2-cffi`, installed with the **`relay` extra** on the machine that
runs the relay. It is deliberately relay-only — the producer never imports it, so a local Orion
install keeps its small dependency footprint. If the relay starts without it, password login
fails closed with a clear error rather than silently degrading to something weaker.

## Data and schema

The relay stores everything in one SQLite database on a persistent volume. The identity tables
are:

| Table | Holds |
| --- | --- |
| `relay_users` | Accounts: name, role, `kind` (human/agent), `operated_by`, active flag, session version |
| `relay_credentials` | N credentials per account: type (key/password), label, verifier, active flag |
| `relay_user_projects` | Explicit per-project grants (default-deny) |
| `relay_project_meta` | Per-project settings, including `visibility` (org/restricted) |
| `relay_admin_audit` | A trail of provisioning acts |

The account↔credential split is the structural change worth internalizing: **an account is the
durable identity, and credentials are attachable, individually revocable things beneath it.**
That is what lets one person hold keys on two machines under one identity, and what lets a
password be reset without disturbing any machine key.

Every table is created with `CREATE TABLE IF NOT EXISTS`, and new columns arrive as additive
`ALTER`s, so a relay that already holds reports migrates itself on deploy. The one-time
migration that copied each existing account's key into a credential row ran as a single
serialized transaction with a postcondition check, so no account could be left credential-less
and no key was ever re-minted.

## What is not here yet

Named seams, deliberately not built:

- **Delegation (acting-as).** An agent acts *on behalf of* a human but is always attributed as
  itself. A credential writing *as* another identity — the parked Slack bot's need — would
  bolt on additively; nothing in the schema precludes it.
- **Per-IP login throttling.** The throttle is keyed by dimension (account, global) so an `ip`
  dimension is additive, but it needs a proxy-trust flag to be safe. See KI-38.
- **Self-service signup, email, a password-change UI, OAuth/OIDC, teams/groups.** All additive
  on the account model if ever wanted.
- **Stamping an authenticated author onto the report blob itself** — the submitter-identity
  half of KI-17.

<!-- =========================================================================
docs/applications-tracker-kickoff.md
---------------------------------------------------------------------------
Responsible for: The kickoff for tracking the user's applications to-do list on
                 the Orion dashboard via a status-aware "tracker" collector, plus
                 the sibling tasks_file-bootstrapping idea.
Role in project: A kickoff doc (like the archived phase/dashboard kickoffs). Read
                 it at the start of the next session, THEN do a plan-mode scoping
                 pass before any code. This one has real design forks to settle.
                 Roadmap: plans/orion-plan.md "Horizon E" table + the E2 ladder
                 (Inc 2.6). Builds on the shipped checklist features (Inc 2 / 2.5,
                 PRs #47 / #49). Access model: docs/dashboard-auth.md. Deploy:
                 docs/deployment.md.
========================================================================= -->

# Kickoff: applications tracking via a status-aware "tracker" collector (E2 Inc 2.6)

> **✅ SHIPPED + ARCHIVED (2026-06-26).** This kickoff is a historical record. Inc 2.6 (the tracker
> collector + `tasks_file` bootstrapping) shipped in **PR #50**; the `applications` project is wired
> and pushed live. The carried-over CSRF bug below was **RESOLVED in PR #51** — and its real root cause
> was NOT the hypothesis recorded below (`public_origin` not live): the actual cause was the dashboard's
> own `Referrer-Policy: no-referrer` forcing every browser comment POST to `Origin: null`. The original
> (now-superseded) text is preserved unchanged for the record. Current state lives in the
> `plans/orion-plan.md` roadmap (E2 ladder). Remaining open item: family viewer provisioning (on hold).

## ⚠ Carried-over bug (unrelated to this kickoff) — CSRF comment 403 still failing

The relay CSRF comment fix from **PR #48** (this cycle) **did NOT resolve the bug**. The error is
**identical after deploy**, so the earlier diagnosis was wrong and must be redone from scratch. This
is a **live production breakage of the family comment loop** (supervisors can't reply on the
dashboard), so it is worth fixing early. Sequence relative to the applications work as you see fit, but
do not let it get lost.

**Corrected evidence (from the user):**
- The commenter (the user's dad) was on **Chrome**, not Safari. Chrome **does** send an `Origin`
  header on same-origin form POSTs, so PR #48's "missing-`Origin` → `Referer` fallback" does not even
  apply to his case. The Safari theory was wrong.
- The error message is **exactly the same** after the deployed fix: "Request blocked by an origin
  (CSRF) check."
- It occurs at `https://orion-relay-horizon-c.fly.dev/report/25/comment`.

**Leading hypothesis (verify empirically — do NOT assume again):** the check falls into the
`_origin_error` branch (`relay/server.py`) that compares the request's `Origin` against the `Host`
header, and behind Fly's proxy the `Host` the relay sees does not equal the public origin Chrome
sends. The intended guard is `ORION_RELAY_PUBLIC_ORIGIN` (set in `fly.toml [env]` this cycle); if that
var is **not actually live in the running Fly app**, the exact-origin match never runs and the brittle
Host fallback fails. In short: the fix may simply not be deployed/effective, or `public_origin` is
unset/mismatched in production.

**First diagnostic steps (next session):**
1. Confirm `ORION_RELAY_PUBLIC_ORIGIN` is actually set in the **running** app (e.g. `fly ssh console`
   then inspect env, or `fly secrets list`). `fly.toml [env]` only takes effect on a deploy that
   actually applied it.
2. Temporarily log the inbound `Origin`, `Referer`, and `Host` on a 403 in `_handle_comment` /
   `_origin_error`, reproduce on **Chrome**, and read the real values. That shows exactly which
   comparison fails.
3. Fix from the real values (likely: make `public_origin` reliably set, and/or trust Fly's forwarded
   host/proto rather than the rewritten `Host`). Add a regression test for the real Chrome + proxy case.
4. Verify live by commenting as admin on the actual dashboard, then update/delete the
   `csrf-comment-bug-followup` memory.

## Why

The checklist features shipped this cycle (Inc 2 live checklist signal, Inc 2.5 near-real-time
`checklist-push --watch`) are deployed but **unused**: no project has a `tasks_file`, so nothing is
tracked yet. This is their first real workload.

The user wants to track a real **applications tracker** on the dashboard, visible to family. The file
is `/Users/yoolee/Developer/applications/to_do.md` (a git-backed repo). It is a **rich document**, not
a checkbox list:

- Each application is a `## N. <Title>` section with a `- **Status:** Not started / In progress /
  Submitted / Closed` field, plus details (deadline, link, location, notes).
- A "Non-Application To-Do" section is a Markdown **table** with `Task / Purpose / Deadline` rows.
- Some complex tasks have **sub-goal tables** with staggered deadlines.
- It contains **zero** `- [ ]` / `- [x]` checkboxes.

The shipped `tasks` collector parses only GitHub-style checkbox lines, so pointed at this file it
surfaces nothing. Rather than reformat the tracker into checkboxes (which would flatten a deliberately
rich document), the user chose to build a **richer, status-aware collector** that reads the file's
native format. That is the work this kickoff scopes.

## Context to carry (current project state, decided this cycle)

- **Alex / Sam are on-hold placeholders.** The two recipients in `orion.toml` ("Alex (supervisor)"
  on Discord, "Sam (supervisor)" on Slack) represent chat channels whose development is **paused**.
  The real supervisors are **family**, who use the **dashboard** (relay viewer logins, C3). The
  dashboard, not chat, is the delivery surface for this work.
- **Applications are checklist-only.** No git-activity reporting, even though the repo is git-backed.
- **Visibility is grant-controlled.** C3 is default-deny: a family viewer sees the applications
  project only if explicitly granted it. Applications stay private otherwise.

## The build: forks to settle in the plan-mode pass

1. **New collector vs. extend `tasks`.** Lean: a **separate `tracker` collector/parser**. The formats
   are genuinely different, and keeping `tasks` pure to checkboxes avoids overloading one parser with
   two grammars. Decide explicitly. (Adding a fifth/sixth collector is the established additive pattern:
   `SUPPORTED_COLLECTORS`, `COLLECTOR_FILE_KEYS`, a `ProjectConfig` file field, `_parse_project`, the
   `cli` import + `_COLLECTOR_TITLES` + `_collect_for` dispatch + error handling.)
2. **Status to done/open mapping.** Each `## N. <Title>` application is one checklist item.
   `Submitted` / `Closed` map to done, `Not started` / `In progress` to open. Decide whether to also
   parse the **non-application table** rows (Task / Deadline) as a second item source, or scope v1 to
   the application sections only.
3. **Deadlines and at-risk: the Inc 3 (E1) on-ramp.** The tracker carries real deadlines (e.g.
   June 30, July 8, July 17). Surfacing "due soon / overdue / at-risk" is exactly the forward-looking
   layer (E2 Inc 3, which equals E1). Decide whether v1 surfaces deadlines (a first, contained taste of
   forward-state) or stays status-only and leaves dates to Inc 3. This tracker is the natural motivating
   use case for that layer, so call out the convergence either way.
4. **Item identity.** Key items by the application title. The live checklist reflects current state, so
   identity-by-title is fine for the dashboard snapshot. Only a retrospective "newly submitted" delta
   (if ever wanted) would need the title to stay stable, mirroring the `tasks` collector's KI-6 model.

## The applications project setup (decide and write next session)

A tasks-only / tracker-only project needs **no config-spine code change** (confirmed this cycle:
`repo_path` is required but not git-validated, and a non-`git` collector set already works). Settle:

- `collectors = ["tracker"]` (or `["tasks"]` if the format question lands on extend-`tasks`),
  `checklist = true`, **no git collector**.
- `repo_path = "/Users/yoolee/Developer/applications"`.
- An **absolute** `tasks_file` / tracker-file path
  (`/Users/yoolee/Developer/applications/to_do.md`). Relative collector-file paths resolve against the
  **config directory** (`/Users/yoolee/Developer/Orion`), not the tracked repo, so an absolute path is
  the clean choice here.
- A recipient model given chat-on-hold: a single placeholder recipient satisfies the "at least one
  recipient" rule, and delivery is dashboard-only (the project is pushed via `checklist-push`, not
  `report`, so recipients do not actually deliver anything for it).

## Family dashboard access (the real "share with family")

Now that chat is on hold, the dashboard is how family supervise. Provision each family member as a
relay **viewer** scoped to the applications project, against the **live Fly relay**, with the admin
token:

```
orion relay-user add <family-name> --role viewer --project applications
```

This needs the family members' names/handles and the admin token in `.env`. It is an outbound action
on the production relay, so confirm names and intent first.

## Related / parallel track: `tasks_file` bootstrapping (user-raised)

A broader idea worth pursuing alongside or after the collector:

- **Default `tasks_file` on attach.** When a project is attached to Orion (`add-project`), optionally
  **create a `tasks_file` by default**, so every tracked project has a checklist surface from the
  start. This fixes the "no project has a `tasks_file`" gap structurally rather than one project at a
  time. Today `add-project` wires a `tasks_file` only when `--tasks-file` is passed.
- **Derive content from the project's roadmap.** When a tracked project has an associated roadmap or
  design doc (for **Orion itself**, `plans/orion-plan.md`; for other projects, their own roadmap), the
  `tasks_file` should **reflect that doc** rather than be hand-authored, so the checklist stays in step
  with the roadmap.
- **The shared insight.** This and the status-aware collector are two faces of one capability: **turn a
  rich project document into checklist signal** (a tracker's status fields, or a roadmap's items).
  Design the extraction so both can reuse it. Caveat: deriving a checklist from a freeform roadmap is an
  extraction/generation problem. Prefer a **structured parse** where the source has a consistent shape,
  and reach for an LLM step only where it genuinely must. Settle "parse vs. generate" as its own fork.
  This track can be **deferred** or run **in parallel** with the collector.

## Scope boundary

Surface only the trackable items and their status. The rich free-text (notes, links, compensation,
sub-goal prose) is not for the dashboard. **Reformatting `to_do.md` is explicitly not required** (that
is the entire point of the richer collector).

## Verification (next session)

- Unit tests for the new parser against the **real** `to_do.md` format (status mapping, item
  extraction, the table case if in scope, malformed/odd lines ignored).
- Redaction still runs on every item's text before it leaves the machine (shared `_redacted_checklist`).
- Eyes-on: push via `checklist-push` to the relay, then confirm the applications **project page** shows
  the items with their status, escaped and CSP-clean.
- Access: a provisioned family viewer sees the applications project, a non-granted viewer gets the
  same "does not exist" response (default-deny).
- Live-Fly confirmation after merge + deploy.

## Pointers

- Roadmap: `plans/orion-plan.md` Horizon E table + the E2 ladder (Inc 2.6).
- Built on: Inc 2 (PR #47) + Inc 2.5 (PR #49). Access model: `docs/dashboard-auth.md`. Deploy:
  `docs/deployment.md`. Commands: `docs/commands.md` (`checklist-push`, `relay-user`).

# Orion Architecture

> A from-scratch mental model of how Orion is built, written to be read cold.

## The one fact everything hangs off

Orion is **two decoupled halves that share no code and agree on exactly one contract.**

```
   YOUR MACHINE (local-first)            A SERVER (hosted)
 ┌───────────────────────────┐        ┌──────────────────────────┐
 │  THE PRODUCER  src/orion/  │        │   THE RELAY   relay/     │
 │                            │        │                          │
 │  collectors → summarizer   │        │  HTTP server (stdlib)    │
 │  → redact → compose        │        │  + sqlite store          │
 │  → preview → deliver       │        │  + account/credential auth│
 │                            │        │  + read-only JSON API    │
 └─────────────┬──────────────┘        └────────────┬─────────────┘
               │                                     │
               │   the portable blob (JSON)          │  serves /api/*
               │   POST /ingest  ───────────────────▶│
               │   report.py :: serialize_blob       │
               │                                     ▼
               │                          ┌──────────────────────────┐
        also sends to                     │   THE SPA      web/       │
        Discord / Slack                   │   React + Vite dashboard  │
        (chat channels)                   │   reads /api/*, no logic  │
                                          └──────────────────────────┘
```

The producer and the relay **import none of each other's code.** The producer's
package is `src/orion/`; the relay's is `relay/`. They are wired together by one
thing only: the JSON shape that `serialize_blob` produces and that `POST /ingest`
accepts. Everything else on each side is free to change independently.

Why it is built this way:

- **Local-first is a stage-appropriate choice, not a permanent principle.** Today
  collectors only read local files and only delivery makes outbound calls. As the
  project grows (multi-party, a hosted dashboard), the center of gravity may move
  to the server. Keeping the two halves decoupled behind the blob seam means that
  shift is additive, not a rewrite.
- **The blob is a "portable summary + metadata" object on purpose.** It carries
  everything a receiver needs to store and render a report, and nothing that ties
  it back to the machine that produced it.

---

## Unit 0 — The split and the contract

### The three parts, in one line each

1. **The producer** (`src/orion/`) runs on your machine. It turns local project
   activity into a report and sends it. It is a command-line program, entry point
   `orion.cli:main`.
2. **The relay** (`relay/`) is the hosted service. It receives reports, stores them
   in sqlite, authenticates and scopes every reader, and exposes a read-only JSON API.
   It is a separate Python package that is deliberately **not** part of the installed
   producer wheel. The dependency asymmetry is architectural, not incidental: the relay
   needs `argon2-cffi` to hash passwords, and containing it behind a `relay` extra is what
   keeps the producer install small. The producer never imports it.
3. **The SPA** (`web/`) is a React/Vite dashboard. It holds no business logic. It
   reads the relay's JSON API and renders it. In production the relay serves the
   built SPA files itself (single-host).

### The contract: the portable blob

The single file that defines the seam is **`src/orion/report.py`**. It is small and
worth knowing in full. Three pieces:

- **`ReportBlob`** (a frozen dataclass): the in-memory report. Fields: `project`,
  `participants`, `share_level`, `lane`, `body`, `generated_at`, `orion_version`,
  `sections`, and an optional `checklist`.
- **`build_report(...)`**: assembles a `ReportBlob` from a project config and a
  finished body. It is **lane-agnostic** on purpose. Whether the body came from the
  LLM or was passed straight through, it is built the same way.
- **`serialize_blob(blob) -> str`**: turns the blob into the JSON string that crosses
  the wire. Keys are sorted so the same blob always serializes to the **same bytes**
  (a deterministic wire format). The dict is built field by field, not with
  `dataclasses.asdict`, precisely so the contract reads as a deliberate interface
  rather than a dump of whatever fields the dataclass happens to have.

A few field meanings that matter:

- **`lane`** is provenance: `"raw"` means an LLM summarized at least part of this run,
  `"structured"` means everything passed through untouched. (Unit 2 covers lanes.)
- **`participants`** are named explicitly rather than assuming one implicit "me." That
  is what keeps the door open for multi-supervisor delivery later.
- **`checklist`** uses a three-state convention: `None` means "feature off, omit from
  the wire entirely," an empty list means "enabled but no items" (which tells the relay
  to clear any stale checklist), and a populated list is the live state. This
  None-vs-empty distinction recurs across the wire format.
- **`orion_version`** rides along so a receiver can reject or adapt to a payload from a
  different producer version.

### Why this is the spine

Every other thing in Orion is "upstream of the blob" (the producer building it) or
"downstream of the blob" (the relay and SPA consuming it). If you can hold the blob in
your head, you can place any file you open into one of those two sides. That is the
mental model the rest of this document hangs off.

---

## Unit 1 — The producer spine (`_run_report`)

The entire producer pipeline for one project lives in one function:
**`_run_report`** in `src/orion/cli.py` (starts ~line 1134). Everything else in
`src/orion/` is a helper this function calls. If you know this function, you know the
producer. Read it top to bottom as a sequence of stages.

### The stages, in order

0. **Preview-gate short-circuit (before any work).** If the run is unattended
   (`--yes`) but the project has **not** opted into preview-less sending
   (`auto_send=false`), it stops immediately, before collecting anything, calling any
   LLM, or touching state. This is the first expression of the safety rule: *`--yes`
   alone never sends, and config alone never sends. Both are required.*

1. **Collect, per signal, in config order.** For each enabled collector it reads the
   stored **marker** (where it left off last time), asks the collector for what's new
   since then, and skips it if there's no new activity. Each collector tracks its own
   marker independently, so advancing git doesn't disturb tasks or notes.

2. **Redaction pass 1.** Each collector's raw text is scrubbed for secrets *before it
   goes anywhere* — before the LLM on the raw lane, before merging on the structured
   lane.

3. **The lane decision.** `if result.lane == LANE_RAW:` the text is sent to the LLM to
   be summarized (the summarizer is built lazily here, so a structured-only run never
   needs an API key). `else:` the text passes straight through, no LLM. This one `if`
   is the whole "conditional summarizer" design. (Unit 2 covers which collectors are
   which lane.)

4. **Redaction pass 2, per section.** Every finished section is redacted *again* before
   assembly, so anything that will later land in a Slack/Discord field is
   twice-scrubbed. Sections that are empty after redaction are dropped.

5. **Merge + empty guard.** The surviving sections are merged into the flat `body`. If
   the body is empty after redaction, it **refuses to send** rather than deliver a
   blank report.

6. **Capture the live checklist.** A *separate* read from the collector deltas: the full
   current checklist (open + done), each item redacted. This rides on the relay payload
   only, not the chat message.

7. **Build the full blob.** `build_report(...)` assembles the `ReportBlob` from Unit 0.
   `lane` is `"raw"` if the LLM touched *any* part of this run, else `"structured"`.

8. **Group into audiences and compose (D5).** An "audience" is a `(channel, signal-set)`
   pair. Recipients who share both get the exact same bytes. For each audience it keeps
   only the sections that audience subscribed to, then composes one filtered message.
   The **full** blob is kept separately for the relay — per-recipient filtering applies
   only to chat delivery, never to the dashboard's record.

9. **The preview gate.** By construction, if we reach here with `--yes`, then
   `auto_send` is also true (the not-opted case already returned at stage 0), so this
   is the both-required bypass. Otherwise it always shows the human a preview of each
   audience's message plus the redaction hit count, and waits for confirmation.

10. **Deliver.** Each recipient gets the message composed for its audience. Delivery is
    fail-soft per recipient.

11. **Advance state — only after at least one successful send.** Markers advance (and
    the report is recorded) *only* if at least one delivery succeeded, and only for the
    collectors that had activity. If nothing sent, state is untouched, so the same delta
    is still available next run. (The "≥1 success" rather than "all success" rule is a
    deliberate policy, KI-1: one permanently-broken recipient must not block state
    forever and re-spam the working ones.)

12. **Relay push — last, and fail-soft.** The full blob is pushed to the relay *after*
    state has advanced, and a relay error is swallowed. This ordering guarantees the
    dashboard can never affect the delivered-report outcome or the markers.

### The invariants worth memorizing

Three safety properties are *encoded in the order of these stages*, not bolted on:

- **Two-pass redaction** (stages 2 and 4) — scrub before the LLM, scrub again before send.
- **Advance-only-after-success** (stage 11) — a failed run leaves state exactly as it
  was, so nothing is ever silently lost.
- **The preview gate needs both `--yes` and `auto_send`** (stages 0 and 9) — neither
  alone can bypass the human.

And one structural fact: **the relay always gets the full, unfiltered report; chat
channels get filtered slices.** The full blob and the per-audience blobs are built from
the same redacted sections, so they can never disagree on content, only on *which*
sections are included.

---

## Unit 2 — Collectors and the two lanes

A **collector** is the thing that reads one signal and reports what's new. They all live
in `src/orion/collectors/` and they all return the same four-field contract,
`CollectorResult` (defined in `collectors/__init__.py`):

- `lane` — `LANE_RAW` or `LANE_STRUCTURED` (see below).
- `raw_text` — the collected text.
- `new_marker` — the value to advance state to *if this report sends*.
- `has_activity` — whether there's anything new at all.

Because every collector returns exactly these four fields, the orchestrator never has to
know *which* collector produced the data. That uniformity is the seam that lets a new
signal slot in without touching `_run_report`'s core logic.

### The lane is a property of the *content*, not the command

This is the part that's easy to get backwards. The lane answers one question: **does this
content need narrating, or is it already written?**

- **`LANE_RAW`** — raw activity a person wouldn't want to read as-is, so it needs an LLM
  to narrate it into prose. **Only the git collector is raw.** This is the *only* lane
  that calls Orion's summarizer.
- **`LANE_STRUCTURED`** — content that's already in readable form, so it's passed straight
  through with **no LLM call**: tasks, notes, incubator, tracker, and `intake`.

The lane has nothing to do with CLI-vs-automation or manual-vs-generated. `orion report`
on a git repo runs the *raw* lane fully automatically; a hand-written note runs the
*structured* lane. The rule from the project's principles: *already-written content is
never force-routed through the model.* The LLM is conditional.

### Orion does not read your Claude Code session

A consequence worth stating loudly, because it inverts the obvious guess: **Orion never
parses your Claude Code session transcript.** By design, the external session skill reads
the session, writes the summary *itself*, and hands Orion the finished text through the
`intake` command. Orion passes that through on the **structured** lane. So:

- The summarization of a session happens *outside* Orion, in the Claude Code session's
  own model (Opus, or whatever you're running).
- **Orion's own summarizer (Haiku 4.5) only ever runs on the git raw lane.** Haiku is
  "the narrate-raw-git model," not "the CLI model."

`intake` is the structured lane in its purest form: no collector, no marker, no LLM — the
pushed body *is* the update.

### The two structural shapes of a collector

1. **Report-lane collectors** produce a *delta* keyed off a stored marker (what's new
   since last time). These feed the report pipeline:
   - **git** (`git.py`) — the only raw-lane collector. Shells out to `git` via
     `subprocess`, marker = current HEAD sha. Two collection-time secret defenses: a path
     denylist (`.env`, `*.pem`, `*.key`, `credentials*` never enter the diff) and a diff
     size cap. At `share_level=high_level` the diff is omitted entirely (diffstat only).
   - **tasks** (`tasks.py`) — a Markdown checklist. Reports newly-checked items; marker =
     the JSON list of all currently-completed item texts (delta = current − stored).
   - **notes** (`notes.py`) — one hand-written "current note" file. Marker = a content
     hash, so any edit re-sends the whole note.
   - **incubator** (`incubator.py`) — an idea-pipeline table. Reports new ideas and status
     transitions; marker = an `{idea → status}` map.
   - **tracker** (`tracker.py`) — a status-per-section doc. Produces the *same* checklist
     signal as tasks but from `**Status:**` fields instead of `[ ]` checkboxes.

2. **Dashboard-only signals** don't have a report lane at all. They take a *snapshot* of
   full current state and push it straight to the relay:
   - **disciplines** (`disciplines.py`) — working principles observed in a project's docs.
     They surface as the **"Working agreements"** section on that project's dashboard page
     (Unit 5, 2026-07-13 — reframed from the old standalone Disciplines tab).

   This *does* use an LLM (an extraction step), but note that's a **separate** extractor
   in `extract.py`, not the report summarizer, and it's not part of the raw/structured
   report lanes at all. It's a third path: observe → extract → push to dashboard.

### The marker is the memory

Every report-lane collector is stateless except for its marker. The marker is "where I
left off," and its *type* differs per collector (a sha, a set of done-texts, a content
hash, an idea→status map), but its *role* is identical everywhere: the collector diffs
current-state against the stored marker to compute the delta, and the marker only advances
after a successful send (Unit 1, stage 11). That's how Orion reports each thing exactly
once without a database of "what I've already said."

---

## Unit 3 — Summarizer, redaction, and the safety model

This unit is the one that matters most, because Orion's number-one rule is *never leak a
secret*, and this is where that rule is enforced. Two small files: `redact.py` and
`summarize.py`.

### Redaction (`redact.py`)

A single pure function, `redact(text) -> RedactionResult`, that runs an **ordered list of
regex patterns** over the text and replaces each match with a labeled token. Two design
choices to know:

- **Ordered, specific-first.** High-confidence formats (PEM private keys, AWS/Google/
  GitHub/Slack tokens, `sk-` keys, JWTs) run first so a known shape gets a precise token.
  A generic "a variable whose *name* looks secret-ish, assigned a value" catch-all runs
  **last**, for anything the specific patterns miss. It keeps the name (signal) and
  redacts only the value.
- **Regex, not an ML/entropy detector.** Deliberate: regex is explicit, auditable, fast,
  and dependency-free. For a *safety control* you want to be able to read exactly what is
  and isn't caught. `re.subn` (not `re.sub`) is used so it returns a **hit count** for
  free — that count surfaces in the preview so the human is *told* redaction fired.

Where it runs: pass 1 (pre-LLM), pass 2 (pre-send), on the git denylist at collection,
on every stored history row, and on the checklist / disciplines push lanes. It
scrubs *everything* that leaves the machine or gets persisted.

### The summarizer (`summarize.py`)

Runs **only on the raw lane** (git). Its input is **already redacted** by the caller —
the summarizer itself redacts nothing. Structure:

- **A provider-agnostic seam.** `Summarizer` is a `Protocol` with one method,
  `summarize(text, share_level) -> str`. Two backends implement it:
  - **`AnthropicSummarizer`** (default) — Claude **Haiku 4.5**, the lightest model
    adequate for summarization. A plain non-streaming Messages call.
  - **`LocalSummarizer`** — any OpenAI-compatible `/chat/completions` endpoint (Ollama,
    llama.cpp, LM Studio, vLLM) over stdlib `urllib`, **zero added dependencies**. This
    is the local-first payoff: with a local model, *nothing but delivery* leaves the
    machine.
- **The system prompt is the privacy dial.** A shared base (`_SYSTEM_BASE`: report
  outcomes not code, never reproduce secrets/keys, be factual, clean prose) plus a
  per-share-level instruction: `high_level` = 1–3 sentences, no file or function names;
  `detailed` = may name components and the nature of changes, but still no raw code. Both
  backends send the *same* security prompt (DRY).
- **Fails closed.** Any API failure or empty result becomes a `SummarizerError`, which
  aborts the run *before* sending and *without* advancing state, so a retry re-reports the
  same delta.

### The safety model — the one thing to internalize

Secret-leak defense is **layered (defense in depth)**, and the layers are not equal. From
the code's own headers:

| Layer | What it does | Strength |
|-------|-------------|----------|
| git denylist (collection) | sensitive files never enter the diff at all | strong, but git-only |
| redaction pass 1 (pre-LLM) | scrub before the model sees it | pattern-bounded |
| the LLM prompt | told to report outcomes, not secrets | **weakest — never trusted as a control** |
| redaction pass 2 (pre-send) | scrub again before anything is delivered | pattern-bounded |
| `share_level` | limits how much detail is even requested | policy, not a filter |
| **human preview-before-send** | you see the exact bytes + hit count, and approve | **the guaranteeing layer** |

The honest framing, straight from `redact.py`'s header: *"No pattern set is 100%. This is
ONE layer of defense in depth; the guaranteeing layer is the human preview-before-send."*
Redaction is not a guarantee — it's a strong net that also *raises an alarm* (the hit
count) so the human scrutinizes harder. The **preview is the load-bearing control.** The
LLM is explicitly the *weakest* layer and is never relied on to keep a secret.

This is why the preview gate in Unit 1 (stage 9) is so strict about needing both `--yes`
*and* `auto_send` to skip: skipping the preview removes the *guaranteeing* layer, so it's
gated behind an explicit, deliberate opt-in.


## Unit 4 — Identity on the relay (accounts, credentials, scope)

Everything above is the producer. This unit is the relay's other half: deciding *who is
asking* and *what they may see*. It is worth reading as its own model, because the shape is
not obvious from the endpoints.

### Accounts and credentials are 1:N, and that is the whole idea

```
  relay_users  (the ACCOUNT — durable identity)
    id, name, role, kind, operated_by, active, session_version
        │
        │ 1:N
        ▼
  relay_credentials  (attachable, individually revocable)
    id, user_id, type ('key' | 'password'), label, verifier, active
```

The account is who you are; a credential is one way of proving it. That split is what lets
one person hold keys on two machines under a single identity, lets a password be reset
without disturbing any machine key, and lets a lost key be revoked without logging the human
out everywhere. Before it, identity died with the credential's lifecycle.

Two credential types, two hash strategies, for one reason — the threat differs:

| Type | Held by | Verifier | Why |
| --- | --- | --- | --- |
| `key` | machines, agents | `HMAC-SHA256(pepper, key)` | server-minted, 256-bit — nothing to brute-force, so a slow hash buys nothing |
| `password` | people | Argon2id (own salt + params) | a human picked it, so make guessing expensive |

**One credential never spans both auth worlds.** A password cannot push; a key cannot log in
once its account has a password. And every Bearer key resolves *contributor-bounded* — scoped
to its account's grants regardless of the account's role — so no machine credential ever
carries unrestricted push, not even on an admin account.

### Authorization is one function, re-read every request

`_allowed_projects` is the single place read scope is computed. It returns `None` for
unrestricted (admin, legacy push, open relay) or a set of project names. Two inputs feed it:

- **grants** — explicit per-project rows (`relay_user_projects`), default-deny.
- **visibility** — `relay_project_meta.visibility`, `'org' | 'restricted'`. A `member` reads
  every org-visible project unioned with its grants; every other scoped role reads grants only.

Because scope is resolved per request rather than cached in the session cookie, a grant, a
revoke, or a visibility flip takes effect immediately. The cookie deliberately carries only an
id, a version, and an expiry — never a role or a scope. *Trust the database, not the cookie.*

Out-of-scope always returns a `404` identical to a genuinely missing resource, so a status
code can never enumerate what exists.

### Agents: attribution and grouping point in opposite directions

An agent is an account (`kind='agent'`) whose `operated_by` points at an accountable human.
The two directions are deliberate and easy to conflate:

- **Attribution keeps the agent** — a report is badged as agent work and names its operator,
  so provenance is never lost.
- **Work-tracking folds into the operator** — checklist cards group by
  `operated_by ?? author_id`, so a person plus their agents is one contributor.

The rule that keeps folding honest: it is a **display grouping applied after every
derivation**. Slippage is derived on each raw producer's own observation stream and only
unioned under the operator for display. Folding first would interleave two histories of the
same item and manufacture a signal that never happened.

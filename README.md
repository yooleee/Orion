# Orion

[![CI](https://github.com/yooleee/Orion/actions/workflows/ci.yml/badge.svg)](https://github.com/yooleee/Orion/actions/workflows/ci.yml)

A **local-first** tool that turns your project activity — git commits, a to-do/milestone
checklist, hand-written notes, and pushed updates — into readable progress updates and
delivers them to designated "supervisors" over Discord and Slack. Collectors read your local
files; only delivery makes an outbound call. Reports are previewed in your terminal before
anything is sent.

> **Status: Horizons A & B shipped; Horizon C (hosted) underway.** `orion report <project>` collects from each
> enabled signal (git, tasks, notes), redacts secrets, summarizes only the raw git activity with
> Claude (structured signals skip the LLM), previews the message, and on your confirmation
> delivers it to each recipient's Discord **and/or** Slack webhook — then records what was sent so
> the next run only covers what's new. `orion intake <project>` sends a pushed or hand-written
> update. **Unattended scheduled digests** (`orion report --all --yes` from your OS scheduler;
> see [Scheduling](#scheduling)), **event-driven git-hook triggers**
> (see [Event-driven reports](#event-driven-reports-git-hooks)), and a **Claude Code session
> skill** (see [below](#claude-code-session-skill)) are all available. Reports render as
> **Discord embeds and Slack Block Kit** (with a plain-text fallback). An **optional local relay**
> can additionally store reports and serve a **read-only web dashboard**
> (see [Web dashboard](#web-dashboard-optional-local-relay)); a *hosted, shareable* dashboard is a
> later horizon.

## Supported platforms

Orion runs natively on **Linux, macOS, and Windows 10/11**. All OS-touching work uses only
the Python standard library (`pathlib`, `subprocess`, `sqlite3`), so there is no per-platform
build step and **WSL is not required** on Windows (though it works fine, as a Linux
environment). You need:

- **Python 3.11+** (for the stdlib `tomllib` TOML parser), and
- **`git` available on your PATH**.

> **Tested on:** Linux, Windows 11 + WSL2, and **native macOS** (Apple Silicon, 2026-06-16 —
> full test suite plus a live end-to-end report delivered to Discord and Slack). Native Windows
> is supported by design and smoke-tested as hardware is available — please open an issue if you
> hit a platform-specific quirk.

## Setup

```bash
git clone <your-fork-url> orion && cd orion

# 1. Create a virtual environment (use python3 on macOS/Linux if `python` is missing/Python 2)
python -m venv .venv

# 2. Activate it — this is the one step that differs per OS
source .venv/bin/activate       # macOS / Linux
# .venv\Scripts\activate        # Windows (PowerShell or cmd)

# 3. Install Orion (2 runtime deps: anthropic, python-dotenv)
python -m pip install -e .

# 4. Provide secrets (gitignored — never committed)
cp .env.example .env            # then fill in ANTHROPIC_API_KEY and your webhook URL(s)

# 5. Configure which projects to track
cp orion.toml.example orion.toml   # then edit repo_path and recipients
```

Everything after activation uses `python -m ...`, which is identical on every OS. (On Windows
`cmd`, use `copy` instead of `cp` for steps 4–5.)

Get a **Discord** webhook from a channel's **Settings → Integrations → Webhooks**, a **Slack**
incoming webhook from <https://api.slack.com/messaging/webhooks>, and an Anthropic API key from
<https://console.anthropic.com>. You only need webhooks for the channels you actually use.

### Confirm it works

Two commands tell you the setup is good — a pre-flight check, then a first real report:

```bash
python -m orion check                 # validate config + report send-readiness
python -m orion report <project>      # collect, preview, and (on your y) deliver
```

`check` exits **non-zero** and lists anything missing (it reports each secret **by name** as
`set`/`MISSING`, never by value); fix those and re-run until it's clean. Then `report` shows a
**terminal preview** of exactly what will be sent — confirm with `y` to deliver to your
recipients, or `n` to send nothing (state is left unchanged, so the same activity stays
reportable). Re-running immediately reports "no new activity" — that round-trip is your
confirmation the pipeline works end to end. For a clone-to-first-report walkthrough (including
the optional web dashboard), see [**docs/new-project-setup.md**](docs/new-project-setup.md).

## Usage

With the virtual environment activated (see Setup), run:

```bash
python -m orion report <project>
```

`python -m orion` is the portable invocation — it works identically on every OS, regardless
of where the venv puts the launcher (`.venv/bin/` vs `.venv\Scripts\`). The bare `orion`
console script works too once the venv is active; the docs use `python -m orion` because it's
the same everywhere.

This runs the full pipeline for the named project (from `orion.toml`):

1. **Collect** from each enabled signal since the last report — git activity (commit messages
   + diffstat, plus a capped code diff if `share_level = "detailed"`), newly-completed
   checklist items, and changed notes.
2. **Redact** obvious secrets from the collected text.
3. **Summarize** only the raw git activity with an LLM (the one step that calls out) — Claude
   Haiku 4.5 by default, or a model/provider of your choice including a local model (see
   [Summarizer backend](#summarizer-backend)); structured signals (tasks, notes) are already
   report-ready and skip the LLM.
4. **Merge** the signals into one report, **redact again** as a safety net, then **preview** it
   in your terminal (one block per channel when you have both Discord and Slack recipients).
5. On your `y` confirmation, **deliver** to each recipient — rendered for their channel (a
   Discord **embed** or a Slack **Block Kit** message, each with a plain-text fallback, falling
   back to a plain message if a report is too large for the channel's structured limits) and
   POSTed to their webhook.
6. **Record** the report so the next run only covers what's new.

If there's nothing new, it says so and sends nothing. If you decline the preview, nothing is
sent and state is left unchanged, so the same activity is still reportable.

To report on **every** project in one go, use `--all` instead of a project name:
`python -m orion report --all`. For running Orion **unattended** on a schedule (where no one is
present to confirm the preview), add `--yes` — but a project is only ever sent without the
preview if it has also opted in with `auto_send = true`. See [Scheduling](#scheduling).

To send a pushed or hand-written update (no collectors, no LLM — the same entry point the Claude
Code session skill uses), use `intake`:

```bash
python -m orion intake <project> -m "Your update."   # or pipe the body on stdin
```

`intake` previews before sending like `report` does. Add `--yes` to skip that preview and send
non-interactively (used by the session skill below, which shows the summary for approval in the
session first); redaction still runs either way. See
[Claude Code session skill](#claude-code-session-skill).

To have a project report itself **automatically when you commit or push**, install a git hook
(see [Event-driven reports](#event-driven-reports-git-hooks)):

```bash
python -m orion install-hook <project>               # default: a pre-push hook
```

## Configuration (`orion.toml`)

```toml
state_db = "orion.sqlite3"        # relative paths resolve next to this file

[projects.orion]
repo_path   = "/home/you/orion"   # local git repo to read
share_level = "high_level"        # "high_level" (no code diff) | "detailed" (capped diff)
auto_send   = false               # unattended-send opt-in; needs `--yes` too (see Scheduling)
collectors  = ["git", "tasks", "notes"]   # any of: git, tasks, notes
tasks_file  = "TODO.md"           # required when "tasks" is enabled (a Markdown checklist)
notes_file  = "NOTES.md"          # required when "notes" is enabled (a hand-written update)

  [[projects.orion.recipients]]
  name            = "Alex (supervisor)"
  channel         = "discord"     # "discord" or "slack"
  webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"   # names the .env key holding the URL

  [[projects.orion.recipients]]
  name            = "Sam (supervisor)"
  channel         = "slack"
  webhook_env_var = "ORION_SLACK_WEBHOOK_SAM"
```

The config never contains a secret: each recipient names an **environment variable** that
holds its webhook URL, and the URL lives only in `.env`.

> **Windows paths:** a backslash is an escape character in TOML, so write file paths either
> with forward slashes — `repo_path = "C:/Users/you/orion"` (forward slashes work fine on
> Windows) — or as a single-quoted *literal* string — `repo_path = 'C:\Users\you\orion'`.
> A double-quoted `"C:\Users\..."` will be misread because `\U` starts an escape.

### Summarizer backend

The one step that calls an LLM — summarizing raw git activity — is configurable, but the
default needs no setup. With **no `[summarizer]` table**, Orion uses Anthropic's **Claude
Haiku 4.5** (the lightest model adequate for the job). The choice is **global** (one
summarizer for all projects); structured signals (tasks, notes, intake) never call an LLM
either way.

To step up to a stronger Anthropic model (only if Haiku misses nuance on your real diffs):

```toml
[summarizer]
provider = "anthropic"
model    = "claude-sonnet-4-6"
```

To summarize with a **local model** — so the activity never leaves your machine (only
delivery makes an outbound call) — point `base_url` at any **OpenAI-compatible** chat
endpoint and name the local model:

```toml
[summarizer]
provider = "local"
base_url = "http://localhost:11434/v1"   # e.g. Ollama; also llama.cpp / LM Studio / vLLM
model    = "llama3.1:8b"                  # pick a CAPABLE model — see note below
# api_key_env = "LOCAL_LLM_KEY"           # only if the endpoint requires a key (most don't)
```

**Choosing a local model:** summarizing real diffs needs a reasonably capable model — treat the
cloud default, **Haiku 4.5**, as the quality bar. Very small models degrade noticeably (a 0.5B
model in testing produced rough, partly hallucinated summaries), so prefer a capable instruct
model (≈7–8B or better). This is "lightest **adequate**": small and fast is good, but
adequate-for-quality comes first.

Orion targets the OpenAI-compatible `/chat/completions` shape because it is the common
denominator across local runtimes (Ollama exposes it at `/v1`), so one code path serves all
of them — you just point `base_url` at the right endpoint. Redaction and preview-before-send
are identical for every backend; a local model is simply more private. (Why this shape and
not a runtime's native API, and when that might change: see
[`docs/known-issues.md`](docs/known-issues.md) KI-16.)

### Inspecting your config

Three **read-only** commands let you see what's configured (Orion never *writes* the config —
you change it by editing `orion.toml`):

```bash
python -m orion projects          # list every project: auto_send, share level, channels
python -m orion show <project>    # one project's resolved config (paths, flags, recipients)
python -m orion check             # validate the config and report send-readiness
```

`check` is a pre-flight gate: it validates the config, then reports whether each project is
actually ready to send — the git repo path exists, each recipient's webhook secret is present,
and the summarizer's key is set when the git lane is in use (which key depends on the backend:
`ANTHROPIC_API_KEY` for Anthropic, the named `api_key_env` for a keyed local endpoint, or none
at all for a keyless local one). It reports secrets **by name** as `set`/`MISSING`, never by
value, and exits non-zero if anything required is missing. None of these commands print a secret
value (the config holds env-var *names* and paths, not secrets).

## Web dashboard (optional local relay)

Orion can **also** push each report to a small local **relay** that stores it and serves a
**read-only web dashboard** — *in addition to* your Discord/Slack delivery, which is unchanged.
It is **opt-in and additive**: with no `[relay]` table in your config, nothing changes.

Enable it by adding a `[relay]` table (an ingest URL + the name of an `.env` variable holding a
shared Bearer token), then run the relay in its own terminal:

```bash
orion relay-serve                 # serves ingest + dashboard at http://127.0.0.1:8787
```

A subsequent `orion report` / `orion intake` delivers as usual **and** pushes the report to the
relay, which you can browse at `http://127.0.0.1:8787` (projects → history → one report). The
push is **fail-soft** — a relay that is down or misconfigured never blocks or fails a delivered
report. At this stage the relay binds **loopback only** (`127.0.0.1`), so the dashboard is for
your own machine; your supervisors still see the Discord/Slack delivery. See
[**docs/new-project-setup.md**](docs/new-project-setup.md) for a full walkthrough.

## Scheduling

Orion has **no built-in scheduler** — to deliver digests on a cadence, hand the one-shot
command to your OS's own scheduler (cron, a systemd timer, launchd, or Task Scheduler). The
unattended command is:

```bash
python -m orion report --all --yes
```

- `--all` reports on every project in the config.
- `--yes` allows the terminal preview to be skipped — but **only** for projects with
  `auto_send = true`. A project without `auto_send` is **skipped and logged, never sent**, even
  under `--yes`; and **without** `--yes` every run previews as usual. Both are required, so a
  scheduled run can never deliver a project you didn't explicitly opt in (defense in depth).
- Each run already reports only what's new, so a project with no activity sends nothing — a
  daily job is naturally a daily digest.
- The run exits non-zero **only on a real failure**, so your scheduler alerts on genuine
  problems, not on routine "nothing to send" runs.

Redaction is unchanged on this path: both passes still run, so unattended delivery relaxes no
secret-scrubbing — it only skips the *human* preview, for opted-in projects.

Per-OS setup (cron / systemd timer / launchd / Task Scheduler), the **WSL2 caveat** (cron inside
WSL2 fires only while a WSL session is open — for reliable scheduling on Windows, prefer the
**native Task Scheduler** path, which Orion documents), and the minimal-environment gotchas (use
absolute paths, the venv's own Python, and ensure `git` is on PATH) are in
[`docs/scheduling.md`](docs/scheduling.md).

## Event-driven reports (git hooks)

Orion doesn't watch your repo — but you can have git run it for you on a commit or push:

```bash
python -m orion install-hook <project>          # default: pre-push (fires on `git push`)
python -m orion install-hook <project> --hook post-commit   # or fire on every commit
python -m orion install-hook <project> --print  # review the hook script without installing
```

This installs a small `#!/bin/sh` hook that runs `report <project> --yes` in the background and
**always exits 0**, so it never delays or blocks your `git commit`/`git push`. Like scheduled
runs, it only delivers projects with **`auto_send = true`** (others are skipped) — so a hook can
never send a project you didn't opt in. Output goes to `<repo>/.git/orion-hook.log`. One portable
hook works on all three OSes (git runs hooks under `sh`, which Git for Windows bundles).

Full details — `pre-push` vs `post-commit`, reviewing/replacing/removing a hook, coexistence with
hook managers — are in [`docs/git-hooks.md`](docs/git-hooks.md).

## Claude Code session skill

Orion's fourth signal — your **coding sessions** — arrives as a *pushed summary*, not by Orion
parsing session files. A bundled [Claude Code skill](skills/orion-session/SKILL.md) summarizes
the current session and sends it to a project's supervisor(s) via `intake`:

```sh
cp -r skills/orion-session ~/.claude/skills/    # install once (per-user)
```

Then, in any coding session, ask Claude to "send a progress update to Orion for `<project>`."
Claude writes an outcome-focused, secret-free summary, **shows it to you for approval in the
session**, and on your OK sends it with `intake --yes`. That in-session review is the human gate
(which is why the send is non-interactive); Orion still redacts before delivering, and **Orion
does not re-summarize** — the summary you approve is what's sent. Setup and details are in
[`skills/README.md`](skills/README.md).

## Privacy & security

Leak prevention is the top priority. Defense in depth:

- **Sensitive files are never read into the diff** (`.env`, `*.pem`, `*.key`, `id_rsa*`,
  `credentials*`, `*secret*`, …) — excluded at collection time.
- **`share_level = "high_level"`** (the default) sends no code diff at all — only commit
  messages and a diffstat.
- **Two redaction passes** scrub API keys, tokens, private keys, JWTs, and secret-ish
  assignments — once before the LLM, once before sending.
- **Preview-before-send** is always on; the preview shows how many secrets were redacted.
- The state DB stores only the redacted report text; `.env`, `orion.toml`, and `*.sqlite3`
  are gitignored.

No redactor is perfect — the terminal preview is the human gate that makes the guarantee.

## Development

With the virtual environment activated:

```bash
python -m pip install -e ".[dev]"   # adds pytest
python -m pytest                    # run the test suite
```

What the suite covers and why — plus what's intentionally *not* covered — is documented in
[`docs/testing.md`](docs/testing.md). The manual cross-OS checks (native Windows/macOS) live
in [`docs/portability-smoke-test.md`](docs/portability-smoke-test.md).

## Contributing

Contributions are welcome. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for how to set up a dev
environment, run the test suite, and the conventions to follow (cross-platform, minimal
dependencies, secret-safe).

## Security

Orion is built around leak prevention (see [Privacy & security](#privacy--security)). If you
find a vulnerability, please **do not** open a public issue — follow the private disclosure
process in [`SECURITY.md`](SECURITY.md).

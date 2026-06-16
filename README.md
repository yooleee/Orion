# Orion

A **local-first** tool that turns your project activity — git commits, a to-do/milestone
checklist, hand-written notes, and pushed updates — into readable progress updates and
delivers them to designated "supervisors" over Discord and Slack. Collectors read your local
files; only delivery makes an outbound call. Reports are previewed in your terminal before
anything is sent.

> **Status: Horizon A shipped; Horizon B underway.** `orion report <project>` collects from each
> enabled signal (git, tasks, notes), redacts secrets, summarizes only the raw git activity with
> Claude (structured signals skip the LLM), previews the message, and on your confirmation
> delivers it to each recipient's Discord **and/or** Slack webhook — then records what was sent so
> the next run only covers what's new. `orion intake <project>` sends a pushed or hand-written
> update. **Unattended scheduled digests** (`orion report --all --yes` from your OS scheduler;
> see [Scheduling](#scheduling)), **event-driven git-hook triggers**
> (see [Event-driven reports](#event-driven-reports-git-hooks)), and a **Claude Code session
> skill** (see [below](#claude-code-session-skill)) are all available. Richer message formatting
> and a multi-party/hosted dashboard are later horizons.

## Supported platforms

Orion runs natively on **Linux, macOS, and Windows 10/11**. All OS-touching work uses only
the Python standard library (`pathlib`, `subprocess`, `sqlite3`), so there is no per-platform
build step and **WSL is not required** on Windows (though it works fine, as a Linux
environment). You need:

- **Python 3.11+** (for the stdlib `tomllib` TOML parser), and
- **`git` available on your PATH**.

> **Tested on:** Linux and Windows 11 + WSL2 (active development). macOS and native Windows
> are supported by design and smoke-tested as hardware is available — please open an issue if
> you hit a platform-specific quirk.

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
3. **Summarize** only the raw git activity with Claude Haiku 4.5 (the one step that calls out);
   structured signals (tasks, notes) are already report-ready and skip the LLM.
4. **Merge** the signals into one report, **redact again** as a safety net, then **preview** it
   in your terminal (one block per channel when you have both Discord and Slack recipients).
5. On your `y` confirmation, **deliver** to each recipient — formatted for their channel and
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

Per-OS setup (cron / systemd timer / launchd / Task Scheduler), the **WSL2 caveat** (cron runs
only while WSL is running), and the minimal-environment gotchas (use absolute paths, the venv's
own Python, and ensure `git` is on PATH) are in [`docs/scheduling.md`](docs/scheduling.md).

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

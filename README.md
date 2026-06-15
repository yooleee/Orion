# Orion

A **local-first** tool that turns your project activity — git commits, a to-do/milestone
checklist, hand-written notes, and pushed updates — into readable progress updates and
delivers them to designated "supervisors" over Discord and Slack. Collectors read your local
files; only delivery makes an outbound call. Reports are previewed in your terminal before
anything is sent.

> **Status: Phases 1–3 (the on-demand reporting core).** `orion report <project>` collects
> from each enabled signal (git, tasks, notes), redacts secrets, summarizes only the raw git
> activity with Claude (structured signals skip the LLM), previews the message, and on your
> confirmation delivers it to each recipient's Discord **and/or** Slack webhook — then records
> what was sent so the next run only covers what's new. `orion intake <project>` sends a
> pushed or hand-written update. Scheduled digests, git-hook triggers, and the Claude-session
> skill are later phases.

## Setup

Requires **Python 3.11+** and **git**.

```bash
git clone <your-fork-url> orion && cd orion

# 1. Create a virtual environment and install (2 runtime deps: anthropic, python-dotenv)
python3 -m venv .venv
.venv/bin/pip install -e .

# 2. Provide secrets (gitignored — never committed)
cp .env.example .env          # then fill in ANTHROPIC_API_KEY and your webhook URL(s)

# 3. Configure which projects to track
cp orion.toml.example orion.toml   # then edit repo_path and recipients
```

Get a **Discord** webhook from a channel's **Settings → Integrations → Webhooks**, a **Slack**
incoming webhook from <https://api.slack.com/messaging/webhooks>, and an Anthropic API key from
<https://console.anthropic.com>. You only need webhooks for the channels you actually use.

## Usage

```bash
.venv/bin/orion report <project>
```

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

To send a pushed or hand-written update (no collectors, no LLM — the same entry point a future
Claude-session skill will use), use `intake`:

```bash
.venv/bin/orion intake <project> -m "Your update."   # or pipe the body on stdin
```

## Configuration (`orion.toml`)

```toml
state_db = "orion.sqlite3"        # relative paths resolve next to this file

[projects.orion]
repo_path   = "/home/you/orion"   # local git repo to read
share_level = "high_level"        # "high_level" (no code diff) | "detailed" (capped diff)
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

```bash
.venv/bin/pip install -e ".[dev]"   # adds pytest
.venv/bin/python -m pytest          # run the test suite
```

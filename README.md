# Orion

A **local-first** tool that turns your git activity into readable progress updates and
delivers them to designated "supervisors" over Discord. Collectors read your local repos;
only delivery makes an outbound call. Reports are previewed in your terminal before anything
is sent.

> **Status: Phase 1 (MVP).** `orion report <project>` reads git activity since the last
> report, redacts obvious secrets, summarizes it with Claude, previews the message, and on
> your confirmation posts it to a Discord webhook — then records what was sent so the next
> run only covers what's new. Slack, to-do/notes signals, scheduling, and the Claude-session
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

Get a Discord webhook URL from a channel's **Settings → Integrations → Webhooks**, and an
Anthropic API key from <https://console.anthropic.com>.

## Usage

```bash
.venv/bin/orion report <project>
```

This runs the full pipeline for the named project (from `orion.toml`):

1. **Collect** git activity since the last report (commit messages + diffstat, plus a capped
   code diff if `share_level = "detailed"`).
2. **Redact** obvious secrets from the raw activity.
3. **Summarize** the redacted activity with Claude Haiku 4.5 (the only step that calls out).
4. **Redact again** as a safety net, then **preview** the message in your terminal.
5. On your `y` confirmation, **POST** to each recipient's Discord webhook.
6. **Record** the report so the next run only covers new commits.

If there's nothing new, it says so and sends nothing. If you decline the preview, nothing is
sent and state is left unchanged, so the same activity is still reportable.

## Configuration (`orion.toml`)

```toml
state_db = "orion.sqlite3"        # relative paths resolve next to this file

[projects.orion]
repo_path   = "/home/you/orion"   # local git repo to read
share_level = "high_level"        # "high_level" (no code diff) | "detailed" (capped diff)
collectors  = ["git"]             # Phase 1: only "git"

  [[projects.orion.recipients]]
  name            = "Alex (supervisor)"
  channel         = "discord"     # Phase 1: only "discord"
  webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"   # names the .env key holding the URL
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

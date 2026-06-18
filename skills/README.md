# Orion skills

Separable [Claude Code skills](https://docs.claude.com/en/docs/claude-code/skills) that ship
with Orion but live outside the Python package — install the ones you want into your Claude Code
skills directory. They are optional: everything they do can also be done by hand with the `orion`
CLI.

## `orion-session` — push a session summary to Orion

At the end (or any point) of a Claude Code coding session, this skill summarizes the session and
sends it to a project's supervisor(s) via `orion intake`. Claude writes the summary, shows it to
you for approval **in the session**, and on your OK sends it non-interactively (`intake --yes`);
Orion redacts and delivers it. This is the "session signal" — the same structured-lane intake a
hand-written update uses, so Orion never has to parse session files.

### Install

Copy (or symlink) the skill into your Claude Code skills directory:

```sh
# Available in every project (recommended):
cp -r skills/orion-session ~/.claude/skills/

# …or just within one project:
cp -r skills/orion-session <that-project>/.claude/skills/
```

On Windows, copy the `skills\orion-session` folder into `%USERPROFILE%\.claude\skills\`.

### Prerequisites (one-time)

- **Orion installed and reachable** — either the `orion` console script on your PATH, or know
  the path to its venv Python (`<orion>/.venv/bin/python -m orion`).
- **A configured `orion.toml`** with the target `[projects.<name>]` and its recipients, and a
  **`.env` beside it** holding the webhook URL(s) (and the Anthropic key, though intake doesn't
  use the LLM). The skill passes `--config <that orion.toml>`; Orion finds the `.env` next to it.

### Use

In a session, ask Claude to "send a progress update to Orion for `<project>`" (or just "update my
supervisor"). Claude will draft the summary, show it for your approval, and send it on your OK.
You can always edit the draft before approving, or decline to send nothing.

## `orion-dashboard` — open the local dashboard

Starts Orion's local **relay** (the small server that stores pushed reports and serves a read-only
web dashboard) if it isn't already running, and opens it in your browser — so you skip running
`orion relay-serve` and navigating there by hand. Claude checks whether it's already up (and won't
start a second one), launches it in the background anchored to a stable store beside your
`orion.toml`, waits for it, then opens `http://127.0.0.1:8787`.

> **Local-only convenience.** The dashboard binds loopback (`127.0.0.1`), so it is for your own
> machine. Auto-starting a server only makes sense while the dashboard is local; once Orion gains a
> *hosted* dashboard, "open the dashboard" is just opening a URL and this skill becomes a bookmark.

### Install

```sh
# Available in every project (recommended):
cp -r skills/orion-dashboard ~/.claude/skills/

# …or just within one project:
cp -r skills/orion-dashboard <that-project>/.claude/skills/
```

On Windows, copy the `skills\orion-dashboard` folder into `%USERPROFILE%\.claude\skills\`.

### Prerequisites (one-time)

- **Orion installed and reachable** — the `orion` console script on your PATH, or the path to its
  venv Python (`<orion>/.venv/bin/python -m orion`).
- **An `ORION_RELAY_TOKEN`** in the `.env` beside your `orion.toml` (the relay's ingest token).
- **For the dashboard to show anything:** `[relay]` **enabled** in `orion.toml` (with `url`
  pointing at the same host/port) and at least one `orion report`/`orion intake` run since enabling
  it — the dashboard shows only what has been pushed.

### Use

Ask Claude to "open my Orion dashboard" (or "launch/show the dashboard"). Claude starts the relay
if needed and opens it in your browser. To stop it, ask Claude to stop the relay, or kill the
background process yourself. You can always do it by hand instead: run `orion relay-serve` in a
terminal and open `http://127.0.0.1:8787`.

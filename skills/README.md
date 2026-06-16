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

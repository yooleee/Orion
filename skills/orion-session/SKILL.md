---
name: orion-session
description: >-
  Summarize the current Claude Code coding session and send it as a progress
  update to the user's supervisor(s) via Orion. Use when the user asks to report
  progress, update a supervisor, "send this to Orion", or push a session summary.
  The user must already have Orion installed and configured (an orion.toml with
  the target project and recipients, and a .env with the webhook URL(s)).
---

# Orion — session progress update

This skill turns what just happened in a coding session into a short progress
update and pushes it to Orion, which delivers it to the project's configured
supervisor(s) over Discord/Slack. **You (Claude) write the summary — Orion does
not re-summarize it**; it only redacts and delivers exactly what you send. So
write it well, and keep it safe.

Follow these steps in order.

## 1. Identify the target

You need two things to send:

- **The Orion project name** — the `[projects.<name>]` key in the user's
  `orion.toml`. If the user didn't say which project, **ask** (don't guess).
- **The path to their `orion.toml`** — needed because the skill runs from the
  current repo, not Orion's directory. **If the `ORION_CONFIG` environment variable
  is set**, Orion uses it automatically, so you can **omit `--config` entirely** (and
  skip asking). Otherwise, if you don't already know the path from this session, ask
  for the absolute path (commonly `~/orion/orion.toml`). Either way, Orion finds the
  matching `.env` next to that config automatically.

## 2. Write the summary

Summarize **this session's** work as a supervisor-facing progress update:

- **Outcomes, not mechanics** — what was accomplished and why it matters, not a
  blow-by-blow of commands. A few sentences or a short bullet list is ideal.
- **Audience** — a supervisor who is not necessarily deep in the code. Plain,
  concise, honest (include in-progress / blocked items if relevant).
- **Privacy is non-negotiable.** Never include secrets, API keys, tokens,
  `.env` contents, credentials, or raw code/file contents. Report progress, not
  internals. (Orion redacts as a safety net, but that is a backstop — do not rely
  on it; simply don't write secrets.)

## 3. Show it and get explicit approval

**Display the drafted summary to the user and ask whether to send it** (to which
project, over which they can confirm the recipients are right). This in-session
review is the human gate — it replaces Orion's terminal preview, which is why we
send non-interactively in the next step.

- If the user wants changes, revise and show again.
- Only proceed on a clear "yes". If they decline, **send nothing** and stop.

## 4. Send it via Orion intake

On approval, pipe the summary into `orion intake … --yes` (stdin avoids
multi-line quoting issues). Use the project and config path from step 1:

```sh
printf '%s' "<the approved summary>" | orion intake <project> --config <abs path to orion.toml> --yes
```

- `--yes` skips Orion's terminal preview (you already showed the summary for
  approval). **Redaction still runs** on the way out.
- If `orion` is not on PATH, use the venv's Python instead:
  `… | <abs path>/.venv/bin/python -m orion intake <project> --config <abs path to orion.toml> --yes`
  (on Windows, `…\.venv\Scripts\python.exe`).

## 5. Report the result

Relay Orion's output to the user:

- Success prints `Sending '<project>' (preview skipped: --yes).` then
  `Sent to: <recipients>.` — tell the user it was delivered and to whom.
- On any error (e.g. `Error: …`, `No deliveries succeeded`), show it and stop;
  do not retry blindly. A common cause is a missing secret — Orion names the
  exact variable (it never prints the value).

## Notes

- **One project per send.** To update several projects, repeat the steps per
  project.
- **This is a push, not a delta.** Running it twice sends twice (intake tracks no
  state). Send once per update you want delivered.
- **Hand-written alternative.** The user can always skip this skill and run
  `orion intake <project> -m "…"` themselves (which previews in their terminal);
  this skill is convenience, not a requirement.

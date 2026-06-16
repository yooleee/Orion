<!-- =========================================================================
git-hooks.md
---------------------------------------------------------------------------
Responsible for: The runbook for Orion's event-driven trigger (Phase B1) — a git
                 hook that fires `orion report <project> --yes` automatically when
                 you commit or push.
Role in project: Orion does not WATCH git; a git hook is what runs Orion on a git
                 event. This documents the `orion install-hook` command and the
                 hook it installs, with the cross-platform and safety details.
Companion: cadence-based runs (a scheduler) are in `scheduling.md`; the two are
           complementary (push-driven vs time-driven) and can be used together.
========================================================================= -->

# Event-driven reports (git hooks)

Orion has **no daemon and does not watch your repo**. To report *automatically* as you work,
install a **git hook** — git runs it on a git event (a commit or a push), and the hook runs
Orion. This is the event-driven counterpart to the time-driven [`scheduling.md`](scheduling.md);
you can use either or both.

```
orion install-hook <project>
```

That installs a **pre-push** hook (the default) into the project's repo. From then on,
`git push` fires `orion report <project> --yes` in the background.

> **You must opt the project in.** The hook runs `report --yes`, which only delivers projects
> with **`auto_send = true`** in `orion.toml`. Without it, the hook still runs but **skips
> sending** (nothing is delivered) — the command warns you when you install onto a project that
> isn't opted in. This is the same defense-in-depth gate as scheduled runs: a hook can never
> deliver a project you didn't explicitly opt in. Prefer `share_level = "high_level"` for
> hook-driven projects.

## What the installed hook does

The hook is a tiny, portable `#!/bin/sh` script. View the exact script before installing with
`--print`:

```sh
#!/bin/sh
# Orion pre-push hook — installed by `orion install-hook`.
# Fire-and-forget: runs the report in the background and always exits 0,
# so it never delays git or (for pre-push) aborts the push on an error.
"/abs/path/.venv/bin/python" -m orion report "myproject" --yes --config "/abs/path/orion.toml" >> "/abs/path/.git/orion-hook.log" 2>&1 &
exit 0
```

Three deliberate properties:

- **Background + always `exit 0`.** Orion makes an LLM call and a network POST, which take a few
  seconds. The hook runs that in the background (`&`) and exits 0 immediately, so it **never
  delays `git commit`/`git push`** and — important for `pre-push` — **never aborts a push** if a
  report fails.
- **Absolute paths, baked in.** A hook runs in git's minimal environment (no activated venv,
  possibly a stripped `PATH`). So the script embeds the **venv's own Python** (`sys.executable`),
  the **absolute config path**, and a log path — it does not rely on `orion` being on `PATH` or
  on the current directory.
- **Secrets are found via the config.** A hook runs with its working directory set to the
  *tracked* repo, not Orion's directory — so Orion loads the `.env` **next to the `orion.toml`**
  it was given (`--config`), in addition to the usual working-directory search. Keep your `.env`
  beside `orion.toml` (its normal home) and the hook finds your webhook/API-key secrets with no
  extra setup. If you keep secrets only as exported environment variables instead, those work too
  (they take precedence). The webhook is always needed to deliver; the LLM key only for the git
  (raw) lane — a project that never summarizes git activity needs no API key.
- **A log to look at.** Because it's fire-and-forget, output goes to **`<git-dir>/orion-hook.log`**
  (e.g. `.git/orion-hook.log`). That's where you look if a hook "seemed to do nothing."

## `pre-push` vs `post-commit`

Both are supported (`--hook pre-push` / `--hook post-commit`); pick by how often you want updates:

| Hook | Fires on… | Good when |
|---|---|---|
| **`pre-push`** (default) | `git push` | You want an update when you *share* work — it batches the commits in the push, so it's far less noisy. Matches the "I've done something worth reporting" moment. |
| `post-commit` | every `git commit` | You want the finest granularity. **Noisier:** one report per commit for an opted-in project. |

> *(Git has no client-side `post-push` hook — `pre-push` and `post-commit` are the local
> options. `pre-push` fires just before the push completes; our hook still exits 0, so it never
> blocks the push.)*

## Per-OS notes

A single `#!/bin/sh` script works on **all three OSes** — git runs hooks under `sh` everywhere,
and **Git for Windows bundles its own `sh`**, so no per-OS hook variants are needed (unlike
scheduling). Two details the command handles for you:

- **Windows paths** are embedded with forward slashes (`C:/Users/...`), because the hook runs
  under `sh` where a backslash is an escape character.
- The hook is written with `\n` line endings (never CRLF), so the shebang is valid under `sh`.

On macOS/Linux the hook file is marked executable; on Windows that bit is irrelevant (git runs
hooks via its bundled `sh` regardless).

## Reviewing, replacing, and removing

- **Review first:** `orion install-hook <project> --print` prints the script and writes nothing.
- **No silent clobbering:** if a hook of that name already exists, `install-hook` refuses and
  tells you. Re-run with `--force` to replace it.
- **Uninstall:** delete the hook file — `rm <repo>/.git/hooks/pre-push` (or `post-commit`).

### Using a hook manager (husky, pre-commit, …)

`install-hook` writes a single standalone hook file and won't overwrite an existing one, so if
your repo already uses a hook manager, either let that manager call Orion (add the one
`report --yes` line from `--print` to your managed hook) or point the manager's hooks path
elsewhere. Orion deliberately doesn't try to chain hooks. See
[`known-issues.md`](known-issues.md) (KI-14).

## Verifying it works

1. Install: `orion install-hook <project>` (with `auto_send = true` set for that project).
2. Make a commit and `git push` (for the default `pre-push` hook). The push returns immediately.
3. Check **`<repo>/.git/orion-hook.log`** — you should see `Auto-sending '<project>'` and
   `Sent to: …` (or `No new activity` if there's nothing new), and the update should arrive in
   your channel. If the project isn't opted in, the log shows `Skipping '<project>': … auto_send
   is not enabled`.

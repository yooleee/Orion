<!-- =========================================================================
portability-smoke-test.md
---------------------------------------------------------------------------
Responsible for: The MANUAL, per-OS checks that confirm Orion runs natively on
                 macOS and Windows — the validation the automated suite can't do
                 from a single machine.
Role in project: The runbook to follow when a second OS becomes available. After
                 running it, update the README "Tested on" line with what passed.
Companion: the automated suite is mapped in `testing.md`.
========================================================================= -->

# Cross-OS smoke test

The automated suite (`python -m pytest`, see [`testing.md`](testing.md)) proves logic, but it
runs on one machine. Orion targets **native Windows, macOS, and Linux**, and Phase 3.5 added
OS-sensitive surfaces — the `python -m orion` entry point, per-OS venv activation, the console
UTF-8 guard, and Windows TOML paths. This runbook validates those on real hardware.

**Already covered — no action:** Linux, **Windows 11 + WSL2** (which *is* Linux), and **native
macOS** (verified 2026-06-16). The environment that still needs real-hardware validation is
**native Windows** (cmd / PowerShell).

> **Tool-agnostic by design.** The steps below describe *what* to check, not which package
> manager to use — reach for whatever you normally use (`pip` + `venv`, `uv`, Poetry, Conda, …).
> Only the commands that are *themselves* the thing being tested are given literally: the
> `python -m orion` entry point, per-OS activation paths, output redirection, and the TOML
> `repo_path` forms. Everything else is described by intent so the runbook doesn't assume a
> toolchain.

## Shared setup (any toolchain)

On each OS, before its OS-specific checks, get to a working install:

1. **Prerequisites** — confirm Python 3.11+ (needed for the stdlib `tomllib` parser) and that
   `git` is on PATH.
2. **Create and activate a virtual environment** with your tool of choice. Activation is the
   one step that genuinely differs per OS (see each section) — the environment lands under
   `.venv/bin/` on macOS/Linux and `.venv\Scripts\` on Windows, and confirming that layout
   works on this OS is part of the point. (If your tool runs commands without an explicit
   activation step, that's fine — the check is just that the entry point below works.)
3. **Install Orion editable with its dev extras** — the equivalent of an editable (`-e`)
   install of `.[dev]`, which also pulls in `pytest`.
4. **Run the test suite** — expect all green. The current expected count lives in
   [`testing.md`](testing.md), so it isn't duplicated (and can't go stale) here.
5. **Confirm the entry point** — `python -m orion --help` prints usage and exits 0.

`python -m orion` is the portable invocation: it behaves the same regardless of OS or which
tool installed the package, so every per-phase check below uses it.

---

## A. macOS — low risk (mostly Linux-like)

After the shared setup, do one real run against any local git repo you've added to `orion.toml`:

- **`python -m orion report <project>`** — the preview renders, the `⚠`/`✗` glyphs display;
  decline (`n`) so nothing is sent.
- **The same command with stdout + stderr redirected to a file** (your shell's redirection,
  e.g. `> out.txt 2>&1`) — must NOT crash.

macOS streams default to UTF-8, so the console-Unicode guard is a no-op here; this mainly
confirms per-OS activation, the entry point, and the suite.

> **Verified 2026-06-16** (Apple Silicon): full suite green and a live end-to-end `report`
> delivered to both Discord and Slack.

---

## B. Native Windows (cmd + PowerShell) — highest divergence, the real test

After the shared setup, mind the Windows-specific points:

- **Activation differs** — the launcher lives in `.venv\Scripts\` (`activate.bat` for cmd,
  `Activate.ps1` for PowerShell), not `.venv/bin/`.
- PowerShell's `Activate.ps1` may hit an execution-policy block — run
  `Set-ExecutionPolicy -Scope Process Bypass` for the session, or just use the `.bat`. (Skip if
  your tool runs commands without activating.)

### B1 — Console-Unicode check (the whole reason the guard exists)

The crash this guards is `UnicodeEncodeError` when the `⚠`/`✗` glyphs hit a **redirected**
stream that falls back to the cp1252 code page. Trigger the `⚠` glyph deterministically by
feeding a fake secret (so redaction fires), with output redirected and a piped `n` so it's
fully non-interactive — pipe `n` into:

```
python -m orion intake <project> -m "token=sk-ant-fakekey1234567890"
```

with stdout + stderr redirected to a file (your shell's redirection syntax).

**Pass = no `UnicodeEncodeError`**, and the output file contains the preview plus the
`⚠ … redacted` line as valid UTF-8. (Without the guard, this is the exact case that crashes.
The `✗` failure marker goes through the same reconfigured stderr, so this substantially covers
it too.) Also eyeball an interactive run in both **Windows Terminal** and legacy **cmd /
conhost** if available — there the glyphs should already render fine (Python uses
`WriteConsoleW` for an interactive console), but it's worth a look.

### B2 — Windows TOML path check

In `orion.toml`, point `repo_path` at a real Windows repo using **forward slashes**, run a
report, and confirm `git -C` resolves and reads commits:

```toml
repo_path = "C:/Users/you/somerepo"
```

Then confirm the **single-quoted literal** form also works:

```toml
repo_path = 'C:\Users\you\somerepo'
```

Optionally confirm a **double-quoted backslash** path (`"C:\Users\..."`) fails to parse — that
validates the warning in the README / `orion.toml.example`. Also confirm the relative
`state_db = "orion.sqlite3"` is created next to the config file.

---

## C. Record the result

- Update the README **"Tested on"** line to state what actually passed, e.g.
  *"Tested on: Linux, Windows 11 + WSL2, macOS &lt;version&gt;, Windows 11 native."*
- Log any platform quirk you hit as a new entry in [`known-issues.md`](known-issues.md).

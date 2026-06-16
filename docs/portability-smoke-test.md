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

**Already covered — no action:** Linux, and **Windows 11 + WSL2** (which *is* Linux). The two
environments that still need real-hardware validation are **native macOS** and **native
Windows** (cmd / PowerShell).

For each, confirm the four things Phase 3.5 touched: the entry point, per-OS activation, the
console-Unicode guard (the **redirected** case especially — that's the real crash path), and
Windows TOML path handling.

---

## A. macOS — low risk (mostly Linux-like)

```bash
python3 --version           # expect 3.11+
git --version               # must be on PATH
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest            # expect all green (126 at time of writing)
python -m orion --help      # prints usage, exits 0
```

Then one real run against any local git repo you've added to `orion.toml`:

```bash
python -m orion report <project>                  # preview renders, glyphs show; decline (n)
python -m orion report <project> > out.txt 2>&1   # redirected: must NOT crash
```

macOS streams default to UTF-8, so the guard is a no-op here; this mainly confirms per-OS
activation, the entry point, and the suite.

---

## B. Native Windows (cmd + PowerShell) — highest divergence, the real test

```bat
py --version                       :: or `python --version` — expect 3.11+
git --version                      :: must be on PATH
py -m venv .venv
.venv\Scripts\activate.bat         :: cmd;  PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest                   :: expect all green
python -m orion --help             :: prints usage, exits 0
```

> PowerShell's `Activate.ps1` may hit an execution-policy block — run
> `Set-ExecutionPolicy -Scope Process Bypass` for the session, or just use the `.bat`.

### B1 — Console-Unicode check (the whole reason the guard exists)

The crash this guards is `UnicodeEncodeError` when the `⚠`/`✗` glyphs hit a **redirected**
stream that falls back to the cp1252 code page. Trigger the `⚠` glyph deterministically by
feeding a fake secret (so redaction fires), with stdout redirected and a piped `n` so it's
fully non-interactive:

```bat
echo n | python -m orion intake <project> -m "token=sk-ant-fakekey1234567890" > out.txt 2>&1
```

**Pass = no `UnicodeEncodeError`**, and `out.txt` contains the preview plus the
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

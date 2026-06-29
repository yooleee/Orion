<!-- =========================================================================
scheduling.md
---------------------------------------------------------------------------
Responsible for: The per-OS runbook for running Orion UNATTENDED on a cadence by
                 handing the one-shot `report --all --yes` command to the OS's
                 native scheduler (cron / systemd timer / launchd / Task
                 Scheduler).
Role in project: Phase 4 ships unattended send but NOT a scheduler — Orion
                 delegates cadence to the OS (see plans/orion-plan.md, Phase 3.5
                 scheduling stance). This is where that delegation is documented.
Companion: auto_send / --yes / --all behavior is summarized in the README
           "Scheduling" section; this doc is the hands-on setup.
========================================================================= -->

# Scheduling Orion (unattended digests)

Orion has **no built-in scheduler**. It is a one-shot command: to fire on a cadence, something
that is alive at the right time must run it, and Orion can't wake itself. Rather than ship a
daemon, Orion delegates timing to the scheduler your OS already has — which already handles
running-at-boot, missed runs, and run-as-user. (The reasoning behind this choice is recorded in
`plans/orion-plan.md`.)

The command a scheduler should run is:

```
python -m orion report --all --yes
```

- **`--all`** reports on every project in the config.
- **`--yes`** allows the terminal preview to be skipped — but **only** for projects with
  `auto_send = true`. A project without `auto_send` is **skipped and logged, never sent**, even
  under `--yes`. Without `--yes`, every run previews as usual. Both are required.
- Each run reports only what's new, so quiet projects send nothing — a daily job is a daily
  digest. The run **exits non-zero only on a real failure**, so the scheduler alerts on genuine
  problems, not on routine "nothing to send" runs.

> **Before you schedule anything:** run the command once by hand in a terminal and confirm it
> delivers what you expect. Set `auto_send = true` only on the projects you actually want sent
> unattended, and prefer `share_level = "high_level"` for them (no code diff leaves the machine).

---

## The three things that break unattended runs

A scheduler runs your command in a **minimal, non-interactive environment** — not your
logged-in shell. Almost every "it works in my terminal but not from cron/Task Scheduler" issue
is one of these. Address all three and the per-OS setup below is mechanical.

1. **No virtualenv activation.** A scheduler does not run your `.venv/bin/activate`. Call the
   venv's Python **by absolute path** so the right interpreter and dependencies are used:
   - macOS / Linux: `/abs/path/to/orion/.venv/bin/python`
   - Windows: `C:\abs\path\to\orion\.venv\Scripts\python.exe`

2. **The working directory is not your repo.** Orion resolves `state_db`, the collectors'
   relative paths, **and your `.env` secrets** against the **config file's** location, so pass
   the config by **absolute path** and you don't have to care what directory the scheduler
   starts in: `... -m orion report --all --yes --config /abs/path/to/orion/orion.toml`. Keep
   `.env` (webhook URLs + the Anthropic key) beside `orion.toml`, its normal home, and a
   scheduled run finds it with no extra setup. (Exported environment variables also work and
   take precedence, if you'd rather not rely on a file.)

3. **A stripped `PATH` (so `git` may be missing).** The git collector shells out to `git`; a
   scheduler's `PATH` is often minimal and may not include it. Make sure `git` is reachable —
   either ensure the scheduler's PATH contains it, or (simplest) keep the default
   `share_level`/collectors and confirm by running the exact scheduled command from a clean
   shell first. If `git` isn't found, Orion fails that project clearly rather than silently.

Throughout the examples below, replace `/abs/path/to/orion` (or `C:\abs\path\to\orion`) with
your real checkout path, and redirect output to a log file so you can see what happened.

---

## Linux

### Option A — cron (simplest)

Edit your user crontab with `crontab -e` and add a line (this runs daily at 18:00):

```cron
0 18 * * *  /abs/path/to/orion/.venv/bin/python -m orion report --all --yes --config /abs/path/to/orion/orion.toml >> /abs/path/to/orion/orion-cron.log 2>&1
```

cron sets a very small `PATH`. If the git collector reports "git executable not found", add a
`PATH=` line at the top of the crontab that includes git's directory (find it with
`which git`), e.g. `PATH=/usr/bin:/bin`.

### Option B — systemd user timer (more control, logs in the journal)

A timer survives reboots and logs to `journalctl`. Create two files under
`~/.config/systemd/user/`:

`orion.service`:

```ini
[Unit]
Description=Orion unattended report

[Service]
Type=oneshot
ExecStart=/abs/path/to/orion/.venv/bin/python -m orion report --all --yes --config /abs/path/to/orion/orion.toml
```

`orion.timer`:

```ini
[Unit]
Description=Run Orion daily

[Timer]
OnCalendar=*-*-* 18:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

Then enable it:

```bash
systemctl --user daemon-reload
systemctl --user enable --now orion.timer
systemctl --user list-timers          # confirm the next run time
journalctl --user -u orion.service    # read past run output
```

`Persistent=true` runs a missed job once after the machine comes back up. (For a user timer to
run while you're logged out, enable lingering: `loginctl enable-linger "$USER"`.)

---

## macOS

cron still works on macOS, but the native, supported tool is **launchd**. Create
`~/Library/LaunchAgents/com.orion.report.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.orion.report</string>
  <key>ProgramArguments</key>
  <array>
    <string>/abs/path/to/orion/.venv/bin/python</string>
    <string>-m</string>
    <string>orion</string>
    <string>report</string>
    <string>--all</string>
    <string>--yes</string>
    <string>--config</string>
    <string>/abs/path/to/orion/orion.toml</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>18</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>/abs/path/to/orion/orion-launchd.log</string>
  <key>StandardErrorPath</key>
  <string>/abs/path/to/orion/orion-launchd.log</string>
</dict>
</plist>
```

Load it (re-run `bootout` then `bootstrap` after any edit):

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.orion.report.plist
launchctl print gui/$(id -u)/com.orion.report     # inspect status
# launchctl bootout gui/$(id -u)/com.orion.report # to unload
```

If launchd missed the time because the Mac was asleep, it runs the job shortly after wake.

---

## Windows (native)

Use **Task Scheduler**, pointing it at the venv's `python.exe`. The quickest reproducible way
is the `schtasks` command (run in PowerShell or cmd); adjust the path and time:

```bat
schtasks /Create /TN "Orion daily report" /SC DAILY /ST 18:00 ^
  /TR "C:\abs\path\to\orion\.venv\Scripts\python.exe -m orion report --all --yes --config C:\abs\path\to\orion\orion.toml"
```

Notes for native Windows:

- Use `python.exe` from **`.venv\Scripts\`** (not `.venv/bin/`, which is the Unix layout).
- In `orion.toml`, remember the Windows-path rule: a backslash is a TOML escape, so write paths
  with forward slashes (`"C:/Users/you/orion"`) or as single-quoted literals
  (`'C:\Users\you\orion'`). See the README "Windows paths" note.
- The console UTF-8 guard means the status glyphs won't crash the task even when its output is
  redirected to a file.
- To capture output, either set the task to write a log via a wrapper `.bat`, or check the
  task's last result in the Task Scheduler GUI (**Task Scheduler Library → your task →
  History**).

---

## WSL2 caveat (important if you develop in WSL on Windows)

Cron and systemd **inside WSL2 only run while a WSL session is open.** WSL2 has no persistent
init/cron daemon by default — the cron service is not auto-started, and the WSL VM is torn down
once the last session closes, *unless* you've explicitly configured it to stay up (systemd via
`/etc/wsl.conf`'s `[boot] systemd=true`, or a `[boot] command =` line that starts `cron`). So a
crontab you add inside Ubuntu-on-WSL will *not* fire when no WSL terminal is running — including
overnight — unless you've set up that persistence yourself. Two ways to get reliable cadence:

1. **Schedule from the Windows side (recommended).** Use **Windows Task Scheduler** to invoke
   the command *inside* WSL, so Windows owns the wake-up:

   ```bat
   schtasks /Create /TN "Orion daily report (WSL)" /SC DAILY /ST 18:00 ^
     /TR "wsl.exe -e /abs/path/in/wsl/orion/.venv/bin/python -m orion report --all --yes --config /abs/path/in/wsl/orion/orion.toml"
   ```

   (Paths after `wsl.exe -e` are **WSL/Linux** paths, e.g. `/home/you/orion/...`, not `C:\...`.)

2. **Run Orion natively on Windows instead** (see the Windows section above) — Orion is fully
   native, so WSL is not required at all.

If you keep a WSL session open continuously — or you've set up persistent cron yourself (e.g.
`systemd=true` in `/etc/wsl.conf`) — the Linux cron/systemd options work as written. Just know
that without that, the cadence stops whenever WSL shuts down, which is why Task Scheduler
(options 1 or 2 above) is the reliable default on Windows.

---

## Verifying a scheduled job

1. **Run the exact command by hand first**, from a directory that is *not* your repo, to flush
   out PATH / working-directory / venv issues before the scheduler does:
   ```
   cd /tmp        # or any directory that isn't the repo
   /abs/path/to/orion/.venv/bin/python -m orion report --all --yes --config /abs/path/to/orion/orion.toml
   ```
2. **Schedule it a couple of minutes out**, let it fire once, and read the log / journal /
   task history.
3. **Confirm the digest arrived** in Discord/Slack for your opted-in projects, and that
   opted-out projects were skipped (you'll see "Skipping <project>: ... auto_send is not
   enabled" in the output).
4. Once it's proven, set the real cadence.

The deferred enhancement of a cadence-aware `report --all --due` filter (so Orion itself knows
which projects are "due") is tracked in [`known-issues.md`](known-issues.md).

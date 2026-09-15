# =============================================================================
# cli/inspect.py
# -----------------------------------------------------------------------------
# Responsible for: The read-only inspection commands: `status`, `projects [NAME]`, `check`, and
#                  the state-only `baseline`.
# Role in project: Answers "what is configured / what is pending / am I ready to send?" without
#                  sending anything; `check` is the pre-flight gate schedulers rely on.
# Assumptions: None of these print a secret value — names and set/MISSING only.
# =============================================================================
from __future__ import annotations


import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from orion.collectors.git import GitError
from orion.collectors.incubator import IncubatorError
from orion.collectors.notes import NotesError
from orion.collectors.tasks import TasksError
from orion.collectors.tracker import TrackerError
from orion.config import (
    ConfigError,
    Recipient,
    get_project,
    load_config,
)
from orion.secrets import load_secrets
from orion.state import (
    get_last_report_time,
    get_marker,
    open_state,
    set_marker,
)

from .report import _collect_for, _report_collectors_of, _summarizer_key_env


def _humanize_ago(iso_timestamp: str) -> str:
    """Render how long ago an ISO 8601 UTC timestamp was, in coarse units.

    Args:
        iso_timestamp: An ISO 8601 timestamp string (as stored in report_history).

    Returns:
        A short relative phrase like "just now", "5 minutes ago", "3 hours ago",
        or "2 days ago". Returns the input unchanged if it can't be parsed.

    Why:
        `orion status` is a staleness digest, and a relative age reads faster than
        an absolute timestamp for "how overdue is this?". Coarse buckets are enough
        — the point is a glanceable sense of recency, not precision.
    """
    try:
        then = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return iso_timestamp
    seconds = int((datetime.now(timezone.utc) - then).total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


def cmd_status(config_path: Path) -> int:
    """Show, across all projects, what has unreported activity (read-only).

    Args:
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success (no activity is a valid outcome); 1 on a config
        load error.

    Why:
        The cross-project "what still needs reporting?" digest (the gap noted as
        "no unified orion status"). It reuses the report flow's own activity
        detector (_collect_for + CollectorResult.has_activity), so it can never
        disagree with what a real `report` would find, and reads the last-report
        time from report_history — all read-only: no LLM, no send, no network.
    """
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    conn = open_state(config.state_db)
    projects = list(config.projects.values())
    print(f"orion status — {len(projects)} project(s) in {config_path}:")

    # Pad the project name column so the status reads as an aligned list.
    name_width = max((len(p.name) for p in projects), default=0)
    projects_with_activity = 0

    for project in projects:
        new_signals: list[str] = []
        unreadable: list[str] = []
        # Report inputs only: a push-only capability flag has no activity to detect
        # (and no dispatch branch), so it must not reach _collect_for.
        for collector_name in _report_collectors_of(project):
            prior = get_marker(conn, project.name, collector_name)
            try:
                # Reuse the exact report-flow detector — status must agree with what
                # a real `report` would find. Read-only (git log/diff, file reads).
                result = _collect_for(project, collector_name, prior)
            except (GitError, TasksError, NotesError, IncubatorError, TrackerError):
                # Fail-soft: a collector we can't read yet (missing repo path, an
                # uncreated notes file) must not crash the whole digest.
                unreadable.append(collector_name)
                continue
            if result.has_activity:
                new_signals.append(collector_name)

        parts: list[str] = []
        if new_signals:
            parts.append(f"new: {', '.join(new_signals)}")
            projects_with_activity += 1
        if unreadable:
            parts.append(f"unreadable: {', '.join(unreadable)}")
        if not parts:
            parts.append("up to date")
        status = " · ".join(parts)

        last_iso = get_last_report_time(conn, project.name)
        when = "never reported" if last_iso is None else f"last report {_humanize_ago(last_iso)}"

        print(f"  {project.name:<{name_width}}  {status}  · {when}")

    print()
    print(f"{projects_with_activity} of {len(projects)} project(s) have unreported activity.")
    return 0


def cmd_projects(config_path: Path, project_name: str | None = None) -> int:
    """List every configured project, or show ONE fully resolved (read-only).

    Args:
        config_path: Path to orion.toml.
        project_name: A project for the detail view, or None to list everything.

    Returns:
        Exit code: 0 on success; 1 if the config can't be loaded/validated or the
        named project is unknown.

    Why:
        The "what's configured?" command (KI-15), which absorbed the former `show`
        in CS-O PR6 (decision 6): list-vs-detail is one question at two zoom levels,
        so it is one command with an optional name — `check` (validity) and `status`
        (pending activity) stay separate, deliberately. It only READS the config
        (Orion never writes it) and prints only non-secret fields — paths, flags,
        and each recipient's webhook ENV-VAR NAME (never the URL, which lives in
        .env and never enters the config).
    """
    try:
        config = load_config(config_path)
        # Resolve the name inside the try: an unknown project is the same clear
        # ConfigError + exit 1 the former `show` gave.
        project = get_project(config, project_name) if project_name is not None else None
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if project is None:
        # List view: the few facts you most want at a glance.
        print(f"{len(config.projects)} project(s) in {config_path}:")
        for entry in config.projects.values():
            print()
            print(f"  {entry.name}")
            print(f"    auto_send:   {_fmt_bool(entry.auto_send)}")
            print(f"    share_level: {entry.share_level}")
            print(f"    collectors:  {', '.join(entry.collectors)}")
            print(f"    recipients:  {_format_recipients(entry.recipients)}")
        return 0

    # Detail view (the former `show` body, unchanged output format).
    print(f"Project {project.name!r} (from {config_path}):")
    print(f"  repo_path:    {project.repo_path}")
    print(f"  share_level:  {project.share_level}")
    print(f"  auto_send:    {_fmt_bool(project.auto_send)}")
    print(f"  collectors:   {', '.join(project.collectors)}")
    if project.tasks_file is not None:
        print(f"  tasks_file:   {project.tasks_file}")
    if project.notes_file is not None:
        print(f"  notes_file:   {project.notes_file}")
    if project.tracker_file is not None:
        print(f"  tracker_file: {project.tracker_file}")
    print(f"  state_db:     {config.state_db}")
    print("  recipients:")
    for recipient in project.recipients:
        # webhook_env_var is the NAME of the .env key, not the URL — safe to show.
        print(
            f"    - {recipient.name} — channel={recipient.channel}, "
            f"webhook_env_var={recipient.webhook_env_var}"
        )
    return 0


def cmd_check(config_path: Path) -> int:
    """Validate the config and report per-project send-readiness (read-only).

    Args:
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 if the config is valid AND every required piece is in place;
        1 if the config is invalid or any required readiness item is missing.

    Why:
        A pre-flight check — "is my config valid, and am I actually set up to
        send?" Validity reuses load_config (the same validation every command
        runs). Readiness then checks the things a real run needs but config can't
        guarantee: that an enabled git repo path exists, that each recipient's
        webhook secret is present, and that the Anthropic key is present when the
        git (raw) lane is in play. Secrets are loaded exactly as a real run loads
        them (load_secrets — which finds the .env beside the config) and reported
        by NAME as set/MISSING, never by value. Soft items (a collector file not
        created yet) are flagged with a warning but do not fail the check, so the
        exit code is a trustworthy "ready to send" gate.
    """
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Config valid: {len(config.projects)} project(s) in {config_path}.")

    # Load secrets the same way a real run does, so "set" reflects what a run sees.
    load_secrets(config_path)

    problems = 0  # required-but-missing items -> non-zero exit
    warnings = 0  # soft items (may be resolved before the next run)

    for project in config.projects.values():
        print(f"\n  {project.name}")

        # repo_path is only read by the git collector; a missing path would fail
        # that collector at runtime, so it's a problem only when git is enabled.
        if "git" in project.collectors:
            if project.repo_path.exists():
                print(f"    OK  repo_path exists: {project.repo_path}")
            else:
                print(f"    ✗   repo_path MISSING: {project.repo_path}")
                problems += 1

        # Collector files are soft: they may be created before the next run.
        for collector, path in (
            ("tasks", project.tasks_file),
            ("notes", project.notes_file),
            ("tracker", project.tracker_file),
        ):
            if collector in project.collectors and path is not None and not path.exists():
                print(f"    ⚠   {collector}_file not found yet: {path}")
                warnings += 1

        # Each recipient needs its webhook secret present to deliver. Report the
        # variable NAME and whether it's set — never the value.
        for recipient in project.recipients:
            if os.environ.get(recipient.webhook_env_var, "").strip():
                print(f"    OK  {recipient.webhook_env_var} is set ({recipient.name})")
            else:
                print(f"    ✗   {recipient.webhook_env_var} is MISSING ({recipient.name})")
                problems += 1

    # The summarizer secret is needed whenever some project summarizes the git
    # (raw) lane. WHICH secret (or whether one is needed at all) depends on the
    # configured backend: Anthropic needs ANTHROPIC_API_KEY; a local backend needs
    # a key only if api_key_env was set, and otherwise needs none.
    if any("git" in p.collectors for p in config.projects.values()):
        print()
        key_env = _summarizer_key_env(config.summarizer)
        if key_env is None:
            print(
                f"  OK  summarizer provider {config.summarizer.provider!r} needs no "
                f"API key (local endpoint: {config.summarizer.base_url})."
            )
        elif os.environ.get(key_env, "").strip():
            print(f"  OK  {key_env} is set (for the git/raw lane summarizer).")
        else:
            print(f"  ✗   {key_env} is MISSING (for the git/raw lane summarizer).")
            problems += 1

    # Relay readiness (C1). Only relevant when the relay is enabled. A missing token
    # is a WARNING, not a problem: the relay is fail-soft and additive, so a report
    # still sends fine without it — only the dashboard surface is degraded. Keeping
    # it out of `problems` means the `check` exit code stays a faithful "is the core
    # delivery path ready to send?" gate, not "is every optional surface wired up?".
    if config.relay.enabled:
        print()
        if os.environ.get(config.relay.token_env_var, "").strip():
            print(f"  OK  {config.relay.token_env_var} is set (for the relay push).")
        else:
            print(
                f"  ⚠   {config.relay.token_env_var} is MISSING "
                f"(relay push will be skipped; the report still sends)."
            )
            warnings += 1

    print()
    if problems:
        suffix = f", {warnings} warning(s)" if warnings else ""
        # Flush the stdout detail first so the stderr verdict can't jump ahead of
        # it when the two streams are combined (e.g. `orion check >> log 2>&1`).
        sys.stdout.flush()
        print(
            f"Not ready: {problems} required item(s) missing{suffix}.",
            file=sys.stderr,
        )
        return 1
    if warnings:
        print(f"Ready to send ({warnings} warning(s) above).")
    else:
        print("Ready to send.")
    return 0


def cmd_baseline(project_name: str, config_path: Path) -> int:
    """Record a project's current state as already-reported, sending nothing.

    Args:
        project_name: The project to baseline.
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success (including "nothing to baseline"); 1 on a config error.

    Why:
        A never-reported project's FIRST `report` collects its ENTIRE history (the git
        collector diffs against the empty tree), which can be a huge, noisy first
        message. `baseline` sets each enabled collector's marker to its CURRENT state
        WITHOUT sending, so the next real report covers only new activity. This is the
        one sanctioned exception to "advance markers only after a successful send" — it
        is safe because the user explicitly asked to skip current history, and it sends
        nothing (no delivery, no privacy surface). It reuses the normal collection path
        (_collect_for) to read each collector's current marker, so there is no bespoke
        per-collector API and a future collector is baselined automatically.
    """
    try:
        config = load_config(config_path)
        project = get_project(config, project_name)
        conn = open_state(config.state_db)
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # The marker timestamp: same ISO-8601 UTC form set_marker records on a real report.
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    baselined: list[str] = []
    # Report inputs only: a push-only capability flag has no marker to baseline.
    for collector_name in _report_collectors_of(project):
        prior = get_marker(conn, project.name, collector_name)
        try:
            # _collect_for returns the collector's CURRENT marker as new_marker,
            # regardless of whether there is activity — exactly what we baseline to.
            result = _collect_for(project, collector_name, prior)
        except (GitError, TasksError, NotesError, IncubatorError, TrackerError) as exc:
            # A collector that can't be read yet (e.g. a notes file not created) has
            # nothing to baseline — skip it rather than fail the whole command.
            print(f"  skipped {collector_name}: {exc}", file=sys.stderr)
            continue
        set_marker(conn, project.name, collector_name, result.new_marker, generated_at)
        # Warn when re-baselining an already-tracked collector: it skips any activity
        # that accrued since the last report.
        retracked = (
            " (was already tracked — re-baselined, skipping any unreported activity)"
            if prior is not None
            else ""
        )
        baselined.append(collector_name)
        print(f"  {collector_name}: baseline set{retracked}.")

    if not baselined:
        print(f"Nothing to baseline for {project.name!r} (no readable collectors).")
        return 0

    print(
        f"Baselined {project.name!r}: future reports will cover activity after now. "
        f"Nothing was sent."
    )
    return 0


def _fmt_bool(value: bool) -> str:
    """Render a bool the way it's written in TOML (lowercase true/false).

    Args:
        value: The boolean to render.

    Returns:
        "true" or "false".

    Why:
        The inspect output should read back the way the user would type it in
        orion.toml, so `auto_send` shows as `true`/`false`, not Python's
        `True`/`False`.
    """
    return "true" if value else "false"


def _format_recipients(recipients: tuple[Recipient, ...]) -> str:
    """Render recipients as 'Name (channel), Name (channel)' for a one-line listing.

    Args:
        recipients: The project's recipients.

    Returns:
        A comma-joined "name (channel)" string.

    Why:
        A compact, scannable form for `projects`, where each project is a short
        block — the full per-recipient detail (incl. the webhook env-var name)
        lives in `show`.
    """
    return ", ".join(f"{r.name} ({r.channel})" for r in recipients)

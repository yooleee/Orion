# =============================================================================
# cli.py
# -----------------------------------------------------------------------------
# Responsible for: The `orion` command and the end-to-end orchestration of one
#                  report run. This is the ONLY module that knows the full
#                  pipeline order.
# Role in project: Ties every other module together:
#   config -> secrets -> state -> collect -> redact -> (LLM | passthrough)
#   -> redact -> build report -> compose -> preview/confirm -> deliver -> advance.
# Design: the orchestrator is where the pipeline grows. In Phase 2 it gains a loop
#   over multiple collectors + a merge step; every other module's signature stays
#   put. The lane branch below is the real conditional-LLM seam (the `else` is
#   dead in Phase 1 but makes the seam concrete).
# Fail-closed: any error before delivery aborts the run and does NOT advance
#   state, so a retry re-reports the same delta. State advances only after at
#   least one successful send.
# =============================================================================

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from orion.collectors import LANE_RAW, LANE_STRUCTURED
from orion.collectors._markdown import Table, parse_tables
from orion.collectors.about import read_about
from orion.collectors.git import GitError
from orion.collectors.git import collect as collect_git
from orion.collectors.incubator import IncubatorError, read_index
from orion.collectors.incubator import collect as collect_incubator
from orion.collectors.notes import NotesError
from orion.collectors.notes import collect as collect_notes
from orion.collectors.tasks import ChecklistItem, TasksError
from orion.collectors.tasks import collect as collect_tasks
from orion.collectors.tasks import snapshot as snapshot_tasks
from orion.collectors.disciplines import snapshot as snapshot_disciplines
from orion.collectors.tracker import TrackerError
from orion.collectors.tracker import collect as collect_tracker
from orion.collectors.tracker import snapshot as snapshot_tracker
from orion.compose import ComposedMessage, compose
from orion.config import (
    PUSH_ONLY_COLLECTORS,
    REPORT_COLLECTORS,
    SHARE_LEVELS,
    ConfigError,
    ProjectConfig,
    Recipient,
    RelayConfig,
    SummarizerConfig,
    get_project,
    load_config,
    load_relay_config,
)
from orion.delivery import DeliveryError
from orion.delivery.discord import send as discord_send
from orion.delivery.relay import (
    create_user as relay_create_user,
    delete_user as relay_delete_user,
    grant_projects as relay_grant_projects,
    list_users as relay_list_users,
    post_discussion,
    pull_discussions,
    push as relay_push,
    push_checklist,
    push_disciplines,
    revoke_user as relay_revoke_user,
    add_user_key as relay_add_user_key,
    list_user_keys as relay_list_user_keys,
    rename_user as relay_rename_user,
    set_project_lifecycle as relay_set_project_lifecycle,
    set_project_visibility as relay_set_project_visibility,
    set_user_operator as relay_set_user_operator,
    revoke_user_key as relay_revoke_user_key,
    set_user_password as relay_set_user_password,
    set_user_role as relay_set_user_role,
    unlock_user as relay_unlock_user,
)
from orion.delivery.slack import send as slack_send
from orion.extract import (
    AnthropicDisciplineExtractor,
    Discipline,
    DisciplineExtractor,
    ExtractError,
)
from orion.hooks import SUPPORTED_HOOKS, build_hook_script, resolve_hooks_dir
from orion.merge import merge_sections
from orion.redact import redact
from orion.report import (
    ReportBlob,
    build_report,
    serialize_blob,
    serialize_checklist_item,
)
from orion.scaffold import parse_recipient_spec, render_project_stanza, slugify_project_name
from orion.secrets import SecretsError, get_required, load_secrets
from orion.state import (
    get_cache,
    get_discussion_watermark,
    get_last_checklist_push,
    get_last_report_time,
    get_marker,
    open_state,
    record_checklist_push,
    record_report,
    set_cache,
    set_discussion_watermark,
    set_marker,
)
from orion.summarize import AnthropicSummarizer, LocalSummarizer, Summarizer, SummarizerError

# Section title shown in the report for each collector. Kept as an explicit table
# (NOT a registry) so the orchestrator stays a plain dict lookup and adding a
# signal is a one-line change here.
_COLLECTOR_TITLES = {
    "git": "Code activity",
    "tasks": "Completed tasks",
    "notes": "Notes",
    "incubator": "Idea pipeline",
    "tracker": "Application tracker",
}


DEFAULT_CONFIG = "orion.toml"

# Per-project run outcomes. cmd_report maps these to an exit code; `report --all`
# (CP3) tallies them into a summary. Plain string constants mirroring the
# LANE_RAW / LANE_STRUCTURED idiom in collectors — explicit, greppable, and
# directly printable. Deliberately NOT an Enum: the set is tiny and only ever
# compared and counted, so strings keep the call sites readable with no import.
STATUS_SENT = "SENT"                          # delivered to >=1 recipient
STATUS_NO_ACTIVITY = "NO_ACTIVITY"            # nothing new since last report
STATUS_SKIPPED_NOT_OPTED = "SKIPPED_NOT_OPTED"  # --yes but auto_send not enabled
STATUS_ABORTED = "ABORTED"                     # human declined at the preview
STATUS_FAILED = "FAILED"                       # a real failure (alert-worthy)
STATUS_NOT_DUE = "NOT_DUE"                      # --due: reported within its cadence

# Per-project outcomes for `checklist-push --all` (E1.3). A separate small set from the
# report STATUS_* above because the checklist lane's categories differ: there is no
# LLM/preview gate (so no ABORTED / SKIPPED_NOT_OPTED), but there IS a content change-gate
# (NO_CHANGE) and a "project has no checklist to push" skip (NO_CHECKLIST). NOT_DUE and
# FAILED are shared in spirit; reused directly. Kept as strings for the same reason.
STATUS_PUSHED = "PUSHED"                        # checklist pushed to the relay
STATUS_NO_CHANGE = "NO_CHANGE"                  # --due: content identical to last push
STATUS_NO_CHECKLIST = "NO_CHECKLIST"            # project has no checklist/source to push

# E1.2: minimum spacing between unattended reports per `cadence` preset (config.py's
# CADENCES), consumed by `report --all --due`. Each value is the nominal period minus a
# SMALL slack (daily 24h → 23h, weekly 7d → 6d23h) — just enough to absorb a DST shift
# (≤1h) and a scheduler firing slightly early, WITHOUT shortening the cadence. The slack
# is deliberately ~1h, not a full day: a larger margin (e.g. 6d) would let a daily
# scheduler deliver a "weekly" project every 6 days, drifting it faster than weekly.
# Compared in UTC against the stored report_history timestamps — no calendar/local math.
_CADENCE_MIN_INTERVAL = {
    "daily": timedelta(hours=23),
    "weekly": timedelta(days=6, hours=23),
}


def _reconfigure_stream_utf8(stream) -> None:
    """Switch one text stream to UTF-8 output when it supports reconfiguration.

    Args:
        stream: A text stream, typically sys.stdout or sys.stderr.

    Returns:
        None. The stream is reconfigured in place when possible; otherwise this
        is a silent no-op.

    Why:
        On Windows, when output is redirected to a pipe or file, Python encodes
        with the locale ANSI code page (often cp1252), which cannot represent the
        status glyphs we print ("⚠", "✗") — printing one then raises
        UnicodeEncodeError and aborts the run. (To an interactive Windows console
        Python 3.6+ writes via WriteConsoleW, so the glyphs are already fine
        there; the crash is specifically the redirected case.) Forcing UTF-8
        removes that crash while keeping the glyphs on capable terminals. We guard
        two ways because neither should ever break the CLI: `reconfigure` is
        absent on non-TextIOWrapper streams (pytest's capture, embedded
        interpreters like IDLE), and on an unusual stream it can raise. A real end
        user always has a reconfigurable stream; the guards exist for those
        wrapped contexts.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8")
    except (ValueError, OSError):
        # ValueError: the stream is in a state that forbids reconfiguration.
        # OSError: the underlying handle rejected the change. Either way, fall
        # back to the stream as-is rather than crash before any work is done.
        pass


def _ensure_utf8_output() -> None:
    """Make stdout and stderr emit UTF-8 so status glyphs never crash the CLI.

    Returns:
        None. Applies the UTF-8 reconfiguration to both standard streams.

    Why:
        Called once at the top of main(), so it covers every command and both
        invocations (the `orion` console script and `python -m orion`). Applied
        uniformly on all OSes — a harmless no-op where the stream is already UTF-8
        (macOS/Linux, and modern Windows terminals) — so there is no platform
        branch, matching the project's "cross-compat-minded, not one-OS-at-a-time"
        principle.
    """
    _reconfigure_stream_utf8(sys.stdout)
    _reconfigure_stream_utf8(sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to the requested command.

    Args:
        argv: Argument list (defaults to sys.argv[1:] when None). Accepting it
            explicitly makes the CLI testable without touching sys.argv.

    Returns:
        A process exit code (0 success, non-zero failure).

    Why:
        argparse gives us a clear `orion report <project>` interface and free
        --help. The subcommand structure leaves room for future commands
        (`orion history`, etc.) without reshaping this entry point.
    """
    # Before any output, make the standard streams UTF-8 so the status glyphs we
    # print can never raise UnicodeEncodeError on a redirected Windows stream.
    _ensure_utf8_output()

    # The config path defaults to $ORION_CONFIG when set, else "orion.toml". This
    # lets non-interactive callers (git hooks, schedulers, the Claude session skill)
    # set the config location once in the environment instead of passing --config
    # every time. It must be a real env var, NOT a value from .env: the config path
    # is needed BEFORE .env is loaded (load_secrets finds .env beside the config).
    default_config = os.environ.get("ORION_CONFIG") or DEFAULT_CONFIG

    parser = argparse.ArgumentParser(
        prog="orion",
        description="Turn local git activity into supervisor-ready progress updates.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    report_parser = subparsers.add_parser(
        "report", help="Generate and (after preview) send a progress report."
    )
    # project is optional because --all reports on every configured project. main
    # validates that EXACTLY ONE of {project, --all} is given (argparse can't
    # express "a positional XOR a flag, exactly one required" cleanly).
    report_parser.add_argument(
        "project",
        nargs="?",
        default=None,
        help="Project name as defined in orion.toml (omit when using --all).",
    )
    report_parser.add_argument(
        "--all",
        dest="all_projects",
        action="store_true",
        help="Report on every project in the config (for scheduled --all --yes runs).",
    )
    report_parser.add_argument(
        "--config",
        default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )
    report_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help=(
            "Non-interactive: skip the preview for projects with auto_send=true "
            "(for unattended/scheduled runs). Projects without auto_send are "
            "skipped, never sent. Without --yes, every run previews as usual."
        ),
    )
    report_parser.add_argument(
        "--due",
        action="store_true",
        help=(
            "With --all only: report just the projects DUE under their `cadence` "
            "(skip any reported within their interval). Projects with no cadence "
            "set are always due. Lets one scheduled `--all --due --yes` serve "
            "projects on mixed cadences from a single entry."
        ),
    )

    checklist_parser = subparsers.add_parser(
        "checklist-push",
        help="Push a project's current checklist to the relay (no report); --all for every project, --watch for live updates.",
    )
    # project is optional because --all pushes every checklist-enabled project. main
    # validates that EXACTLY ONE of {project, --all} is given (same XOR as `report`).
    checklist_parser.add_argument(
        "project",
        nargs="?",
        default=None,
        help="Project name as defined in orion.toml (omit when using --all; must enable `checklist`).",
    )
    checklist_parser.add_argument(
        "--all",
        dest="all_projects",
        action="store_true",
        help="Push every checklist-enabled project in the config (for scheduled --all --due runs).",
    )
    checklist_parser.add_argument(
        "--config",
        default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )
    checklist_parser.add_argument(
        "--due",
        action="store_true",
        help=(
            "With --all only: push just the projects DUE under their `cadence` (skip any "
            "pushed within their interval), and skip a due project whose content is "
            "unchanged since its last push. Projects with no cadence are always due. Lets "
            "one scheduled `--all --due` entry keep every tracker card fresh."
        ),
    )
    checklist_parser.add_argument(
        "--watch",
        action="store_true",
        help=(
            "Single-project only: run a foreground loop that polls the tasks_file and "
            "pushes the checklist whenever it changes, until Ctrl-C (near-real-time edit "
            "tracking). Cannot combine with --all/--due."
        ),
    )
    checklist_parser.add_argument(
        "--interval",
        type=float,
        default=3.0,
        help="Seconds between polls in --watch mode (default: 3.0).",
    )
    checklist_parser.add_argument(
        "--clear-due-soon-days",
        dest="clear_due_soon_days",
        action="store_true",
        help=(
            "Single-project only: clear this project's stored 'due soon' horizon on the "
            "relay, so it falls back to the default. The relay never clears a setting just "
            "because a push omits it (KI-35), so dropping `due_soon_days` from config "
            "leaves the old horizon in place until you run this."
        ),
    )
    checklist_parser.add_argument(
        "--clear-about",
        dest="clear_about",
        action="store_true",
        help=(
            "Single-project only: clear this project's stored About line on the relay. "
            "Like --clear-due-soon-days, the relay never clears About just because a push "
            "omits it (KI-35), so removing `about_file` from config leaves the old About "
            "in place until you run this."
        ),
    )

    disciplines_parser = subparsers.add_parser(
        "disciplines-push",
        help="Extract a project's disciplines from its docs and push them to the relay (no report).",
    )
    disciplines_parser.add_argument(
        "project",
        help="Project name as defined in orion.toml (must enable the 'disciplines' collector).",
    )
    disciplines_parser.add_argument(
        "--config",
        default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )
    disciplines_parser.add_argument(
        "--clear",
        action="store_true",
        help=(
            "Retire this project's cards: push an EMPTY set deliberately. A normal "
            "push REFUSES to send an empty set (an unreadable doc would otherwise "
            "wipe the section silently), so this is the explicit way to clear it. "
            "Reads no docs and needs no API key."
        ),
    )

    intake_parser = subparsers.add_parser(
        "intake",
        help="Send a pushed/hand-written update for a project (skips collectors & LLM).",
    )
    intake_parser.add_argument("project", help="Project name as defined in orion.toml.")
    intake_parser.add_argument(
        "--config",
        default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )
    intake_parser.add_argument(
        "--message",
        "-m",
        default=None,
        help="The update body. If omitted, the body is read from stdin.",
    )
    intake_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help=(
            "Non-interactive: skip the preview and send (for the Claude session "
            "skill, which shows the summary for approval in-session first). "
            "Redaction still runs; without --yes the preview shows as usual."
        ),
    )

    backfill_parser = subparsers.add_parser(
        "relay-backfill",
        help="Push an already-sent report onto the relay dashboard (relay-only, no chat) — for reports sent before the relay was scoped in.",
    )
    backfill_parser.add_argument("project", help="Project name as defined in orion.toml.")
    backfill_parser.add_argument(
        "--config",
        default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )
    backfill_parser.add_argument(
        "--generated-at",
        required=True,
        help=(
            "ISO 8601 timestamp of when the report was ORIGINALLY sent (read it off the "
            "delivered Slack/Discord message). Sets the card's time on the dashboard; a "
            "naive value is treated as UTC."
        ),
    )
    backfill_parser.add_argument(
        "--body-file",
        default=None,
        help="Path to a file holding the exact report body. If omitted, the body is read from stdin.",
    )
    backfill_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help=(
            "Skip the preview/confirm and push (the content was already delivered once). "
            "Redaction still runs; without --yes the preview shows as usual."
        ),
    )

    hook_parser = subparsers.add_parser(
        "install-hook",
        help="Install a git hook that auto-reports a project on commit/push.",
    )
    hook_parser.add_argument("project", help="Project name as defined in orion.toml.")
    hook_parser.add_argument(
        "--hook",
        choices=SUPPORTED_HOOKS,
        default="pre-push",
        help=(
            "Which git hook to install (default: pre-push, which fires when you "
            "push — less noisy than post-commit, which fires on every commit)."
        ),
    )
    hook_parser.add_argument(
        "--config",
        default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )
    hook_parser.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="Print the hook script to stdout instead of installing it (review first).",
    )
    hook_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing hook of the same name.",
    )

    # The ONE config-writing command. It is explicit, append-only, and previews
    # before writing — so the "config is never written as a side effect of a run"
    # invariant holds (see config.py header).
    add_parser = subparsers.add_parser(
        "add-project",
        help="Register a new project in orion.toml (the only command that writes config).",
    )
    add_parser.add_argument(
        "name",
        nargs="?",
        default=None,
        help="Project name (default: the repo directory's name).",
    )
    add_parser.add_argument(
        "--repo-path",
        dest="repo_path",
        default=None,
        help="Path to the git repo (default: the current repo's top level, else cwd).",
    )
    add_parser.add_argument(
        "--like",
        default=None,
        metavar="PROJECT",
        help="Copy recipients from this existing project (combine with or use instead of --recipient).",
    )
    add_parser.add_argument(
        "--recipient",
        dest="recipients",
        action="append",
        default=[],
        metavar='"Name:channel:ENV_VAR"',
        help='Add a recipient (repeatable). Channel is "discord" or "slack"; the last field NAMES a .env variable.',
    )
    add_parser.add_argument(
        "--share-level",
        dest="share_level",
        choices=SHARE_LEVELS,
        default="high_level",
        help="How much git detail to expose (default: high_level — no code diff).",
    )
    add_parser.add_argument(
        "--collectors",
        default="git",
        help="Comma-separated signals to enable (default: git). Any of: git,tasks,notes,incubator,tracker.",
    )
    add_parser.add_argument(
        "--tasks-file",
        dest="tasks_file",
        default=None,
        help="Path to the tasks checklist. If 'tasks' is enabled and this is omitted, "
        "defaults to <repo>/TODO.md and creates a starter checklist there.",
    )
    add_parser.add_argument(
        "--notes-file",
        dest="notes_file",
        default=None,
        help="Path to the notes file (required if 'notes' is in --collectors).",
    )
    add_parser.add_argument(
        "--tracker-file",
        dest="tracker_file",
        default=None,
        help="Path to the status-aware tracker doc (required if 'tracker' is in --collectors).",
    )
    add_parser.add_argument(
        "--incubator-file",
        dest="incubator_file",
        default=None,
        help="Path to the incubator index.md (required if 'incubator' is in --collectors).",
    )
    add_parser.add_argument(
        "--seed-tasks-from",
        dest="seed_tasks_from",
        default=None,
        metavar="DOC",
        help="When a tasks_file is being created (no --tasks-file given), seed its "
        "checklist from this doc's Markdown tables instead of an empty starter.",
    )
    add_parser.add_argument(
        "--config",
        default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )
    add_parser.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="Print the stanza that would be written, and write nothing (review first).",
    )
    add_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Write without the preview confirmation (for non-interactive callers).",
    )

    # graduate-idea (D4 follow-on): register a graduated incubator idea as a project.
    # It shares add-project's flags (it delegates to it) plus idea-specific ones.
    graduate_parser = subparsers.add_parser(
        "graduate-idea",
        help="Register a graduated incubator idea as a new project (delegates to add-project).",
    )
    graduate_parser.add_argument(
        "idea",
        help="The incubator idea title to graduate (matched case-insensitively).",
    )
    graduate_parser.add_argument(
        "--name",
        default=None,
        help="Project name (default: a slug derived from the idea title).",
    )
    graduate_parser.add_argument(
        "--incubator",
        default=None,
        metavar="PROJECT",
        help="Which incubator project's index to read (needed only if several exist).",
    )
    graduate_parser.add_argument(
        "--incubator-file",
        dest="incubator_file",
        default=None,
        help="Read the index from this path directly (overrides the config lookup).",
    )
    graduate_parser.add_argument(
        "--force",
        action="store_true",
        help="Graduate even if the idea's status is not 'graduated'.",
    )
    # The remaining flags mirror add-project (graduate-idea passes them straight through).
    graduate_parser.add_argument(
        "--repo-path",
        dest="repo_path",
        default=None,
        help="Path to the git repo (default: the current repo's top level, else cwd).",
    )
    graduate_parser.add_argument(
        "--like",
        default=None,
        metavar="PROJECT",
        help="Copy recipients from this existing project.",
    )
    graduate_parser.add_argument(
        "--recipient",
        dest="recipients",
        action="append",
        default=[],
        metavar='"Name:channel:ENV_VAR"',
        help="Add a recipient (repeatable).",
    )
    graduate_parser.add_argument(
        "--share-level",
        dest="share_level",
        choices=SHARE_LEVELS,
        default="high_level",
        help="How much git detail to expose (default: high_level).",
    )
    graduate_parser.add_argument(
        "--collectors",
        default="git",
        help="Comma-separated signals to enable (default: git).",
    )
    graduate_parser.add_argument(
        "--tasks-file",
        dest="tasks_file",
        default=None,
        help="Path to the tasks checklist (required if 'tasks' is in --collectors).",
    )
    graduate_parser.add_argument(
        "--notes-file",
        dest="notes_file",
        default=None,
        help="Path to the notes file (required if 'notes' is in --collectors).",
    )
    graduate_parser.add_argument(
        "--config",
        default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )
    graduate_parser.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="Print the stanza that would be written, and write nothing.",
    )
    graduate_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Write without the preview confirmation (for non-interactive callers).",
    )

    # Read-only inspect commands (B6). They print config; they never write it.
    projects_parser = subparsers.add_parser(
        "projects",
        help="List the projects defined in the config (read-only).",
    )
    projects_parser.add_argument(
        "--config",
        default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )

    show_parser = subparsers.add_parser(
        "show",
        help="Show one project's resolved config (read-only).",
    )
    show_parser.add_argument("project", help="Project name as defined in orion.toml.")
    show_parser.add_argument(
        "--config",
        default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )

    check_parser = subparsers.add_parser(
        "check",
        help="Validate the config and report send-readiness (read-only).",
    )
    check_parser.add_argument(
        "--config",
        default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )

    status_parser = subparsers.add_parser(
        "status",
        help="Show which projects have unreported activity across the config (read-only).",
    )
    status_parser.add_argument(
        "--config",
        default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )

    baseline_parser = subparsers.add_parser(
        "baseline",
        help=(
            "Mark a project's current state as already-reported WITHOUT sending, so the "
            "first real report covers only new activity (skips a giant first report)."
        ),
    )
    baseline_parser.add_argument("project", help="Project name as defined in orion.toml.")
    baseline_parser.add_argument(
        "--config",
        default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )

    # `discussions` is a command GROUP (pull/reply) — the two-way supervisor-interaction
    # loop (E2 Inc 5) and the single CLI conversation surface (KI-28 Stage 2 retired the
    # read-only `comments` command). The developer both reads and replies here, so a group
    # fits. Bearer-authed machine path.
    discussions_parser = subparsers.add_parser(
        "discussions",
        help="Read and reply to a project's two-way supervisor discussion thread (E2 Inc 5).",
    )
    discussions_subs = discussions_parser.add_subparsers(
        dest="discussions_command", required=True
    )

    disc_pull = discussions_subs.add_parser(
        "pull", help="Pull new supervisor messages on a project's discussion thread."
    )
    disc_pull.add_argument("project", help="Project name as defined in orion.toml.")
    disc_pull.add_argument(
        "--config",
        default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )
    disc_pull.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit the raw JSON response instead of the human-readable listing.",
    )
    disc_pull.add_argument(
        "--all",
        dest="show_all",
        action="store_true",
        help=(
            "Show the WHOLE thread without advancing the unread marker (the default "
            "shows only messages new since your last pull, and advances the marker)."
        ),
    )

    disc_reply = discussions_subs.add_parser(
        "reply", help="Post a developer reply to a project's discussion thread."
    )
    disc_reply.add_argument("project", help="Project name as defined in orion.toml.")
    disc_reply.add_argument("body", help="The reply text.")
    disc_reply.add_argument(
        "--as",
        dest="author",
        default="",
        metavar="NAME",
        help=(
            "Display name for this reply (e.g. --as \"Teammate B\"). Defaults to the label "
            "\"developer\". The role is always 'developer' regardless of this name."
        ),
    )
    disc_reply.add_argument(
        "--config",
        default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )

    bot_parser = subparsers.add_parser(
        "bot",
        help="Run the Slack bot (PARKED since KI-28 Stage 2; revived with per-user keys).",
    )
    bot_parser.add_argument(
        "--config",
        default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )

    relay_parser = subparsers.add_parser(
        "relay-serve",
        help="Run the local relay: receive pushed reports and serve a read-only dashboard.",
    )
    relay_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Interface to bind (default: 127.0.0.1 — loopback only, not world-reachable).",
    )
    relay_parser.add_argument(
        "--port",
        type=int,
        default=8787,
        help="Port to bind (default: 8787).",
    )
    relay_parser.add_argument(
        "--db",
        default="orion-relay.sqlite3",
        help="Path to the relay's own sqlite store (default: orion-relay.sqlite3).",
    )
    relay_parser.add_argument(
        "--token-env",
        default="ORION_RELAY_TOKEN",
        help=(
            "Name of the .env variable holding the ingest token "
            "(default: ORION_RELAY_TOKEN). Must match the token your pushing "
            "config's [relay] token_env_var resolves to."
        ),
    )
    relay_parser.add_argument(
        "--view-token-env",
        default="ORION_RELAY_VIEW_TOKEN",
        help=(
            "Name of the .env variable holding the dashboard read secret for HTTP "
            "Basic auth (default: ORION_RELAY_VIEW_TOKEN). REQUIRED when --host is "
            "non-loopback; optional on loopback (reads stay open)."
        ),
    )
    relay_parser.add_argument(
        "--require-view-auth",
        action="store_true",
        help=(
            "Demand the dashboard view secret even on a loopback bind. Use this when "
            "the relay runs on loopback behind a reverse proxy (the proxy exposes it, "
            "so 'loopback' isn't actually private) — a forgotten secret then fails "
            "closed instead of serving an open dashboard. See docs/deployment.md."
        ),
    )
    relay_parser.add_argument(
        "--timezone",
        default="America/Los_Angeles",
        help=(
            "IANA zone the dashboard renders timestamps in (default: "
            "America/Los_Angeles). The relay does not read orion.toml, so this is set "
            'here. Examples: --timezone UTC, --timezone "Europe/London". An unknown '
            "zone is rejected at startup."
        ),
    )
    relay_parser.add_argument(
        "--session-days",
        type=int,
        default=30,
        help="Dashboard login session length in days (default: 30).",
    )
    relay_parser.add_argument(
        "--allow-legacy-admin",
        action="store_true",
        help=(
            "Keep the legacy shared view key usable as an admin login even after "
            "per-user accounts exist (default: off — it is bootstrap-only, usable "
            "only while no users have been provisioned)."
        ),
    )
    relay_parser.add_argument(
        "--disable-legacy-ingest",
        action="store_true",
        help=(
            "Retire the shared ingest token on the push path (default: off — the shared "
            "token keeps working, anonymously, for backward compatibility). Turn this on "
            "once every producer has its own contributor key: the shared token then 401s "
            "and only named per-user keys can push. Each legacy use logs a line first, so "
            "you can confirm it has gone quiet before flipping this."
        ),
    )
    relay_parser.add_argument(
        "--web-dir",
        default=None,
        help=(
            "Path to the built SPA assets (e.g. web/dist) to serve single-host. When set, "
            "the relay serves the React dashboard (static assets + an index.html fallback) "
            "instead of the legacy server-rendered HTML. Omit to serve the legacy HTML."
        ),
    )
    relay_parser.add_argument(
        "--showcase",
        action="store_true",
        help=(
            "Enable the public, no-login Showcase surface (default: off). When on, "
            "GET /api/showcase serves ONLY the projects named with --showcase-project; "
            "while off it 404s and the dashboard hides the Public showcase link."
        ),
    )
    relay_parser.add_argument(
        "--showcase-project",
        action="append",
        default=None,
        dest="showcase_projects",
        metavar='NAME[:"blurb"]',
        help=(
            "Add a project to the public Showcase allowlist (repeatable; order is the "
            'display order). Optionally append a curated one-line blurb after a colon, '
            'e.g. --showcase-project \'orion:A local-first tracker that observes & '
            "reframes'. Without a blurb the project's latest report headline is shown. "
            "Only effective with --showcase."
        ),
    )
    relay_parser.add_argument(
        "--config",
        default=default_config,
        help=f"Path to the config file, used only to locate .env (default: {default_config}; or set $ORION_CONFIG).",
    )

    # `relay-user` is a command GROUP with add/list/revoke subcommands — the admin-side
    # provisioning CLI that talks to a running relay's /api/users endpoint over HTTP
    # (authenticated with the SEPARATE admin token). It is the only nested-subcommand
    # group in the CLI; every other command is flat.
    relay_user_parser = subparsers.add_parser(
        "relay-user",
        help="Manage relay dashboard users: provision, list, and revoke per-user access keys.",
    )
    relay_user_subs = relay_user_parser.add_subparsers(
        dest="relay_user_command", required=True
    )

    ru_add = relay_user_subs.add_parser(
        "add", help="Provision a new user and print their one-time access key."
    )
    ru_add.add_argument("name", help="The user's unique display name / handle.")
    ru_add.add_argument(
        "--role",
        choices=("viewer", "admin", "supervisor", "member", "contributor"),
        default="viewer",
        help=(
            "The user's role (default: viewer). An admin sees all projects; a viewer "
            "or supervisor is scoped to its granted projects (a supervisor may also "
            "post to a project's discussion thread). A contributor is a push-only "
            "producer identity: its key authenticates the ingest endpoints for its "
            "granted projects but never grants dashboard login. A member is a read-only "
            "ORG INSIDER: it sees every org-visible project with no grant at all, plus "
            "any grants on top, and can never write anything."
        ),
    )
    ru_add.add_argument(
        "--project",
        action="append",
        default=[],
        dest="projects",
        metavar="PROJECT",
        help=(
            "A project this viewer may see (repeatable: --project a --project b). "
            "Ignored for an admin's dashboard reads (which see all), but NOT for pushes: "
            "every key is scoped to its account's grants. A viewer with none sees nothing."
        ),
    )
    ru_add.add_argument(
        "--kind",
        choices=("human", "agent"),
        default="human",
        dest="account_kind",
        help=(
            "What this account IS (default: human). An 'agent' is a machine identity "
            "(Claude Code, a CI job) that pushes on a human's behalf: it must have role "
            "contributor and requires --operated-by."
        ),
    )
    ru_add.add_argument(
        "--operated-by",
        metavar="NAME",
        help=(
            "For --kind agent: the human account this agent acts on behalf of. Its work "
            "stays attributed to the agent (badged, 'operated by <name>'), so provenance "
            "is never lost."
        ),
    )
    ru_add.add_argument(
        "--config",
        default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )

    ru_list = relay_user_subs.add_parser(
        "list", help="List the relay's users (no credential material is shown)."
    )
    ru_list.add_argument(
        "--config",
        default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )

    ru_revoke = relay_user_subs.add_parser(
        "revoke",
        help="Revoke a user: deactivate their key and force-log-out any live session.",
    )
    ru_revoke.add_argument("name", help="The user to revoke (by name).")
    ru_revoke.add_argument(
        "--config",
        default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )

    ru_grant = relay_user_subs.add_parser(
        "grant",
        help="Grant an existing user access to one or more additional projects.",
    )
    ru_grant.add_argument("name", help="The user whose scope to widen (by name).")
    ru_grant.add_argument(
        "--project",
        action="append",
        default=[],
        dest="projects",
        metavar="PROJECT",
        help="A project to grant (repeatable: --project a --project b). At least one required.",
    )
    ru_grant.add_argument(
        "--config",
        default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )

    # `key` is a command GROUP (add/list/revoke) — an account holds N credentials, so the
    # verbs act on a credential, not on the account. This REPLACES the retired `rotate`:
    # replacement is now add -> deploy -> verify -> revoke, which overlaps the two keys
    # instead of killing the old one the instant the new one is minted.
    ru_key = relay_user_subs.add_parser(
        "key",
        help="Manage an account's key credentials (add/list/revoke) — replaces `rotate`.",
    )
    ru_key_subs = ru_key.add_subparsers(dest="relay_user_key_command", metavar="{add,list,revoke}")

    ru_key_add = ru_key_subs.add_parser(
        "add", help="Attach a NEW key to an account (shown once); the existing keys keep working."
    )
    ru_key_add.add_argument("name", help="The account to attach the key to.")
    ru_key_add.add_argument(
        "--label", required=True,
        help="A short label for where this key lives, e.g. 'mac' or 'wsl2' (unique per account).",
    )
    ru_key_add.add_argument(
        "--config", default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )

    ru_key_list = ru_key_subs.add_parser(
        "list", help="List an account's credentials (never shows the key material itself)."
    )
    ru_key_list.add_argument("name", help="The account whose credentials to list.")
    ru_key_list.add_argument(
        "--config", default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )

    ru_key_revoke = ru_key_subs.add_parser(
        "revoke", help="Revoke ONE credential by id; the account's other keys keep working."
    )
    ru_key_revoke.add_argument("name", help="The account the credential belongs to.")
    ru_key_revoke.add_argument(
        "--id", required=True, type=int,
        help="The credential id to revoke (from `key list` — labels are reusable, ids are not).",
    )
    ru_key_revoke.add_argument(
        "--config", default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )

    # `password` is a command GROUP (set/unlock). A password is NEVER accepted as an
    # argument: argv lands in shell history, `ps` output, and CI logs. It is either typed
    # at a hidden prompt or minted by the relay and shown once.
    ru_pw = relay_user_subs.add_parser(
        "password",
        help="Manage an interactive account's password (set/unlock).",
    )
    ru_pw_subs = ru_pw.add_subparsers(dest="relay_user_password_command", metavar="{set,unlock}")

    ru_pw_set = ru_pw_subs.add_parser(
        "set",
        help="Set/replace a password (prompts twice, hidden). Their keys stop logging in.",
    )
    ru_pw_set.add_argument("name", help="The interactive account to set a password for.")
    ru_pw_set.add_argument(
        "--generate", action="store_true",
        help="Have the relay mint a strong password and print it ONCE, instead of prompting.",
    )
    ru_pw_set.add_argument(
        "--config", default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )

    ru_pw_unlock = ru_pw_subs.add_parser(
        "unlock",
        help="Clear a login lockout after failed attempts (does not change the password).",
    )
    ru_pw_unlock.add_argument("name", help="The account to unlock.")
    ru_pw_unlock.add_argument(
        "--config", default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )

    ru_role = relay_user_subs.add_parser(
        "role",
        help="Change an account's role (logs out live sessions; a scoped role needs grants).",
    )
    ru_role.add_argument("name", help="The account whose role to change.")
    ru_role.add_argument(
        "role",
        help="The new role: admin | viewer | supervisor | member | contributor.",
    )
    ru_role.add_argument(
        "--config", default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )

    ru_rename = relay_user_subs.add_parser(
        "rename",
        help="Rename an account (already-recorded history keeps the name it was written with).",
    )
    ru_rename.add_argument("name", help="The account to rename.")
    ru_rename.add_argument("new_name", help="The new (unique) name.")
    ru_rename.add_argument(
        "--config", default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )

    ru_set_operator = relay_user_subs.add_parser(
        "set-operator",
        help="Repoint an agent account at a different operating human.",
    )
    ru_set_operator.add_argument("name", help="The agent account to reassign.")
    ru_set_operator.add_argument(
        "operator", help="The human account this agent should act on behalf of."
    )
    ru_set_operator.add_argument(
        "--config", default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )

    ru_delete = relay_user_subs.add_parser(
        "delete",
        help="Hard-delete a user, freeing their name to be reused (revoke keeps the name).",
    )
    ru_delete.add_argument("name", help="The user to delete (by name).")
    ru_delete.add_argument(
        "--config",
        default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )

    # Project-level admin ops. Separate from relay-user because the subject is a PROJECT,
    # not an account — mixing them would make `relay-user visibility` read as a user setting.
    relay_project_parser = subparsers.add_parser(
        "relay-project",
        help=(
            "Manage relay project settings: who inside the org may read a project, and "
            "whether it is still running or finished."
        ),
    )
    relay_project_subs = relay_project_parser.add_subparsers(
        dest="relay_project_command", required=True
    )
    rp_visibility = relay_project_subs.add_parser(
        "visibility",
        help="Set whether a project is org-visible or grant-only (restricted).",
    )
    rp_visibility.add_argument("name", help="The project to set.")
    rp_visibility.add_argument(
        "visibility",
        choices=("org", "restricted"),
        help=(
            "'org': every member-role account may read it with no per-project grant. "
            "'restricted' (the default for every project): grant-only. Viewers and "
            "supervisors are unaffected either way — they always see only their grants."
        ),
    )
    rp_visibility.add_argument(
        "--config", default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )

    # S2.2: a project's lifecycle is DECLARED here, on the relay, and never pushed by a
    # producer — the relay has to keep remembering it after the project leaves orion.toml.
    rp_lifecycle = relay_project_subs.add_parser(
        "lifecycle",
        help="Mark a project finished (past) or still running (active).",
    )
    rp_lifecycle.add_argument("name", help="The project to set.")
    rp_lifecycle.add_argument(
        "lifecycle",
        choices=("past", "active"),
        help=(
            "'past': the project is finished — it groups into the dashboard's 'Past "
            "projects' section and drops out of every deadline view (due-soon, at-risk, "
            "slipping, Scheduling), so it can never read as overdue. 'active' (the default "
            "every project is born with): the normal live state. Fully reversible."
        ),
    )
    rp_lifecycle.add_argument(
        "--config", default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )

    args = parser.parse_args(argv)
    if args.command == "report":
        return cmd_report(
            args.project, Path(args.config), args.yes, args.all_projects, args.due
        )
    if args.command == "intake":
        return cmd_intake(args.project, Path(args.config), args.message, args.yes)
    if args.command == "relay-backfill":
        return cmd_relay_backfill(
            args.project,
            Path(args.config),
            Path(args.body_file) if args.body_file is not None else None,
            args.generated_at,
            args.yes,
        )
    if args.command == "checklist-push":
        return cmd_checklist_push(
            args.project, Path(args.config), args.watch, args.interval,
            args.all_projects, args.due, args.clear_due_soon_days, args.clear_about,
        )
    if args.command == "disciplines-push":
        return cmd_disciplines_push(args.project, Path(args.config), clear=args.clear)

    if args.command == "install-hook":
        return cmd_install_hook(
            args.project, Path(args.config), args.hook, args.print_only, args.force
        )
    if args.command == "add-project":
        return cmd_add_project(
            args.name,
            Path(args.config),
            repo_path=args.repo_path,
            like=args.like,
            recipient_specs=args.recipients,
            share_level=args.share_level,
            collectors_csv=args.collectors,
            tasks_file=args.tasks_file,
            notes_file=args.notes_file,
            tracker_file=args.tracker_file,
            incubator_file=args.incubator_file,
            seed_tasks_from=args.seed_tasks_from,
            print_only=args.print_only,
            assume_yes=args.yes,
        )
    if args.command == "graduate-idea":
        return cmd_graduate_idea(
            args.idea,
            Path(args.config),
            name=args.name,
            incubator=args.incubator,
            incubator_file=args.incubator_file,
            force=args.force,
            repo_path=args.repo_path,
            like=args.like,
            recipient_specs=args.recipients,
            share_level=args.share_level,
            collectors_csv=args.collectors,
            tasks_file=args.tasks_file,
            notes_file=args.notes_file,
            print_only=args.print_only,
            assume_yes=args.yes,
        )
    if args.command == "projects":
        return cmd_projects(Path(args.config))
    if args.command == "show":
        return cmd_show(args.project, Path(args.config))
    if args.command == "check":
        return cmd_check(Path(args.config))
    if args.command == "status":
        return cmd_status(Path(args.config))
    if args.command == "baseline":
        return cmd_baseline(args.project, Path(args.config))
    if args.command == "discussions":
        if args.discussions_command == "pull":
            return cmd_discussions_pull(
                args.project, Path(args.config),
                as_json=args.as_json, show_all=args.show_all,
            )
        if args.discussions_command == "reply":
            return cmd_discussions_reply(
                args.project, args.body, args.author, Path(args.config)
            )
    if args.command == "bot":
        return cmd_bot(Path(args.config))
    if args.command == "relay-serve":
        return cmd_relay_serve(
            args.host,
            args.port,
            Path(args.db),
            args.token_env,
            args.view_token_env,
            args.require_view_auth,
            args.timezone,
            Path(args.config),
            session_days=args.session_days,
            allow_legacy_admin=args.allow_legacy_admin,
            disable_legacy_ingest=args.disable_legacy_ingest,
            web_dir=Path(args.web_dir) if args.web_dir else None,
            showcase_enabled=args.showcase,
            showcase_projects=args.showcase_projects,
        )
    if args.command == "relay-user":
        if args.relay_user_command == "add":
            return cmd_relay_user_add(
                args.name,
                args.role,
                args.projects,
                Path(args.config),
                account_kind=args.account_kind,
                operated_by=args.operated_by,
            )
        if args.relay_user_command == "list":
            return cmd_relay_user_list(Path(args.config))
        if args.relay_user_command == "revoke":
            return cmd_relay_user_revoke(args.name, Path(args.config))
        if args.relay_user_command == "grant":
            return cmd_relay_user_grant(args.name, args.projects, Path(args.config))
        if args.relay_user_command == "key":
            if args.relay_user_key_command == "add":
                return cmd_relay_user_key_add(args.name, args.label, Path(args.config))
            if args.relay_user_key_command == "list":
                return cmd_relay_user_key_list(args.name, Path(args.config))
            if args.relay_user_key_command == "revoke":
                return cmd_relay_user_key_revoke(args.name, args.id, Path(args.config))
            print("Error: give a key subcommand: add, list, or revoke.", file=sys.stderr)
            return 2
        if args.relay_user_command == "password":
            if args.relay_user_password_command == "set":
                return cmd_relay_user_password_set(args.name, args.generate, Path(args.config))
            if args.relay_user_password_command == "unlock":
                return cmd_relay_user_password_unlock(args.name, Path(args.config))
            print("Error: give a password subcommand: set or unlock.", file=sys.stderr)
            return 2
        if args.relay_user_command == "role":
            return cmd_relay_user_role(args.name, args.role, Path(args.config))
        if args.relay_user_command == "rename":
            return cmd_relay_user_rename(args.name, args.new_name, Path(args.config))
        if args.relay_user_command == "set-operator":
            return cmd_relay_user_set_operator(
                args.name, args.operator, Path(args.config)
            )
        if args.relay_user_command == "delete":
            return cmd_relay_user_delete(args.name, Path(args.config))
    if args.command == "relay-project":
        if args.relay_project_command == "visibility":
            return cmd_relay_project_visibility(
                args.name, args.visibility, Path(args.config)
            )
        if args.relay_project_command == "lifecycle":
            return cmd_relay_project_lifecycle(
                args.name, args.lifecycle, Path(args.config)
            )
    return 1  # Unreachable: subparsers are required.


def cmd_report(
    project_name: str | None,
    config_path: Path,
    assume_yes: bool,
    all_projects: bool,
    due_only: bool = False,
) -> int:
    """Set up shared state, then run the report pipeline for one or all projects.

    Args:
        project_name: The project to report on, or None when --all is used.
        config_path: Path to orion.toml.
        assume_yes: True for a non-interactive run (the `--yes` flag). Passed
            through to _run_report, which combines it with each project's
            auto_send to decide whether the human preview is bypassed.
        all_projects: True for `--all` (report on every configured project).
        due_only: True for `--due` — report only projects due under their cadence
            (see _is_due), marking the rest STATUS_NOT_DUE. Requires all_projects
            (a usage error otherwise); defaults False so single-project runs and
            the plain `--all` path are unchanged.

    Returns:
        Exit code: 2 for a usage error (neither or both of project / --all, or
        --due without --all); 1 if ANY project genuinely FAILED; otherwise 0.
        No-activity, skipped, not-due, and human-aborted projects are all clean
        exit 0 — so a scheduler alerts only on a real failure, not on the routine
        "nothing to send" cases.

    Why:
        Setup (config/secrets/state) is split from the per-project pipeline: those
        errors are GLOBAL (a bad config breaks every project), so they belong here
        and fail the whole command, while per-project pipeline errors are handled
        inside _run_report so that `report --all` can fail-soft. Single-project and
        --all share ONE loop over a resolved project list (DRY): the only
        difference is that --all prints a tally afterward. Exit code is driven by
        the collected statuses, not by which mode was used.
    """
    # Exactly one of {project, --all} must be given. argparse can't express this
    # XOR for a positional vs. a flag, so validate it here with a clear message.
    if all_projects and project_name is not None:
        print(
            "Error: give either a project name or --all, not both.",
            file=sys.stderr,
        )
        return 2
    if not all_projects and project_name is None:
        print(
            "Error: give a project name, or --all to report on every project.",
            file=sys.stderr,
        )
        return 2
    # --due is a filter over the --all set; on a single named project it has no
    # meaning (you asked for that one explicitly), so reject the combination rather
    # than silently ignoring the flag.
    if due_only and not all_projects:
        print(
            "Error: --due only applies with --all (it filters the all-projects set).",
            file=sys.stderr,
        )
        return 2

    try:
        config = load_config(config_path)
        load_secrets(config_path)
        conn = open_state(config.state_db)
        # Resolve the target project list up front. For a single project this also
        # turns an unknown name into a clean setup error (get_project raises
        # ConfigError), preserving the pre-Phase-4 behavior.
        projects = (
            list(config.projects.values())
            if all_projects
            else [get_project(config, project_name)]
        )
    except (ConfigError, SecretsError) as exc:
        # Setup errors are global and user-fixable (a config typo, a missing .env):
        # print cleanly and fail closed before any project work begins.
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # Single "now" for the whole run so every project's due check compares against
    # the same instant (a long run can't drift a borderline project across its edge).
    now = datetime.now(timezone.utc)

    # Fail-soft loop: _run_report catches its own per-project errors and returns
    # STATUS_FAILED, so one bad project never stops the rest of an --all run. When
    # --due is set, a project reported within its cadence is skipped BEFORE any
    # collection/LLM/preview work and recorded STATUS_NOT_DUE — so it still counts in
    # the tally (numbers reconcile) but does nothing. The skip runs ahead of the
    # auto_send/preview gate and never alters it for the projects that DO run.
    statuses = []
    for project in projects:
        if due_only and not _is_due(project, conn, now):
            print(f"[{project.name}] not due yet (cadence={project.cadence}); skipping.")
            statuses.append(STATUS_NOT_DUE)
            continue
        statuses.append(
            _run_report(
                project, conn, assume_yes, config.summarizer, config.relay,
                config.display_timezone,
            )
        )

    if all_projects:
        _print_all_summary(statuses)

    # Only a real FAILED is a non-zero exit; NO_ACTIVITY / SKIPPED / ABORTED are
    # all routine, intended outcomes that should not look like an error to cron.
    return 1 if any(status == STATUS_FAILED for status in statuses) else 0


def _is_due_at(cadence: str | None, last_iso: str | None, now: datetime) -> bool:
    """Whether a cadence-gated action is due at `now`, given when it last ran.

    Args:
        cadence: One of config.CADENCES ("daily" | "weekly") or None (no cadence).
        last_iso: ISO 8601 UTC timestamp of the last run of this action for this
            project, or None if it has never run. The caller supplies it from the
            relevant per-action source (report_history for reports,
            checklist_push_history for checklist pushes) — this function is agnostic
            to which.
        now: The current instant as a timezone-aware UTC datetime, passed in (not read
            here) so the decision is deterministic and unit-testable.

    Returns:
        True if the action is due now, False if it ran within its cadence interval.

    Why:
        The whole of `--due`'s per-project decision, factored out of _is_due so BOTH
        `report --due` and `checklist-push --due` consume one implementation of the
        slack-adjusted interval (_CADENCE_MIN_INTERVAL) — each caller supplies its own
        last-run source (E1.3). Two cases are always due: no cadence (opt-in — behaves
        like plain --all), and never run (nothing to be "too soon" after). Otherwise we
        compare in UTC using the min interval — no calendar/local-midnight math, so
        scheduler jitter and DST can't wrongly skip a run. Malformed and future
        timestamps both fall through to "due" (the conservative choice: better to act
        than to silently go quiet on a bad/skewed row), never a crash.
    """
    # Opt-in: no cadence → always due, so --due degrades to plain --all for this action.
    if cadence is None:
        return True
    # Never run → nothing to be too-soon after, so it is due.
    if last_iso is None:
        return True

    # Stored timestamps are tz-aware UTC ISO. Defensive parse: a malformed row (external
    # tampering, or a format change) must NOT crash the caller's --all loop — treat
    # "can't tell when it last ran" as DUE rather than raising.
    try:
        last_dt = datetime.fromisoformat(last_iso)
    except ValueError:
        return True
    # Guard against a legacy naive row: attach UTC so the subtraction is tz-aware both sides.
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)

    # A timestamp AHEAD of now (a clock correction, or imported state) would make the
    # elapsed interval negative and wrongly suppress the action until that future time
    # plus its cadence. Treat "last ran in the future" as due too — same conservative
    # default as an unparseable row.
    if last_dt > now:
        return True

    return (now - last_dt) >= _CADENCE_MIN_INTERVAL[cadence]


def _is_due(project: ProjectConfig, conn: sqlite3.Connection, now: datetime) -> bool:
    """Whether a project is due for a REPORT under its `cadence`, at instant `now`.

    Args:
        project: The project whose cadence gates the decision. `project.cadence` is
            one of config.CADENCES ("daily" | "weekly") or None.
        conn: An open state connection, to read the last delivered report time.
        now: The current instant as a timezone-aware UTC datetime (one value shared
            across the whole --all run, passed in rather than read here so the
            decision is deterministic and unit-testable).

    Returns:
        True if the project should be reported now, False if it reported within its
        cadence interval and should be skipped.

    Why:
        The report-lane binding of the shared cadence decision (_is_due_at): its
        last-run source is report_history (get_last_report_time). We keep the
        no-cadence short-circuit here so an un-cadenced project skips the DB read
        entirely, exactly as before this was factored — report's behavior is unchanged.
    """
    # Short-circuit before any DB read: an un-cadenced project is always due (opt-in).
    if project.cadence is None:
        return True
    return _is_due_at(project.cadence, get_last_report_time(conn, project.name), now)


def _print_all_summary(statuses: list[str]) -> None:
    """Print a one-line tally of per-project outcomes after a `report --all` run.

    Args:
        statuses: One STATUS_* value per project, in the order they ran.

    Returns:
        None. Prints a single summary line to stdout.

    Why:
        An --all run can touch many projects; a human (or a cron log) needs a
        single glance to see what happened. Every category is shown — including
        aborted — so the numbers always reconcile to the project count, which
        makes a surprising result (e.g. an unexpected "failed") obvious instead of
        hidden by omission.
    """
    counts = {
        STATUS_SENT: 0,
        STATUS_NO_ACTIVITY: 0,
        STATUS_SKIPPED_NOT_OPTED: 0,
        STATUS_ABORTED: 0,
        STATUS_FAILED: 0,
        STATUS_NOT_DUE: 0,
    }
    for status in statuses:
        counts[status] += 1

    print(
        f"\n{len(statuses)} project(s): "
        f"{counts[STATUS_SENT]} sent, "
        f"{counts[STATUS_NO_ACTIVITY]} no activity, "
        f"{counts[STATUS_SKIPPED_NOT_OPTED]} skipped, "
        f"{counts[STATUS_NOT_DUE]} not due, "
        f"{counts[STATUS_ABORTED]} aborted, "
        f"{counts[STATUS_FAILED]} failed."
    )


def _run_report(
    project: ProjectConfig,
    conn: sqlite3.Connection,
    assume_yes: bool,
    summarizer_cfg: SummarizerConfig,
    relay_cfg: RelayConfig,
    display_timezone: str,
) -> str:
    """Run the full report pipeline for ONE already-loaded project.

    Args:
        project: The validated project config to report on.
        conn: An open state-store connection (from open_state).
        assume_yes: True for an unattended run (the `--yes` flag). Combined with
            project.auto_send, it decides whether the human preview is bypassed.
        summarizer_cfg: The global summarizer backend config (B4). Used only on
            the raw lane, to build the configured summarizer lazily.
        relay_cfg: The global relay config (C1). When enabled, the serialized blob
            is also pushed to the relay after a successful delivery — fail-soft, so
            a relay error never changes this run's outcome.
        display_timezone: The configured IANA zone for message timestamps (KI-20),
            passed to compose so the delivered message matches the dashboard's zone.

    Returns:
        One of the STATUS_* constants describing the outcome, so the caller can
        set an exit code and (for `report --all`) tally a summary.

    Why:
        Phase 4 needs the per-project pipeline callable both for a single project
        and in a loop (`report --all`), so it is extracted here. It owns its own
        error handling — returning STATUS_FAILED instead of raising — so one
        project's failure never aborts an --all run, mirroring the per-recipient
        fail-soft in _deliver. It also encodes the pipeline order and the safety
        rules: two-pass redaction, advance-only-after-success, and the
        preview gate.

        Preview gate (the security-critical part): the human preview is skipped
        ONLY when assume_yes AND project.auto_send are BOTH true. --yes on a
        project that has not opted in is skipped outright (short-circuited before
        any collection or LLM call); auto_send without --yes still previews
        (a human is present, so config alone never bypasses the gate). Redaction
        is unchanged on every path.
    """
    # Defense-in-depth gate, checked before ANY work: an unattended run for a
    # project that has not opted in to preview-less delivery is skipped here, so
    # we never collect, never call the LLM, and never send for it. This enforces
    # "--yes alone never sends; config alone never sends" — both are required.
    if assume_yes and not project.auto_send:
        print(
            f"Skipping {project.name!r}: --yes was given but auto_send is not "
            f"enabled, so a preview is required and no human is present. "
            f"Nothing sent; state unchanged."
        )
        return STATUS_SKIPPED_NOT_OPTED

    try:
        # --- Collect from each enabled signal, in config order ---
        # Each collector becomes one titled section; each tracks its own delta
        # marker independently (advancing git must not disturb tasks/notes).
        sections: list[tuple[str, str, str]] = []     # (collector, title, finished body)
        pending_markers: list[tuple[str, str]] = []   # (collector, new_marker) to advance on send
        redaction_hits = 0
        any_raw_lane = False
        # Built lazily, and only if a RAW collector actually has activity, so a
        # structured-only run never needs an API key (or any summarizer at all).
        # _build_summarizer keeps secret handling in the CLI (not in summarize.py).
        summarizer: Summarizer | None = None

        # Push-only capability flags produce no section here and have no _collect_for
        # branch by design, so they are filtered out before dispatch (see
        # _report_collectors_of). Reading a marker for one would be meaningless too.
        for collector_name in _report_collectors_of(project):
            prior = get_marker(conn, project.name, collector_name)
            result = _collect_for(project, collector_name, prior)
            if not result.has_activity:
                continue

            # Redaction pass 1: scrub this collector's text BEFORE it goes anywhere
            # — to the LLM on the raw lane, or into the merged body on the
            # structured lane (the structured-lane safety net the plan requires).
            pass1 = redact(result.raw_text)
            redaction_hits += pass1.hit_count

            if result.lane == LANE_RAW:
                if summarizer is None:
                    summarizer = _build_summarizer(summarizer_cfg, get_required)
                body = summarizer.summarize(pass1.text, project.share_level)
                any_raw_lane = True
            else:
                # Structured lane: pass through, NO LLM. This is the seam Phase 1
                # left dead; structured collectors now fill it.
                body = pass1.text

            # Carry the collector name (not just its title) so D5 can filter
            # sections per recipient-audience without re-deriving it from the title.
            sections.append((collector_name, _COLLECTOR_TITLES[collector_name], body))
            pending_markers.append((collector_name, result.new_marker))

        if not sections:
            print(f"No new activity for {project.name!r} since the last report.")
            return STATUS_NO_ACTIVITY

        # --- Redaction pass 2 per section (safety net), then merge ---
        # The second redaction pass runs on EACH section body before assembly, so
        # every piece of text that will later land in a Block Kit / embed field
        # (B3) is twice-redacted at the source. The flat `body` is then the merge
        # of these already-twice-redacted sections, which stays byte-identical to
        # the old "merge then redact" output for normal content: section titles
        # are Orion constants (no secrets), and a secret never straddles a section
        # boundary. Sections that are empty after redaction are dropped here, the
        # same rule merge_sections applies, so the carried sections and the flat
        # body always agree.
        redacted_sections: list[tuple[str, str, str]] = []   # (collector, title, safe body)
        for collector_name, title, section_body in sections:
            pass2 = redact(section_body)
            redaction_hits += pass2.hit_count
            safe_section = pass2.text.strip()
            if not safe_section:
                continue
            redacted_sections.append((collector_name, title, safe_section))

        # The FULL body/blob (every surviving section) backs two things the
        # per-audience filtering must NOT change: the empty-after-redaction safety
        # guard below, and the relay push (the dashboard always receives the
        # complete, unfiltered report — D5 filters only the chat-channel delivery).
        full_pairs = [(title, body) for _, title, body in redacted_sections]
        safe_body = merge_sections(full_pairs)
        if not safe_body.strip():
            print(
                f"Refusing to send {project.name!r}: the report body is empty "
                f"after redaction.",
                file=sys.stderr,
            )
            return STATUS_FAILED

        # --- Build the portable (full) report blob ---
        # lane is provenance: RAW if the LLM touched any part of this run, else
        # STRUCTURED. Delta markers are per-collector (in the state store), so the blob
        # carries none (the old single source_marker was dropped in KI-8). The
        # twice-redacted sections ride along for B3's structured rendering. This is
        # the blob the relay receives; the per-audience blobs below are derived from
        # the same sections, filtered.
        generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        lane = LANE_RAW if any_raw_lane else LANE_STRUCTURED

        # --- Capture the project's LIVE checklist (E2 Inc 2), if enabled ---
        # A SEPARATE read of the checklist source(s) from any collector's delta: it
        # carries the FULL current checklist (open + done) to the dashboard's live view.
        # The source is a tasks_file and/or a tracker_file (E2 Inc 2.6). It rides on
        # full_blob ONLY (the relay payload); the per-audience chat blobs below are
        # unaffected (chat enrichment is out of scope). Each item's text passes through
        # redact() — the structured-lane safety net the privacy rule requires — before
        # it leaves the machine, and its hits join the run's count. None when the
        # project has no checklist enabled, which omits it from the wire.
        checklist: tuple[ChecklistItem, ...] | None = None
        if project.checklist and _checklist_source_files(project):
            redacted_items, checklist_hits = _redacted_checklist(project)
            redaction_hits += checklist_hits
            checklist = tuple(redacted_items)

        # --- Observe the project's About line (KB-surface Unit 2), if configured ---
        # The first prose paragraph of about_file, redacted and rides the ingest blob
        # set-only (a report never clears About). None ⇒ omitted from the wire. Its
        # redaction hits join the run count, like the checklist's.
        about, about_hits = _redacted_about(project)
        redaction_hits += about_hits

        full_blob = build_report(
            project,
            safe_body,
            lane,
            generated_at,
            sections=tuple(full_pairs),
            checklist=checklist,
            # Carrier 1 of 2 for the due-soon window: the ingest blob. None when the
            # project doesn't set it, which omits the key from the wire (relay default).
            due_soon_days=project.due_soon_days,
            # Carrier 1 of 2 for About (set-only on ingest); None ⇒ omitted from the wire.
            about=about,
        )

        # --- Group recipients into audiences and compose one filtered message
        #     per audience (D5) ---
        # An audience is (channel, signal-set): recipients who share both receive
        # the exact same bytes. For each audience we keep only the sections whose
        # collector that audience subscribed to, then merge/build/compose that
        # filtered slice (reusing merge_sections/build_report/compose unchanged). An
        # audience whose subscribed signals had no activity this run yields no
        # sections and is skipped — those recipients simply get nothing this run.
        groups = _audience_groups(project)
        group_messages: dict[tuple[str, frozenset[str]], ComposedMessage] = {}
        for (channel, signals), _recips in groups.items():
            group_pairs = [
                (title, body)
                for collector_name, title, body in redacted_sections
                if collector_name in signals
            ]
            if not group_pairs:
                continue
            group_body = merge_sections(group_pairs)
            # The group blob is transient — it only feeds compose. lane rides along
            # as the run's provenance; compose does not read it and this blob is
            # never serialized (the relay gets full_blob), so the run-level value is
            # honest enough without tracking lane per section.
            group_blob = build_report(
                project, group_body, lane, generated_at, sections=tuple(group_pairs)
            )
            group_messages[(channel, signals)] = compose(
                group_blob, channel, display_timezone
            )

        if not group_messages:
            # Sections existed, but no recipient subscribed to a signal that was
            # active this run. Nothing to send; do NOT advance markers (an
            # unconsumed delta stays available for a future recipient of that signal).
            print(
                f"No new activity for {project.name!r} matched any recipient's "
                f"signals this run; nothing sent, state unchanged."
            )
            return STATUS_NO_ACTIVITY

        # --- Preview gate ---
        # By construction, if assume_yes is True here then project.auto_send is
        # also True (the not-opted case returned above), so this branch is the
        # BOTH-required bypass. Otherwise we always show the human preview — which
        # is why auto_send alone (no --yes) can never skip it. With >1 audience each
        # block is labeled with who receives it, so the human sees each filtered view.
        multi = len(group_messages) > 1
        previews = [
            (_describe_group(channel, recips) if multi else "", group_messages[(channel, signals)])
            for (channel, signals), recips in groups.items()
            if (channel, signals) in group_messages
        ]
        if assume_yes:
            print(
                f"Auto-sending {project.name!r} "
                f"(preview skipped: --yes and auto_send=true)."
            )
        elif not _preview_and_confirm(previews, redaction_hits):
            print("Aborted. Nothing was sent; state unchanged.")
            return STATUS_ABORTED

        # --- Deliver: each recipient gets the message composed for its audience ---
        sent_to, failed = _deliver(
            group_messages,
            project.recipients,
            lambda r: (r.channel, frozenset(r.signals)),
        )
        if not sent_to:
            print(
                f"No deliveries succeeded for {project.name!r}; state not advanced.",
                file=sys.stderr,
            )
            return STATUS_FAILED

        # --- Advance markers ONLY after at least one successful send, and ONLY
        # for the collectors that had activity this run. ---
        # KI-1 (deliberate policy, decided 2026-06-18): we advance on >=1 successful
        # recipient, NOT only on all-success. Advancing only when ALL succeed would let
        # one permanently-broken recipient block state forever and re-spam the working
        # ones every run. The accepted gap — a transiently-failed recipient misses this
        # delta — is bounded; the real fix (per-recipient delivery state) belongs with
        # the C3 multi-party model (KI-11). D5 nuance: with per-recipient signal
        # routing, ALL active markers still advance on >=1 send of the run, so a signal
        # whose only subscriber failed (while another audience succeeded) advances
        # unreceived — the same bounded gap at audience granularity. See known-issues KI-1.
        for collector_name, marker in pending_markers:
            set_marker(conn, project.name, collector_name, marker, generated_at)
        record_report(conn, project.name, safe_body, sent_to, generated_at)

        print(f"Sent to: {', '.join(sent_to)}.")
        if failed:
            print(
                f"(Note: {len(failed)} recipient(s) failed; state advanced because "
                f"at least one delivery succeeded.)"
            )

        # Additive C1 step: push the portable blob to the relay (if enabled). Placed
        # AFTER state has advanced and is fail-soft, so the dashboard surface can
        # never affect the delivered-report outcome or the markers. The relay always
        # receives the FULL, unfiltered report — D5's per-recipient filtering applies
        # only to chat-channel delivery, not the dashboard's record.
        _relay_push(full_blob, relay_cfg)
        return STATUS_SENT

    except (GitError, SummarizerError, TasksError, NotesError, IncubatorError, TrackerError, SecretsError) as exc:
        # Per-project, fail-soft: print a clean message and report FAILED so an
        # --all run can continue with the next project. SecretsError here is the
        # ANTHROPIC key fetch on the raw lane (the webhook fetch is handled inside
        # _deliver); setup-time config/secrets errors are caught by the caller.
        print(f"Error reporting {project.name!r}: {exc}", file=sys.stderr)
        return STATUS_FAILED


def _checklist_source_files(project: ProjectConfig) -> list[Path]:
    """The local file(s) a project's live checklist is read from.

    Args:
        project: The project to inspect.

    Returns:
        The configured checklist-source paths in a stable order: tasks_file first (if
        set), then tracker_file (if set). Empty when the project has no checklist
        source at all.

    Why:
        The checklist push (guard) and the watch loop (the polled/printed file) both
        need to know "what feeds this project's checklist," and that became more than
        just tasks_file once the tracker collector landed. Centralizing the answer
        keeps the guard and the watch agreeing on the same set of sources.
    """
    files: list[Path] = []
    if project.tasks_file is not None:
        files.append(project.tasks_file)
    if project.tracker_file is not None:
        files.append(project.tracker_file)
    return files


def _redacted_checklist(project: ProjectConfig) -> tuple[list[ChecklistItem], int]:
    """Snapshot a project's current checklist and redact each item's text.

    Args:
        project: The project to read. Its checklist comes from a tasks_file (checkbox
            snapshot) and/or a tracker_file (status-aware snapshot) — both feed the
            same {text, done} surface.

    Returns:
        A (items, hits) pair: the redacted ChecklistItem list (an item whose text is
        ENTIRELY a secret — empty after redaction — is dropped to avoid a blank row),
        and the count of secrets scrubbed across all items. Returns ([], 0) when the
        project has neither a tasks_file nor a tracker_file.

    Why:
        BOTH the report push (_run_report) and the dedicated checklist push
        (cmd_checklist_push / its watch loop) must apply the SAME redaction to checklist
        item texts before they leave the machine — the non-negotiable privacy net.
        Factoring it here means that guarantee lives in ONE place and cannot drift
        between the two lanes, and means a new checklist source (the tracker) is
        redacted by construction without a second redaction site.
    """
    items: list[ChecklistItem] = []
    hits = 0
    # Gather raw items from every checklist source in a stable order (tasks first, then
    # tracker), then dedup by RAW text (KI-6 identity-by-text) so a title present in
    # both sources collapses to its first occurrence before redaction.
    raw_items: list[ChecklistItem] = []
    if project.tasks_file is not None:
        raw_items.extend(snapshot_tasks(project.tasks_file))
    if project.tracker_file is not None:
        raw_items.extend(snapshot_tracker(project.tracker_file))

    seen: set[str] = set()
    for item in raw_items:
        if item.text in seen:
            continue
        seen.add(item.text)
        scrub = redact(item.text)
        hits += scrub.hit_count
        # A secret inside an item name is replaced with a placeholder (not dropped), so
        # the item still shows with its done-state. We skip an item only if its text is
        # empty AFTER redaction (the whole label was a secret), to avoid a blank row.
        safe_text = scrub.text.strip()
        if safe_text:
            # due_date is already a normalized ISO date (or None), never raw user text, so
            # it rides through untouched — but the rebuild must preserve it (and key/group)
            # or they would be lost before reaching the wire. The `key` (a title) and
            # `group` (a heading) ARE user text, so both are redacted here too as a safety
            # net; their hits are NOT re-counted: a `key` is a substring of `text` (already
            # counted), and a `group` is shared across many items, so counting it per item
            # would multiply one secret into many. None (tasks/table items) stays None.
            safe_key = redact(item.key).text if item.key is not None else None
            safe_group = redact(item.group).text if item.group is not None else None
            items.append(
                ChecklistItem(
                    text=safe_text,
                    done=item.done,
                    due_date=item.due_date,
                    key=safe_key,
                    group=safe_group,
                    # `status` is a semantic enum value (not user free text), so it rides
                    # through untouched like done/due_date — but the rebuild must carry it
                    # or it would be lost before reaching the wire (E2 Inc 4, gap-8).
                    status=item.status,
                )
            )
    return items, hits


def _checklist_payload(project: ProjectConfig) -> list[dict]:
    """The project's redacted checklist as the wire payload (list of {text, done[, due_date]}).

    Why:
        The relay push and the watch loop both need the checklist in the exact JSON
        shape push_checklist sends. Deriving it here (over _redacted_checklist) keeps
        the redaction-then-serialize step in one place and gives the watch loop a value
        it can compare across ticks to detect changes.
    """
    items, _hits = _redacted_checklist(project)
    return [serialize_checklist_item(item) for item in items]


def _redacted_about(project: ProjectConfig) -> tuple[str | None, int]:
    """Read and redact a project's About line, or (None, 0) when there is none.

    Args:
        project: The project to read. Its About source is `about_file` (the band is off
            when that is None).

    Returns:
        A (about, hits) pair: the redacted About string (None when no about_file is set,
        the doc is missing/unreadable, or it has no prose), and the count of secrets
        scrubbed. None vs a string is the absent-vs-present distinction the caller uses to
        decide whether to send About at all.

    Why:
        About is user-authored content, so it MUST pass the same structured-lane redaction
        net as checklist item text before it leaves the machine (privacy rule). Factoring
        it here means both carriers — the report/ingest blob and the /checklist push —
        redact About in ONE place and cannot drift, exactly as _redacted_checklist does for
        item text. Reading fails soft to None (via read_about) so a mis-pointed doc never
        blocks a report or push.
    """
    if project.about_file is None:
        return None, 0
    raw = read_about(project.about_file)
    if raw is None:
        return None, 0
    scrub = redact(raw)
    # Unlike a checklist row we do NOT drop an all-secret About to None: the placeholder
    # left by redaction is a truthful "there was a secret here" signal, and About is a
    # single field, not a list where a blank row looks broken. read_about already ruled
    # out empty input, so scrub.text is non-empty.
    return scrub.text, scrub.hit_count


def _checklist_content_hash(
    payload: list[dict] | None, kind: str, due_soon_days: int | None, about: str | None
) -> str:
    """Hash the exact checklist wire payload a push would send, for change detection.

    Args:
        payload: The redacted checklist items in wire shape (from _checklist_payload)
            — the list order is meaningful and preserved. None means the push carries NO
            checklist at all (the about-only path, S2.2 U3), which hashes differently from
            an empty list: "I am not talking about the checklist" and "the checklist is
            empty" are different claims, and collapsing them here would let a switch between
            the two slip past the change gate as "no change".
        kind: The project's kind ("project" | "tracker"), which rides the push.
        due_soon_days: The project's configured due-soon window, or None. Included even
            when None (as JSON null) so a config-only horizon change still changes the
            hash — that is what keeps a scheduled push refreshing the relay's due-soon
            flag (the KI-35 case-2 mitigation) under change-gating.
        about: The project's redacted About line, or None. Included (even when None) for
            the SAME reason as due_soon_days: About is content that rides this carrier, so
            an edit to ONLY the About source must change the hash — otherwise a scheduled
            `--all --due` push would skip it as "no change" and the dashboard's About would
            never refresh (a freshness-honesty regression the risk list calls out).

    Returns:
        A hex sha256 of the canonicalized {about, checklist, kind, due_soon_days} payload.

    Why:
        `checklist-push --all --due` (Unit 2) must skip a scheduled push whose content
        has not changed, so it can never make an untouched card look freshly updated
        (the relay stamps updated_at on every push). This hashes the exact WIRE payload
        push_checklist transmits — not the raw tasks_file — so a change that alters the
        wire form (e.g. due_soon_days or about) counts, and a raw-file edit that redaction
        erases does not spuriously re-push. sort_keys canonicalizes dict key order (item
        list order is left intact) so JSON key ordering can never fake or hide a change.
    """
    canonical = json.dumps(
        {
            "checklist": payload,
            "kind": kind,
            "due_soon_days": due_soon_days,
            "about": about,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _watch_tick(
    project: ProjectConfig, relay_cfg: RelayConfig, token: str, last_pushed: list | None
) -> tuple[list[dict], str | None, bool]:
    """One watch iteration: snapshot the checklist and push it only if it changed.

    Args:
        project: The project being watched.
        relay_cfg: The relay config (url to push to).
        token: The relay ingest Bearer token.
        last_pushed: The payload pushed on the previous successful tick, or None on the
            first tick (so the first snapshot always pushes).

    Returns:
        A (payload, about, pushed) triple: the current checklist payload, the redacted
        About sent with it (None when the project has no about_file), and whether THIS
        tick pushed (True when the checklist differed from last_pushed). On no change,
        returns (last_pushed, None, False) and performs no network call.

    Why:
        Factoring one iteration out of the loop makes the "push on change, skip when
        unchanged" rule unit-testable without an infinite loop. Content-compare (rather
        than file mtime) is robust to how editors save and self-dedupes a touch that
        did not actually change the checklist. The CHECKLIST is the change trigger (About
        is a separate file); when a checklist change fires a push we read About fresh and
        carry it along, and return it so the loop records the exact hash that was sent.
        May raise DeliveryError (the loop treats that as transient and retries next tick).
    """
    payload = _checklist_payload(project)
    if payload == last_pushed:
        return last_pushed, None, False
    about, _hits = _redacted_about(project)
    push_checklist(
        relay_cfg.url, project.name, payload, token,
        kind=project.kind, due_soon_days=project.due_soon_days, about=about,
    )
    return payload, about, True


def _watch_checklist(
    project: ProjectConfig,
    relay_cfg: RelayConfig,
    token: str,
    interval: float,
    conn: sqlite3.Connection,
) -> int:
    """Poll the project's checklist source and push the checklist whenever it changes.

    Args:
        project: The project to watch (its tasks_file and/or tracker_file is polled).
        relay_cfg: The relay config (push target).
        token: The relay ingest Bearer token.
        interval: Seconds between polls.
        conn: An open state connection. Each on-change push is recorded in
            checklist_push_history (E1.3) so a later scheduled `--due` run knows when
            this project last pushed and with what content.

    Returns:
        0 on a clean Ctrl-C stop.

    Why:
        The near-real-time mechanism: push once at startup, then re-read every
        `interval` seconds and push only when the redacted checklist changed. Polling
        (not OS filesystem events) is stdlib-only and identical on every platform; a
        few-second interval is near-real-time for a human editing a checklist. One
        project per process, mirroring relay-serve/bot as single foreground commands. A
        transient relay failure is reported and retried on the next tick rather than
        killing the watch.
    """
    watched = ", ".join(str(f) for f in _checklist_source_files(project))
    print(
        f"Watching {watched} for {project.name!r} "
        f"(every {interval:g}s; Ctrl-C to stop)...",
        file=sys.stderr,
    )
    last_pushed: list | None = None
    try:
        while True:
            try:
                last_pushed, sent_about, pushed = _watch_tick(
                    project, relay_cfg, token, last_pushed
                )
                if pushed:
                    # Record the push right after it landed (mirrors record_report's
                    # placement): a tick that pushed nothing records nothing. Recompute
                    # the hash from the just-pushed payload + the About actually sent —
                    # one cheap sha256 — so _watch_tick stays a pure push-transport helper
                    # with no state/time dependency of its own.
                    record_checklist_push(
                        conn,
                        project.name,
                        _checklist_content_hash(
                            last_pushed, project.kind, project.due_soon_days, sent_about
                        ),
                        datetime.now(timezone.utc).isoformat(),
                    )
                    print(
                        f"Pushed checklist for {project.name!r} "
                        f"({len(last_pushed)} item(s)).",
                        file=sys.stderr,
                    )
            except DeliveryError as exc:
                # Don't kill the watch on a transient failure (relay briefly down): keep
                # last_pushed unchanged so the next tick retries the same payload.
                print(f"Push failed (will retry): {exc}", file=sys.stderr)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped watching.", file=sys.stderr)
        return 0


def _push_checklist_all(
    projects: list[ProjectConfig],
    relay_cfg: RelayConfig,
    token: str,
    conn: sqlite3.Connection,
    now: datetime,
    due_only: bool,
) -> list[str]:
    """Push every checklist-enabled project once, fail-soft, returning per-project statuses.

    Args:
        projects: The full project list to sweep (config.projects.values()).
        relay_cfg: The relay config (push target).
        token: The relay ingest Bearer token.
        conn: An open state connection (last-push history is read here and advanced on push).
        now: The single run instant (tz-aware UTC), shared across the sweep so a long run
            can't drift a borderline project across its cadence edge, and used as the
            recorded pushed_at.
        due_only: When True, apply the cadence filter AND the content change-gate; when
            False (`--all` without `--due`), push every eligible project unconditionally.

    Returns:
        One STATUS_* value per project, in order — so the caller can print a tally and set
        the exit code.

    Why:
        The `--all` sweep, mirroring cmd_report's fail-soft loop shape: one project's relay
        failure is reported and the loop continues (exit 1 only on a genuine FAILED). It
        reuses the single-project path's payload/redaction/hash/record pieces per project.
        The change-gate (under --due only) is the honesty guard: since the relay stamps
        updated_at on every push, an unattended run must not re-push unchanged content and
        make an untouched card look freshly updated — cadence gates WHEN to check, the hash
        gates WHETHER to push. Manual `--all` (no --due) is explicit intent, so it pushes
        unconditionally.
    """
    statuses: list[str] = []
    for project in projects:
        # A project with no checklist to push is skipped, not an error: --all is a sweep,
        # not a per-project assertion that each one is pushable (mirrors the report loop
        # skipping a non-opted project).
        if not project.checklist or not _checklist_source_files(project):
            print(f"[{project.name}] no checklist to push; skipping.")
            statuses.append(STATUS_NO_CHECKLIST)
            continue

        # --due filters on cadence BEFORE any file read: a project pushed within its
        # interval is skipped without touching its tasks_file. `last` is reused below for
        # the change-gate, so we read it once here.
        last = get_last_checklist_push(conn, project.name)
        if due_only and not _is_due_at(
            project.cadence, last[0] if last is not None else None, now
        ):
            print(f"[{project.name}] not due yet (cadence={project.cadence}); skipping.")
            statuses.append(STATUS_NOT_DUE)
            continue

        # Build the exact wire payload (redaction runs inside _checklist_payload) and hash
        # it — needed both to change-gate under --due and to record after a push. About is
        # resolved+redacted here too so an About-only edit registers as a content change
        # (otherwise the --due gate would skip it and the dashboard's About would go stale).
        payload = _checklist_payload(project)
        about, _about_hits = _redacted_about(project)
        content_hash = _checklist_content_hash(
            payload, project.kind, project.due_soon_days, about
        )

        # Change-gate (ONLY under --due): skip a due project whose content is identical to
        # its last push, so an unattended run never re-stamps the relay's updated_at for an
        # untouched card. This is why "no cadence = always due" stays harmless (Decision 3):
        # a no-cadence project is always due, but pushes only when its content changed.
        if due_only and last is not None and last[1] == content_hash:
            print(f"[{project.name}] no change since last push; skipping.")
            statuses.append(STATUS_NO_CHANGE)
            continue

        try:
            push_checklist(
                relay_cfg.url, project.name, payload, token,
                kind=project.kind, due_soon_days=project.due_soon_days, about=about,
            )
            # Record only after a successful push (mirrors the single-project path): a
            # DeliveryError skips this, so a failed push leaves no history row.
            record_checklist_push(conn, project.name, content_hash, now.isoformat())
            print(f"[{project.name}] pushed checklist ({len(payload)} item(s)).")
            statuses.append(STATUS_PUSHED)
        except DeliveryError as exc:
            # Fail-soft: one project's relay failure is reported and the sweep continues.
            print(f"[{project.name}] push failed: {exc}", file=sys.stderr)
            statuses.append(STATUS_FAILED)
    return statuses


def _print_checklist_push_summary(statuses: list[str]) -> None:
    """Print a one-line tally of per-project outcomes after a `checklist-push --all` run.

    Args:
        statuses: One STATUS_* value per project, in the order they ran.

    Returns:
        None. Prints a single summary line to stdout.

    Why:
        An --all sweep can touch many projects; a human (or a cron log) needs a single
        glance to see what happened. Every category is shown so the numbers reconcile to
        the project count — the analogue of report's _print_all_summary, with the
        checklist lane's categories (no LLM/preview gate, but a change-gate).
    """
    counts = {
        STATUS_PUSHED: 0,
        STATUS_NO_CHANGE: 0,
        STATUS_NOT_DUE: 0,
        STATUS_NO_CHECKLIST: 0,
        STATUS_FAILED: 0,
    }
    for status in statuses:
        counts[status] += 1

    print(
        f"\n{len(statuses)} project(s): "
        f"{counts[STATUS_PUSHED]} pushed, "
        f"{counts[STATUS_NO_CHANGE]} unchanged, "
        f"{counts[STATUS_NOT_DUE]} not due, "
        f"{counts[STATUS_NO_CHECKLIST]} no checklist, "
        f"{counts[STATUS_FAILED]} failed."
    )


def cmd_checklist_push(
    project_name: str | None,
    config_path: Path,
    watch: bool,
    interval: float,
    all_projects: bool = False,
    due_only: bool = False,
    clear_due_soon_days: bool = False,
    clear_about: bool = False,
) -> int:
    """Push a project's current checklist to the relay (single project, --all, or --watch).

    Args:
        project_name: The project whose checklist to push, or None when --all is used.
        config_path: Path to orion.toml.
        watch: When True, run a foreground poll loop that pushes on every change to the
            project's checklist source until interrupted; when False, push once and exit.
            Single-project only (rejected with --all/--due).
        interval: Seconds between polls in --watch mode.
        all_projects: True for `--all` (push every checklist-enabled project in config).
        due_only: True for `--due` — with --all only, push just the projects DUE under
            their `cadence` and skip a due project whose content is unchanged since its
            last push (the scheduled-run surface). Defaults False so manual pushes are
            unconditional.
        clear_due_soon_days: True for `--clear-due-soon-days` — send an explicit clear for
            this project's due-soon horizon instead of its configured value. Single-project
            only (rejected with --all/--watch).
        clear_about: True for `--clear-about` — send an explicit clear for this project's
            stored About instead of its observed value. Single-project only, mirroring
            --clear-due-soon-days (KI-35: an absent value never clears).

    Returns:
        Process exit code: 2 for a usage error (neither/both of project & --all, --due
        without --all, --watch with --all/--due, or --clear-due-soon-days outside a
        single one-shot push); 1 on a setup error or a single-project
        delivery failure, OR if any project in an --all sweep genuinely FAILED; else 0.
        Not-due / no-change / no-checklist skips are all clean exit 0 (routine outcomes a
        scheduler should not alert on).

    Why:
        The dedicated checklist-only push: it updates ONLY the live checklist on the
        dashboard — no report — reusing the report path's redaction (_redacted_checklist)
        and the relay's ingest token. E1.3 adds `--all [--due]` so one scheduled entry can
        keep every tracker card fresh on its own cadence, change-gated for honesty. The
        single-project and --watch paths are unchanged.
    """
    # Validate the flag combination up front (argparse can't express these), mirroring
    # cmd_report's XOR handling — a clear message and exit 2 for each misuse.
    if all_projects and project_name is not None:
        print("Error: give either a project name or --all, not both.", file=sys.stderr)
        return 2
    if not all_projects and project_name is None:
        print(
            "Error: give a project name, or --all to push every checklist.",
            file=sys.stderr,
        )
        return 2
    if due_only and not all_projects:
        print(
            "Error: --due only applies with --all (it filters the all-projects set).",
            file=sys.stderr,
        )
        return 2
    if watch and (all_projects or due_only):
        print(
            "Error: --watch is single-project; it cannot combine with --all or --due.",
            file=sys.stderr,
        )
        return 2
    # KI-35: clearing a setting is a deliberate, single-shot act on ONE project — never
    # something a sweep or a poll loop repeats. Confining it to the one-shot single-project
    # push also means it never meets the --all --due change-gate, so a clear can't be
    # skipped as "no change" (the gate is only consulted in _push_checklist_all).
    if clear_due_soon_days and (all_projects or watch):
        print(
            "Error: --clear-due-soon-days is a single-project, one-shot act; it cannot "
            "combine with --all or --watch.",
            file=sys.stderr,
        )
        return 2
    if clear_about and (all_projects or watch):
        print(
            "Error: --clear-about is a single-project, one-shot act; it cannot combine "
            "with --all or --watch.",
            file=sys.stderr,
        )
        return 2

    try:
        config = load_config(config_path)
        load_secrets(config_path)
        relay_cfg = config.relay
        if not relay_cfg.enabled:
            raise ConfigError(
                f"checklist-push needs an enabled [relay] in {config_path} — it pushes "
                f"the checklist to the dashboard relay."
            )
        token = get_required(relay_cfg.token_env_var)
        # Open the state store so each successful push is logged (E1.3): the scheduled
        # `--due` path reads this history to gate on cadence and content change.
        conn = open_state(config.state_db)
        if all_projects:
            projects = list(config.projects.values())
        else:
            project = get_project(config, project_name)
            # S2.2 U3: a project that does not enable `checklist` but DOES set an about_file
            # takes the settings-only path — it pushes its About (and kind / horizon) with no
            # checklist on the wire, leaving any stored one untouched. This is the producer
            # half of the About-carrier decoupling: before it, a project with no tasks_file
            # could never get an About onto the dashboard, because the only carrier demanded
            # an unrelated field.
            about_only = not project.checklist and project.about_file is not None
            # The other single-project preconditions stay HARD errors: the user named this
            # one, so a misconfiguration is a mistake to surface, not a silent skip (unlike
            # the --all sweep, where a non-checklist project is simply passed over).
            if not project.checklist and not about_only:
                raise ConfigError(
                    f"Project {project.name!r} does not enable `checklist`, and has no "
                    f"`about_file` to push on its own. Set `checklist = true` (with a "
                    f"'tasks' or 'tracker' collector) to push its checklist, or set "
                    f"`about_file` to push just its About line."
                )
            # Only meaningful when a checklist was actually asked for: `checklist = true`
            # with nothing to read from is a real misconfiguration and stays loud.
            if project.checklist and not _checklist_source_files(project):
                raise ConfigError(
                    f"Project {project.name!r} has no tasks_file or tracker_file to read a "
                    f"checklist from."
                )
            if about_only and watch:
                # --watch polls the checklist SOURCE FILES for changes; an about-only
                # project has none, so the loop would have nothing to watch.
                raise ConfigError(
                    f"--watch polls {project.name!r}'s checklist source files, and it has "
                    f"none. Run the push without --watch to send its About."
                )
    except (ConfigError, SecretsError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if all_projects:
        # Single "now" for the whole sweep so every due check compares against the same
        # instant (a long run can't drift a borderline project across its cadence edge).
        now = datetime.now(timezone.utc)
        statuses = _push_checklist_all(projects, relay_cfg, token, conn, now, due_only)
        _print_checklist_push_summary(statuses)
        # Only a genuine FAILED is a non-zero exit; NOT_DUE / NO_CHANGE / NO_CHECKLIST are
        # routine outcomes a scheduler should not alert on.
        return 1 if any(status == STATUS_FAILED for status in statuses) else 0

    if watch:
        return _watch_checklist(project, relay_cfg, token, interval, conn)

    # One-shot single project: push the current checklist once. A delivery failure is fatal
    # here (unlike the watch loop, which retries), so the user sees a non-zero exit.
    try:
        # None (not []) on the about-only path: the relay then omits the key and leaves the
        # stored checklist alone. An empty list would claim the checklist is now empty.
        payload = None if about_only else _checklist_payload(project)
        # A clear sends an explicit null INSTEAD of the configured value, so the horizon we
        # actually put on the wire is None. The recorded hash must reflect what was sent,
        # not what config holds, or a later `--all --due` run would compare against a state
        # the relay never saw.
        sent_due_soon_days = None if clear_due_soon_days else project.due_soon_days
        # Same clear-vs-send rule for About: a clear puts None on the wire (and in the
        # recorded hash), otherwise we send the observed, redacted About.
        sent_about = None if clear_about else _redacted_about(project)[0]
        push_checklist(
            relay_cfg.url, project.name, payload, token,
            kind=project.kind, due_soon_days=sent_due_soon_days,
            clear_due_soon_days=clear_due_soon_days,
            about=sent_about, clear_about=clear_about,
        )
        # Record only after the push succeeded (mirrors record_report): a DeliveryError
        # below skips this, so a failed push leaves no history row and is retried later.
        record_checklist_push(
            conn,
            project.name,
            _checklist_content_hash(
                payload, project.kind, sent_due_soon_days, sent_about
            ),
            datetime.now(timezone.utc).isoformat(),
        )
    except DeliveryError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if payload is None:
        # Say what was actually sent. "Pushed checklist (0 items)" would be a small lie about
        # a push whose entire point was to leave the checklist alone.
        print(f"Pushed About for {project.name!r} (no checklist in this push).")
    else:
        print(f"Pushed checklist for {project.name!r} ({len(payload)} item(s)).")
    return 0


def _redacted_disciplines(
    project: ProjectConfig,
    extractor: DisciplineExtractor,
    conn: sqlite3.Connection,
) -> tuple[list[Discipline], int]:
    """Snapshot a project's disciplines and redact each card's text (the privacy net).

    Args:
        project: The project to read (its discipline_docs are the source).
        extractor: The configured DisciplineExtractor (called only on changed docs).
        conn: An open state connection, used for the extraction cache.

    Returns:
        A (items, hits) pair: the redacted Discipline list (a card whose title is
        ENTIRELY a secret — empty after redaction — is dropped to avoid a blank card),
        and the count of secrets scrubbed across the cards.

    Why:
        Mirrors _redacted_checklist: the dedicated disciplines push must apply the
        SAME redaction net to the observed text before it leaves the machine. The doc
        text was already redacted BEFORE the model (in the collector); this is the
        second pass on the model's OUTPUT (defense in depth, exactly as the summarizer
        body is redacted again after the LLM). Factoring it here keeps that guarantee
        in one place. The cache_get/cache_set closures bind the collector's storage to
        this project's "disciplines" namespace in the state store.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    raw = snapshot_disciplines(
        project.discipline_docs,
        project.repo_path,
        extractor,
        cache_get=lambda key: get_cache(conn, project.name, "disciplines", key),
        cache_set=lambda key, content_hash, value: set_cache(
            conn, project.name, "disciplines", key, content_hash, value, now
        ),
    )

    items: list[Discipline] = []
    hits = 0
    for d in raw:
        scrub_title = redact(d.title)
        scrub_why = redact(d.why)
        hits += scrub_title.hit_count + scrub_why.hit_count
        safe_title = scrub_title.text.strip()
        if not safe_title:
            # The whole title was a secret — drop the card rather than show a blank one.
            continue
        # `source` is a repo-relative path the collector stamped (not raw user prose),
        # so it rides through but is run through redact as a net; its hits are NOT
        # re-counted (it is not author free-text).
        safe_source = redact(d.source).text
        items.append(
            Discipline(
                title=safe_title,
                why=scrub_why.text.strip(),
                scope=d.scope,
                source=safe_source,
            )
        )
    return items, hits


def _disciplines_payload(
    project: ProjectConfig,
    extractor: DisciplineExtractor,
    conn: sqlite3.Connection,
) -> list[dict]:
    """The project's redacted disciplines as the wire payload (list of dicts).

    Why:
        Mirrors _checklist_payload: derive the exact {title, why, scope, source} shape
        push_disciplines sends, in one place, over _redacted_disciplines.
    """
    items, _hits = _redacted_disciplines(project, extractor, conn)
    return [d.as_dict() for d in items]


def cmd_disciplines_push(
    project_name: str, config_path: Path, *, clear: bool = False
) -> int:
    """Extract a project's disciplines from its docs and push them to the relay.

    Args:
        project_name: The project whose disciplines to push.
        config_path: Path to orion.toml.
        clear: When True, push an EMPTY set deliberately — the explicit way to
            retire a project's cards. Skips the docs, the extractor and the API
            key entirely, since clearing observes nothing.

    Returns:
        Process exit code: 0 on success, 1 on a setup or delivery error.

    Why:
        The dedicated disciplines push (E2 Inc 4 slice 4b): it sets ONLY the project's
        observed principles on the dashboard — no report — exactly as checklist-push
        sets the live checklist. It reads the project's own docs UNMODIFIED, reframes
        their stated principles via the configured (opt-in) LLM extractor (cache-gated,
        so the model runs only on a changed doc), redacts, and pushes. It requires the
        'disciplines' collector enabled (with discipline_docs) and an enabled [relay].
    """
    try:
        config = load_config(config_path)
        load_secrets(config_path)
        project = get_project(config, project_name)
        relay_cfg = config.relay
        if not relay_cfg.enabled:
            raise ConfigError(
                f"disciplines-push needs an enabled [relay] in {config_path} — it "
                f"pushes the disciplines to the dashboard relay."
            )
        if "disciplines" not in project.collectors:
            raise ConfigError(
                f"Project {project.name!r} does not enable the 'disciplines' collector. "
                f"Add 'disciplines' to its collectors (with a `discipline_docs` list) to "
                f"push its observed principles."
            )
        token = get_required(relay_cfg.token_env_var)
        # Clearing observes nothing, so it needs no docs, no model and no API key —
        # building the extractor here would demand a key just to empty a section.
        extractor = (
            None if clear else _build_extractor(config.summarizer, get_required)
        )
    except (ConfigError, SecretsError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    conn = open_state(config.state_db)
    try:
        payload = [] if clear else _disciplines_payload(project, extractor, conn)

        # The push is a full-state REPLACE, so an empty payload wipes the project's
        # cards. Empty is reachable without anyone intending it: the snapshot is
        # deliberately fail-soft, so a renamed/moved doc, a config whose relative
        # `discipline_docs` resolves somewhere else, or an extraction that failed for
        # every doc all yield zero cards — and the wipe then reports success. Refuse
        # it, and make clearing say so out loud (`--clear`), which keeps "observed
        # nothing" and "asked for nothing" distinct instead of collapsing both into
        # an empty push. Mirrors intake's "Refusing to send an empty update" guard,
        # and the empty-clobber guard the skills batch endpoint already carried.
        if not payload and not clear:
            print(
                f"Refusing to push an empty discipline set for {project.name!r} — "
                f"it would replace the project's current cards with nothing.\n"
                f"  Configured docs: "
                f"{', '.join(str(d) for d in project.discipline_docs) or '(none)'}\n"
                f"  Check each path exists and is readable (a relative path resolves "
                f"next to the CONFIG file, not the repo), then retry.\n"
                f"  To retire the cards on purpose, run: orion disciplines-push "
                f"{project.name} --clear",
                file=sys.stderr,
            )
            return 1

        push_disciplines(relay_cfg.url, project.name, payload, token)
    except DeliveryError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if clear:
        print(f"Cleared disciplines for {project.name!r} (0 card(s)).")
    else:
        print(f"Pushed disciplines for {project.name!r} ({len(payload)} card(s)).")
    return 0


def cmd_intake(
    project_name: str, config_path: Path, message: str | None, assume_yes: bool
) -> int:
    """Send a pushed/hand-written update for a project, skipping collectors.

    Args:
        project_name: The project to send the update for.
        config_path: Path to orion.toml.
        message: The update body, or None to read it from stdin.
        assume_yes: True for a non-interactive send (the `--yes` flag). When set,
            the terminal preview is skipped; otherwise it shows as usual.

    Returns:
        Exit code: 0 if the update was sent or the user declined; 1 on any error
        or if no delivery succeeded.

    Why:
        Intake is the structured lane in its purest form: the body IS the update,
        already audience-ready, so there is NO collector, NO LLM, and NO delta
        marker (running intake twice deliberately sends twice — it is a push, not
        a delta). This is the entry point the Claude session skill (B2) uses. It
        still runs the full safety path — two redaction passes — because a pushed
        body can contain a secret just as easily as a git diff can.

        The `--yes` preview skip exists for that skill: a skill runs intake through
        a non-interactive shell, where the terminal preview would get EOF and
        fail-close to "Aborted" — so it could never send. With --yes the human gate
        moves into the session (the skill shows the summary for approval before
        invoking this). Unlike `report --yes`, there is NO auto_send-style gate:
        report can run unattended (cron), but intake is ALWAYS an explicit push,
        so a deliberate --yes is sufficient. Redaction is unchanged either way.
    """
    try:
        config = load_config(config_path)
        project = get_project(config, project_name)
        load_secrets(config_path)
        conn = open_state(config.state_db)

        # The body comes from --message, or from stdin when that is omitted (so a
        # skill or shell pipe can feed a summary in: `summarize | orion intake p`).
        body = message if message is not None else sys.stdin.read()
        if not body.strip():
            print("Refusing to send an empty update.", file=sys.stderr)
            return 1

        # Two redaction passes, mirroring the report path, so a pushed secret is
        # scrubbed with the same defense-in-depth (the second pass is the safety
        # net on the exact bytes that will be sent).
        pass1 = redact(body)
        pass2 = redact(pass1.text)
        safe_body = pass2.text
        redaction_hits = pass1.hit_count + pass2.hit_count
        if not safe_body.strip():
            print(
                "Refusing to send: the update is empty after redaction.",
                file=sys.stderr,
            )
            return 1

        # A pushed update is already audience-ready: structured lane.
        generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        blob = build_report(project, safe_body, LANE_STRUCTURED, generated_at)

        # Compose per distinct channel and route each recipient accordingly —
        # identical delivery path to cmd_report (just no markers afterward). Intake
        # is deliberately UNFILTERED: a pushed body has no per-signal sections, so a
        # recipient's `signals` filter cannot apply — every recipient gets the push,
        # keyed only by channel. (D5 filtering lives on the collected-report path.)
        messages = {
            ch: compose(blob, ch, config.display_timezone)
            for ch in _channels(project)
        }
        multi = len(messages) > 1
        previews = [(ch if multi else "", messages[ch]) for ch in messages]
        # --yes skips the terminal preview (the skill already showed the summary
        # for in-session approval); otherwise preview-before-send as usual.
        if assume_yes:
            print(f"Sending {project.name!r} (preview skipped: --yes).")
        elif not _preview_and_confirm(previews, redaction_hits):
            print("Aborted. Nothing was sent.")
            return 0

        sent_to, failed = _deliver(messages, project.recipients, lambda r: r.channel)
        if not sent_to:
            print("No deliveries succeeded.", file=sys.stderr)
            return 1

        # Record history, but advance NO marker — there is no delta to track for a
        # push (this is also why intake works for a never-reported project).
        record_report(conn, project.name, safe_body, sent_to, generated_at)

        print(f"Sent to: {', '.join(sent_to)}.")
        if failed:
            print(f"(Note: {len(failed)} recipient(s) failed.)")

        # Same additive, fail-soft relay push as the report path — one push of the
        # same portable blob, after the history record, so intake feeds the
        # dashboard too without affecting the send outcome.
        _relay_push(blob, config.relay)
        return 0

    except (ConfigError, SecretsError) as exc:
        # Git/Summarizer/Tasks/Notes errors cannot occur on this path (no
        # collectors, no LLM), so only config/secrets setup errors are expected.
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def _confirm_backfill(
    project: str, relay_url: str, generated_at: str, body: str, redaction_hits: int
) -> bool:
    """Preview a relay-backfill push and ask the user to confirm.

    Args:
        project: The project the report belongs to.
        relay_url: The relay the blob will be POSTed to.
        generated_at: The normalized ISO 8601 UTC timestamp the card will carry.
        body: The REDACTED body that will be pushed (the exact bytes).
        redaction_hits: How many potential secrets were scrubbed from the body.

    Returns:
        True only if the user explicitly confirms (y/yes); False otherwise (a bare
        Enter, EOF, or anything else does not push — fail closed).

    Why:
        Preview-before-send, adapted for the relay-only lane: the chat-framed
        _preview_and_confirm renders a ComposedMessage for a channel, which a
        backfill has no notion of (it never composes or delivers to a recipient). So
        this shows exactly what will reach the relay — project, target, timestamp,
        and the redacted body — and defaults to NO. It doubles as the idempotence
        guard: the relay's report history is append-only, so a re-run would add a
        duplicate row; the human confirming each push is what prevents that.
    """
    bar = "=" * 60
    print(bar)
    print("PREVIEW — backfill to the relay dashboard (NOT pushed yet)")
    print(bar)
    print(f"project:      {project}")
    print(f"relay:        {relay_url}")
    print(f"generated_at: {generated_at}")
    print("-" * 60)
    print(body)
    print(bar)
    if redaction_hits > 0:
        print(f"⚠  {redaction_hits} potential secret(s) were redacted from this report.")
    try:
        answer = input("Push this report to the relay? [y/N] ")
    except EOFError:
        # No interactive input available -> treat as "no" (fail closed).
        return False
    return answer.strip().lower() in ("y", "yes")


def cmd_relay_backfill(
    project_name: str,
    config_path: Path,
    body_file: Path | None,
    generated_at: str,
    assume_yes: bool,
) -> int:
    """Push an already-sent report's content onto the relay dashboard (relay-only, chat-silent).

    Args:
        project_name: The project the report belongs to.
        config_path: Path to orion.toml.
        body_file: A file holding the exact report body, or None to read it from stdin.
        generated_at: ISO 8601 timestamp of when the report was ORIGINALLY sent (the
            user reads it off the delivered message). A naive value is treated as UTC.
        assume_yes: True to skip the preview/confirm (the content was already delivered
            once, so a knowing re-push is sufficient); redaction still runs.

    Returns:
        Exit code: 2 for a malformed --generated-at; 1 on a setup error, an empty
        body, an unreadable --body-file, or a relay push failure; 0 on a successful
        push or a user-declined preview.

    Why:
        Reports sent to a project BEFORE its relay grant landed never reached the
        dashboard — ingest 404'd and the fail-soft _relay_push dropped them (KI-36).
        This is the one-time recovery path: it takes the exact report content (which
        the user still has in Slack/Discord) and pushes it onto the relay's
        append-only history, at the original timestamp, WITHOUT re-delivering to any
        chat recipient (that is the whole difference from `intake`, which always sends
        to chat too). It reuses the report path's two-pass redaction, build_report,
        and the relay transport; sections=() so the relay renders `body` as one
        untitled section, exactly like an intake push. lane=structured because no LLM
        runs here — the supplied body is pushed verbatim. Deliberately one report per
        invocation: it sidesteps any multi-report delimiter parsing and mirrors
        intake's single-body idiom (a batch/history-replay mode is a recorded
        follow-on). Unlike the report path's fail-soft relay push, a failure here is
        fatal — landing the report on the relay IS the point.
    """
    try:
        config = load_config(config_path)
        project = get_project(config, project_name)
        load_secrets(config_path)
        relay_cfg = config.relay
        if not relay_cfg.enabled:
            raise ConfigError(
                f"relay-backfill needs an enabled [relay] in {config_path} — it pushes "
                f"the report to the dashboard relay."
            )
        token = get_required(relay_cfg.token_env_var)
    except (ConfigError, SecretsError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # Validate + normalize the original send time up front (before reading the body):
    # a malformed timestamp is a usage error the caller must fix. Normalize to tz-aware
    # UTC ISO so the relay orders the card correctly (a naive value is assumed UTC).
    try:
        dt = datetime.fromisoformat(generated_at)
    except ValueError:
        print(
            f"Error: --generated-at must be an ISO 8601 timestamp "
            f"(e.g. 2026-07-17T09:15:54+00:00), got {generated_at!r}.",
            file=sys.stderr,
        )
        return 2
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    generated_at_norm = dt.astimezone(timezone.utc).isoformat(timespec="seconds")

    # The body comes from --body-file, or from stdin when that is omitted.
    if body_file is not None:
        try:
            body_text = body_file.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Error: could not read --body-file {body_file}: {exc}", file=sys.stderr)
            return 1
    else:
        body_text = sys.stdin.read()
    if not body_text.strip():
        print("Refusing to backfill an empty report.", file=sys.stderr)
        return 1

    # Two redaction passes, mirroring the report/intake path — a historical report body
    # can carry a secret (a token pasted into a report) just as a live one can. This is
    # the non-negotiable net; only the human PREVIEW is relaxable, never redaction.
    pass1 = redact(body_text)
    pass2 = redact(pass1.text)
    safe_body = pass2.text
    redaction_hits = pass1.hit_count + pass2.hit_count
    if not safe_body.strip():
        print(
            "Refusing to backfill: the report is empty after redaction.",
            file=sys.stderr,
        )
        return 1

    # sections=() → the relay renders `body` as one untitled section (intake-style).
    # participants/share_level/orion_version come from build_report (current config —
    # approximate for a historical report, which is acceptable for a recovery push).
    blob = build_report(project, safe_body, LANE_STRUCTURED, generated_at_norm)

    if assume_yes:
        print(f"Backfilling {project.name!r} to the relay (preview skipped: --yes).")
    elif not _confirm_backfill(
        project.name, relay_cfg.url, generated_at_norm, safe_body, redaction_hits
    ):
        print("Aborted. Nothing was pushed.")
        return 0

    # Relay-only, chat-silent: NO compose, NO channel delivery. Unlike the report
    # path's fail-soft _relay_push, a failure here is fatal (exit 1) — the whole point
    # is to land the report on the dashboard, so the user must see if it didn't.
    try:
        relay_push(serialize_blob(blob), relay_cfg.url, token)
    except DeliveryError as exc:
        print(f"Error: relay push failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"Backfilled 1 report for {project.name!r} to the relay "
        f"(generated_at={generated_at_norm})."
    )
    return 0


def cmd_install_hook(
    project_name: str,
    config_path: Path,
    hook_type: str,
    print_only: bool,
    force: bool,
) -> int:
    """Install (or print) a git hook that auto-reports a project on a git event.

    Args:
        project_name: The project the hook should report on.
        config_path: Path to orion.toml.
        hook_type: Which hook to install (one of hooks.SUPPORTED_HOOKS).
        print_only: When True, print the script to stdout and write nothing.
        force: When True, overwrite an existing hook of the same name.

    Returns:
        Exit code: 0 on success (or a clean --print), 1 on a setup error or a
        refused overwrite.

    Why:
        This is the only command that writes into the user's repository, so it is
        deliberately careful: it refuses to clobber an existing hook unless --force
        (a repo may already use husky/pre-commit), offers --print to review the
        exact script before installing, and never changes the report pipeline — the
        installed hook just calls `report --yes`, so all redaction/auto_send
        guarantees carry over unchanged. The actual report runs in the background
        and always exits 0 (see hooks.build_hook_script), so the hook can never
        delay or block a commit/push.
    """
    try:
        config = load_config(config_path)
        project = get_project(config, project_name)
        # Ask git where hooks live (correct for worktrees / core.hooksPath), which
        # also validates repo_path is a real repo — both as a clean ConfigError /
        # GitError rather than a later surprise.
        hooks_dir = resolve_hooks_dir(project.repo_path)
    except (ConfigError, GitError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # Embed ABSOLUTE paths so the hook works from git's minimal environment no
    # matter the cwd: the venv's own python (sys.executable), the resolved config,
    # and a log alongside the git dir (hooks_dir's parent).
    config_abs = config_path.resolve()
    log_path = hooks_dir.parent / "orion-hook.log"
    script = build_hook_script(
        sys.executable, project.name, config_abs, log_path, hook_type
    )

    # --print: show the exact script, write nothing (review-before-install).
    if print_only:
        print(script, end="")
        return 0

    hook_file = hooks_dir / hook_type
    if hook_file.exists() and not force:
        print(
            f"Refusing to overwrite existing hook: {hook_file}\n"
            f"Re-run with --force to replace it, or with --print to view the script.",
            file=sys.stderr,
        )
        return 1

    # git creates .git/hooks on init, but mkdir(exist_ok) is cheap insurance for an
    # unusual layout (e.g. a custom core.hooksPath directory that doesn't exist yet).
    hooks_dir.mkdir(parents=True, exist_ok=True)
    # newline="\n": never let Windows text mode turn the script into CRLF — a CRLF
    # after the shebang ("#!/bin/sh\r") breaks the interpreter lookup under sh.
    hook_file.write_text(script, encoding="utf-8", newline="\n")
    # Make it executable for git on POSIX; harmless on Windows (git runs hooks via
    # its bundled sh regardless of the exec bit), so no platform branch is needed.
    hook_file.chmod(0o755)

    print(f"Installed {hook_type} hook: {hook_file}")
    print(f"  It runs `report {project.name!r} --yes` and logs to {log_path}.")
    if not project.auto_send:
        print(
            f"  Note: {project.name!r} has auto_send=false, so the hook will run but "
            f"SKIP sending (nothing is delivered) until you set auto_send=true in "
            f"{config_path}."
        )
    return 0


def _confirm(prompt: str) -> bool:
    """Ask a yes/no question on stdin, defaulting to NO.

    Args:
        prompt: The question to show (should end with a trailing space).

    Returns:
        True only if the user types y/yes; False on anything else or no stdin.

    Why:
        The preview-before-write gate for `add-project`, mirroring the report
        preview (_preview_and_confirm). A non-interactive stdin (EOFError) means
        "not confirmed" — safer than assuming yes for a command that writes a file.
    """
    try:
        answer = input(prompt)
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def _git_toplevel(start: Path) -> Path | None:
    """Return the git work-tree root containing `start`, or None if not in one.

    Args:
        start: Directory to look from (normally the current working directory).

    Returns:
        The repo's top-level Path, or None when `start` isn't in a git repo or
        git isn't installed.

    Why:
        `add-project` infers a project's repo_path from where it is run, so
        `cd myproject && orion add-project` just works — even from a subdirectory,
        because we ask git for the work-tree root rather than using cwd directly.
        Failure is a soft None (the caller falls back to cwd), never an error:
        inference is a convenience, not a requirement.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    out = completed.stdout.strip()
    return Path(out) if out else None


def _starter_checklist(project_name: str) -> str:
    """Return the seed text for a tasks_file that add-project creates.

    Args:
        project_name: The project the checklist belongs to (named in the comment).

    Returns:
        A minimal Markdown checklist: a "# TODO" header and a short usage comment, with
        NO checkbox items.

    Why:
        E2 Inc 2.6 lets add-project create a project's tasks_file so a new project has a
        checklist surface from the start. Seeding a header + comment teaches the
        "- [ ]" / "- [x]" format without inventing fake tasks — the snapshot parser
        ignores the comment, so the dashboard checklist starts EMPTY (no placeholder
        row) until the user adds real items.
    """
    return (
        "# TODO\n"
        "\n"
        f'<!-- Orion checklist for "{project_name}". Use GitHub-style checkboxes:\n'
        '     "- [ ]" is an open item, "- [x]" is done. Items appear on the dashboard. -->\n'
    )


# --- --seed-tasks-from: build a starter checklist from a doc's Markdown tables ----------
# Preference order for the column whose cells become each checklist item's text. The first
# header that matches (case-insensitive) wins, so a roadmap table keyed by "Scope" and a
# to-do table keyed by "Task" both work without configuration. Matches the column NAMES the
# tracker/incubator collectors already read (see collectors/_markdown.Table).
_SEED_TEXT_HEADERS = ("task", "scope", "sub-goal", "item", "milestone", "name")

# Substrings (case-insensitive) in a "status" cell that flag an item as already done. ✅ and
# a "[x]" checkbox are unambiguous, so they match as plain substrings. The word markers are
# matched on WORD BOUNDARIES instead of as bare substrings so a status like "incomplete"
# does not match "complete" — and a cell containing a standalone "not" (e.g. "not done") is
# never treated as done. This hardening goes slightly beyond the literal "contains a marker"
# spec because the seed source is an arbitrary user doc in a public tool (settled 2026-06-25).
_SEED_DONE_SUBSTRINGS = ("✅", "[x]")
_SEED_DONE_WORD_RE = re.compile(r"\b(done|shipped|complete|signed off)\b", re.IGNORECASE)
_SEED_NEGATION_RE = re.compile(r"\bnot\b", re.IGNORECASE)


def _status_is_done(status_cell: str) -> bool:
    """Decide whether a tracker/roadmap status cell marks its row as complete.

    Args:
        status_cell: The raw text of the row's status column (may be empty).

    Returns:
        True when the cell signals a finished item, False otherwise.

    Why:
        Seeding maps a roadmap's status column onto checkbox state. ✅ / "[x]" are taken
        verbatim; the word markers use word boundaries (so "incomplete" ≠ "complete") and a
        standalone "not" vetoes a match (so "not done" stays open) — the cheap guards that
        keep an arbitrary user doc from producing wrong checkboxes.
    """
    cell = status_cell.casefold()
    if any(sub in cell for sub in _SEED_DONE_SUBSTRINGS):
        return True
    if _SEED_NEGATION_RE.search(cell):
        return False
    return _SEED_DONE_WORD_RE.search(cell) is not None


def _seed_lines_from_table(table: Table) -> list[str]:
    """Turn one parsed Markdown table into GitHub-style checklist lines.

    Args:
        table: A parse_tables() result — its `headers` and per-row `rows` dicts.

    Returns:
        One "- [ ] <text>" (or "- [x] <text>") line per row that has non-empty text in the
        chosen text column. An empty list when the table has no recognized text column.

    Why:
        Tables are read by column NAME, not position (the same contract the tracker uses), so
        a table is usable iff one of the preferred text headers is present. Picking the text
        column here (not in the caller) keeps the per-table rule in one place and lets the
        caller simply concatenate the lines of every table in the doc.
    """
    # Map case-folded header -> the actual header string, so a preference lookup is O(1) and
    # case-insensitive. Last duplicate header wins, which is irrelevant for well-formed tables.
    header_by_fold = {h.casefold(): h for h in table.headers}
    text_header = next(
        (header_by_fold[pref] for pref in _SEED_TEXT_HEADERS if pref in header_by_fold),
        None,
    )
    if text_header is None:
        return []  # no usable text column — skip this table entirely
    status_header = header_by_fold.get("status")

    lines: list[str] = []
    for row in table.rows:
        text = (row.get(text_header) or "").strip()
        if not text:
            continue  # a row with no item text contributes no checkbox
        done = status_header is not None and _status_is_done(row.get(status_header) or "")
        lines.append(f"- [{'x' if done else ' '}] {text}")
    return lines


def _seed_checklist_from_doc(doc_path: Path, project_name: str) -> str | None:
    """Build tasks_file checklist text from a doc's Markdown tables, or None if unusable.

    Args:
        doc_path: The --seed-tasks-from document to parse.
        project_name: The project the checklist belongs to (named in the header comment).

    Returns:
        Ready-to-write Markdown (a "# TODO" header comment plus one checkbox line per table
        row), or None when the doc cannot be read or has no table with a recognized text
        column — the signal for the caller to fall back to the empty starter.

    Why:
        This is the parse-not-generate seed path (no LLM): a roadmap/to-do doc the user
        already maintains becomes the new project's starting checklist. Reuses
        _markdown.parse_tables (the DRY seam shared with the tracker/incubator collectors) so
        the same table-reading rules apply. Returning None rather than raising keeps the
        "never fail the add" contract — an unparseable doc just yields the empty starter.
    """
    try:
        text = doc_path.read_text(encoding="utf-8")
    except OSError:
        return None  # unreadable/missing doc → caller falls back to the starter

    lines: list[str] = []
    for table in parse_tables(text):
        lines.extend(_seed_lines_from_table(table))
    if not lines:
        return None  # no usable table in the doc

    header = (
        "# TODO\n"
        "\n"
        f'<!-- Orion checklist for "{project_name}", seeded from {doc_path.name}.\n'
        '     "- [ ]" is an open item, "- [x]" is done. Items appear on the dashboard. -->\n'
        "\n"
    )
    return header + "\n".join(lines) + "\n"


def cmd_add_project(
    name: str | None,
    config_path: Path,
    *,
    repo_path: str | None,
    like: str | None,
    recipient_specs: list[str],
    share_level: str,
    collectors_csv: str,
    tasks_file: str | None,
    notes_file: str | None,
    print_only: bool,
    assume_yes: bool,
    tracker_file: str | None = None,
    incubator_file: str | None = None,
    seed_tasks_from: str | None = None,
) -> int:
    """Register a new project by appending (or creating) a stanza in orion.toml.

    Args:
        name: The project name, or None to infer it from the repo directory.
        config_path: Path to orion.toml (created if it does not exist).
        repo_path: The git repo path, or None to infer from the current repo/cwd.
        like: An existing project whose recipients to copy, or None.
        recipient_specs: Explicit "Name:channel:ENV_VAR" recipients (may be empty).
        share_level: One of SHARE_LEVELS.
        collectors_csv: Comma-separated collector names (e.g. "git,tasks").
        tasks_file: Path for the tasks collector. When "tasks" is enabled and this is
            None, it defaults to <repo>/TODO.md and that file is CREATED (a starter
            checklist), preview-gated and never overwriting an existing file. Pass an
            explicit path to opt out of creation (config-only, as before).
        notes_file: Path for the notes collector (required if "notes" enabled).
        print_only: Print the stanza and write nothing.
        assume_yes: Skip the preview confirmation (for non-interactive callers).
        tracker_file: Path for the tracker collector (required if "tracker" enabled).
            Unlike tasks, no file is created — a tracker points at a rich user doc.
        incubator_file: Path for the incubator collector (required if "incubator"
            enabled). Like tracker, config-only (no file creation).
        seed_tasks_from: When a tasks_file is being CREATED (the defaulted-tasks flow),
            seed its checklist from this doc's Markdown tables instead of the empty
            starter. Ignored (with a warning) when no tasks_file is being created; a doc
            with no usable table falls back to the starter (never fails the add).

    Returns:
        Exit code: 0 on success or a declined preview; 1 on any error.

    Why:
        This is Orion's ONLY config writer, added to kill the onboarding friction
        the dogfood surfaced (no way to register a project from its own directory).
        It keeps the invariant's spirit — config is never written as a side effect
        of a run — by being explicit, preview-gated, and append-only (it never
        rewrites existing content, so hand-written comments and ordering survive).
        The validating/rendering lives in scaffold.py; this function owns inference
        and file I/O, mirroring the cmd_install_hook / build_hook_script split.
    """
    try:
        # 1. Infer the repo path: an explicit flag wins; else this git repo's root;
        #    else the cwd. A relative --repo-path resolves against the cwd.
        if repo_path is not None:
            resolved_repo = Path(repo_path).expanduser()
            if not resolved_repo.is_absolute():
                resolved_repo = (Path.cwd() / resolved_repo).resolve()
        else:
            resolved_repo = _git_toplevel(Path.cwd()) or Path.cwd()

        # 2. Infer the name from the repo directory when not given.
        project_name = name if name else resolved_repo.name

        # 3. Load the existing config (if any). It gives us the projects to copy
        #    from (--like) and to check for a duplicate, and it validates the file
        #    we are about to append to (we never append to a broken config).
        config_exists = config_path.exists()
        config = load_config(config_path) if config_exists else None

        if config is not None and project_name in config.projects:
            print(
                f"Error: project {project_name!r} already exists in {config_path}. "
                f"Pick a different name (pass it as the first argument), or edit the "
                f"config by hand to change the existing one.",
                file=sys.stderr,
            )
            return 1

        # 4. Resolve recipients: those copied from --like, plus any explicit
        #    --recipient specs. A project needs at least one (the loader requires it).
        recipients: list[Recipient] = []
        if like is not None:
            if config is None:
                print(
                    f"Error: --like {like!r} needs an existing config to copy from, but "
                    f"none was found at {config_path}. Use --recipient for the first "
                    f"project instead.",
                    file=sys.stderr,
                )
                return 1
            recipients.extend(get_project(config, like).recipients)
        recipients.extend(parse_recipient_spec(spec) for spec in recipient_specs)

        if not recipients:
            print(
                "Error: no recipients given. Use --like <project> to copy an existing "
                'project\'s recipients, or --recipient "Name:channel:ENV_VAR" '
                "(repeatable).",
                file=sys.stderr,
            )
            return 1

        # 5. Render the stanza. This validates the name, share level, collectors,
        #    and collector/file pairing, and rejects values it cannot safely quote.
        collectors = tuple(c.strip() for c in collectors_csv.split(",") if c.strip())

        # B-i (E2 Inc 2.6): when the tasks collector is enabled but no --tasks-file was
        # given, default it to <repo>/TODO.md so a new project gets a checklist surface
        # without a second flag. We remember that we DEFAULTED, because only a defaulted
        # path is auto-created below — an explicit --tasks-file keeps the prior
        # config-only behavior (passing your own path is the opt-out of file creation).
        tasks_file_defaulted = "tasks" in collectors and tasks_file is None
        if tasks_file_defaulted:
            tasks_file = str(resolved_repo / "TODO.md")

        stanza = render_project_stanza(
            project_name,
            resolved_repo,
            share_level,
            collectors,
            tuple(recipients),
            tasks_file=tasks_file,
            notes_file=notes_file,
            incubator_file=incubator_file,
            tracker_file=tracker_file,
            with_state_db=not config_exists,
        )
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # Will we also create a starter checklist? Only for a DEFAULTED tasks_file that does
    # not already exist — never overwrite a user's file, and never touch an explicit
    # path. tasks_file is a real str here whenever tasks_file_defaulted is True.
    new_tasks_path = Path(tasks_file).expanduser() if tasks_file_defaulted else None
    create_tasks_file = new_tasks_path is not None and not new_tasks_path.exists()

    # Unit 3 (E2 Inc 2.6 follow-on): when a tasks_file is being CREATED and --seed-tasks-from
    # was given, seed the checklist from that doc's Markdown tables instead of the empty
    # starter. The content is decided here (before the preview) so the gate can describe it
    # honestly. A doc with no usable table → warn and fall back to the starter (never fail).
    checklist_text = _starter_checklist(project_name) if create_tasks_file else None
    seeded_from: str | None = None
    if seed_tasks_from is not None and not create_tasks_file:
        # The flag only acts when a new tasks_file is being created; say so rather than
        # silently ignoring it (tasks off, an explicit --tasks-file, or the file exists).
        print(
            f"Warning: --seed-tasks-from {seed_tasks_from!r} ignored — no new tasks file is "
            "being created (enable 'tasks' without --tasks-file to create one).",
            file=sys.stderr,
        )
    elif seed_tasks_from is not None and create_tasks_file:
        seeded = _seed_checklist_from_doc(Path(seed_tasks_from).expanduser(), project_name)
        if seeded is None:
            print(
                f"Warning: --seed-tasks-from {seed_tasks_from!r} had no usable table; "
                "using the empty starter checklist instead.",
                file=sys.stderr,
            )
        else:
            checklist_text = seeded
            seeded_from = seed_tasks_from

    # 6. --print: show exactly what would be written, change nothing.
    if print_only:
        print(stanza, end="")
        return 0

    # 7. Preview-before-write: the human gate for the new write surface.
    if not assume_yes:
        bar = "=" * 60
        action = "create" if not config_exists else "append to"
        print(bar)
        print(f"PREVIEW — would {action} {config_path} (nothing written yet)")
        if create_tasks_file:
            # The file creation is a SECOND write surface; surface it in the same gate
            # so a single decline declines both. Name the seed source when there is one.
            if seeded_from is not None:
                print(f"           and seed a checklist at {new_tasks_path} from {seeded_from}")
            else:
                print(f"           and create a starter checklist at {new_tasks_path}")
        print(bar)
        print(stanza, end="")
        print(bar)
        if not _confirm("Write this to the config? [y/N] "):
            print("Aborted. Nothing was written.")
            return 0

    # 8. Write: create a new file, or append a blank-line-separated stanza to the
    #    existing one. newline="\n" keeps the file LF on every OS, matching the
    #    rest of the config and avoiding a CRLF surprise on Windows.
    if config_exists:
        existing = config_path.read_text(encoding="utf-8")
        combined = existing.rstrip("\n") + "\n\n" + stanza
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        combined = stanza
    config_path.write_text(combined, encoding="utf-8", newline="\n")

    # 8b. Create the starter checklist for a defaulted tasks_file (E2 Inc 2.6). Re-check
    #     existence right before writing so a file that appeared in the meantime is never
    #     overwritten — the create is strictly additive, like the config append.
    if create_tasks_file and not new_tasks_path.exists():
        new_tasks_path.parent.mkdir(parents=True, exist_ok=True)
        # checklist_text is the seeded content when --seed-tasks-from produced a usable
        # table, else the empty starter (both decided above, before the preview gate).
        new_tasks_path.write_text(checklist_text, encoding="utf-8", newline="\n")

    # 9. Re-load to prove the written file parses and the project is present — the
    #    same belt-and-suspenders idea as report's "redact again before send".
    try:
        load_config(config_path)
    except ConfigError as exc:
        print(
            f"Error: wrote {config_path} but it failed to re-load ({exc}). Please review it.",
            file=sys.stderr,
        )
        return 1

    # 10. Confirm, and point at the remaining manual steps (secrets stay in .env).
    print(f"Registered {project_name!r} in {config_path}.")
    if create_tasks_file:
        if seeded_from is not None:
            print(f"  Seeded a checklist at {new_tasks_path} from {seeded_from}. Review and edit it.")
        else:
            print(f"  Created a starter checklist at {new_tasks_path}. Add your tasks there.")
    env_vars = sorted({r.webhook_env_var for r in recipients})
    print(f"  Next: set the webhook URL(s) in your .env — {', '.join(env_vars)}")
    # `check` validates the WHOLE config and takes no project argument — naming one
    # here made the first command a new user is told to run fail outright.
    print("  Then: orion check")
    return 0


def _resolve_incubator_index(
    config_path: Path, incubator_file: str | None, incubator: str | None
) -> Path:
    """Find the incubator index file to read for `graduate-idea`.

    Args:
        config_path: Path to orion.toml (used to locate the incubator project).
        incubator_file: An explicit index path that, when given, wins outright.
        incubator: The name of the project whose incubator index to use, when the
            config has more than one incubator-collector project.

    Returns:
        The resolved, absolute path to the incubator index file.

    Why:
        `graduate-idea` reads ideas from the SAME index a configured incubator
        project already points at, so the user doesn't repeat the path. An explicit
        `--incubator-file` overrides (and lets the command work before any config
        exists). Otherwise we find the project(s) whose collectors include
        "incubator": exactly one is unambiguous; several require `--incubator <name>`;
        none is a clear setup error. Raises ConfigError with an actionable message in
        every ambiguous/empty case — the same fail-loud-at-the-edge style as the rest
        of the CLI.
    """
    # An explicit path wins and needs no config — resolve like add-project's
    # --repo-path: expand "~", and resolve a relative path against the cwd.
    if incubator_file is not None:
        path = Path(incubator_file).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        return path

    if not config_path.exists():
        raise ConfigError(
            f"No config at {config_path} to find an incubator project in. Pass "
            f"--incubator-file <path> to point at the index directly."
        )
    config = load_config(config_path)
    incubator_projects = [
        p for p in config.projects.values() if "incubator" in p.collectors
    ]
    if not incubator_projects:
        raise ConfigError(
            "No project enables the 'incubator' collector. Add one (see "
            "orion.toml.example) or pass --incubator-file <path>."
        )

    if incubator is not None:
        project = get_project(config, incubator)  # raises ConfigError if unknown
        if "incubator" not in project.collectors or project.incubator_file is None:
            raise ConfigError(
                f"Project {incubator!r} does not enable the 'incubator' collector."
            )
        return project.incubator_file

    if len(incubator_projects) > 1:
        names = ", ".join(sorted(p.name for p in incubator_projects))
        raise ConfigError(
            f"Multiple projects enable the 'incubator' collector ({names}). "
            f"Pass --incubator <name> to choose one."
        )

    # Exactly one — unambiguous. config validation guarantees its file is set.
    return incubator_projects[0].incubator_file


def cmd_graduate_idea(
    idea: str,
    config_path: Path,
    *,
    name: str | None,
    incubator: str | None,
    incubator_file: str | None,
    force: bool,
    repo_path: str | None,
    like: str | None,
    recipient_specs: list[str],
    share_level: str,
    collectors_csv: str,
    tasks_file: str | None,
    notes_file: str | None,
    print_only: bool,
    assume_yes: bool,
) -> int:
    """Graduate an incubator idea into a tracked project (idea #4 follow-on).

    Args:
        idea: The idea title to graduate (matched case-insensitively against the
            incubator index).
        config_path: Path to orion.toml.
        name: Explicit project name; when None, derived by slugifying the idea title.
        incubator: Which incubator project's index to read (when several exist).
        incubator_file: Explicit index path override (wins over config lookup).
        force: Graduate even if the idea's status is not "graduated".
        repo_path, like, recipient_specs, share_level, collectors_csv, tasks_file,
            notes_file, print_only, assume_yes: passed straight through to
            cmd_add_project (graduate-idea only resolves the NAME; registration is
            identical to add-project).

    Returns:
        Exit code: 0 on success or a declined preview; 1 on any error.

    Why:
        D4 made the incubator a signal; this closes the loop — when an idea reaches
        "graduated", it becomes a real tracked project. It is deliberately a THIN
        wrapper: it resolves the index, finds the idea, checks status, derives a name,
        then DELEGATES to cmd_add_project so the config write, preview-before-write,
        recipient resolution, repo inference, and re-load validation are reused, not
        duplicated (DRY). It is read-only on the incubator file — the only thing it
        writes is orion.toml, via add-project.
    """
    try:
        index_path = _resolve_incubator_index(config_path, incubator_file, incubator)
        statuses = read_index(index_path)  # {title -> status}; IncubatorError if unreadable
    except (ConfigError, IncubatorError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # Match the idea case-insensitively, but keep the index's canonical title for the
    # name slug and messages (so "vlm photo overlay" still graduates "VLM Photo Overlay").
    matched = next((t for t in statuses if t.lower() == idea.strip().lower()), None)
    if matched is None:
        graduated = sorted(t for t, s in statuses.items() if s == "graduated")
        if graduated:
            hint = "Graduated ideas available: " + ", ".join(repr(t) for t in graduated) + "."
        else:
            hint = "No ideas currently have status 'graduated'."
        print(
            f"Error: idea {idea!r} not found in {index_path}. {hint}",
            file=sys.stderr,
        )
        return 1

    status = statuses[matched]
    if status != "graduated" and not force:
        print(
            f"Error: idea {matched!r} has status {status!r}, not 'graduated'. "
            f"Update the incubator index, or pass --force to graduate it anyway.",
            file=sys.stderr,
        )
        return 1

    # Derive the project name from the idea title unless one was given explicitly.
    project_name = name if name else slugify_project_name(matched)
    if not project_name:
        print(
            f"Error: could not derive a project name from {matched!r}. "
            f"Pass an explicit --name.",
            file=sys.stderr,
        )
        return 1

    print(f"Graduating idea {matched!r} (status: {status}) → project {project_name!r}.")

    # Delegate the actual registration — identical to `add-project`, name pre-filled.
    return cmd_add_project(
        project_name,
        config_path,
        repo_path=repo_path,
        like=like,
        recipient_specs=recipient_specs,
        share_level=share_level,
        collectors_csv=collectors_csv,
        tasks_file=tasks_file,
        notes_file=notes_file,
        print_only=print_only,
        assume_yes=assume_yes,
    )


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


def cmd_projects(config_path: Path) -> int:
    """List every configured project with its key facts (read-only).

    Args:
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success; 1 if the config can't be loaded/validated.

    Why:
        The "what's configured / did I set auto_send?" command — the visibility
        the CLI lacked (KI-15). It only READS the config (Orion never writes it),
        so it is safe and side-effect-free, and it surfaces the few facts you most
        want at a glance: the opt-in flag, share level, signals, and who receives
        the report.
    """
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"{len(config.projects)} project(s) in {config_path}:")
    for project in config.projects.values():
        print()
        print(f"  {project.name}")
        print(f"    auto_send:   {_fmt_bool(project.auto_send)}")
        print(f"    share_level: {project.share_level}")
        print(f"    collectors:  {', '.join(project.collectors)}")
        print(f"    recipients:  {_format_recipients(project.recipients)}")
    return 0


def cmd_show(project_name: str, config_path: Path) -> int:
    """Show one project's fully-resolved config (read-only).

    Args:
        project_name: The project to show.
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success; 1 on a config error or unknown project.

    Why:
        The per-project detail view. It prints only non-secret fields — paths,
        flags, and each recipient's channel and webhook ENV-VAR NAME (never the
        URL, which lives in .env and never enters the config). So there is nothing
        sensitive to leak here; the config holds names and paths, not secrets.
    """
    try:
        config = load_config(config_path)
        project = get_project(config, project_name)
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

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


def _format_pacific(iso: str) -> str:
    """Render a stored UTC ISO-8601 timestamp as human Pacific wall-clock time.

    Args:
        iso: An ISO-8601 timestamp with an offset (as the relay stores it, e.g.
            "2026-06-19T19:30:00+00:00").

    Returns:
        A human string in America/Los_Angeles, e.g. "2026-06-19 12:30 PDT".

    Why:
        Discussion items are shown in a fixed Pacific zone (matching the dashboard's
        display choice) so timestamps read consistently regardless of the machine's local
        timezone. ZoneInfo is internally cached, so constructing it per call is cheap;
        doing it HERE rather than at module import keeps a missing tzdata from breaking
        every other command — only the `discussions` listing depends on it. This is its own
        small formatter rather than a shared one: orion/ shares no code with relay/, the
        same independence the duplicated busy-timeout constant reflects.
    """
    # fromisoformat parses the stored "+00:00" offset; astimezone converts that absolute
    # instant to California wall-clock time (zoneinfo applies DST -> PDT/PST).
    pacific = datetime.fromisoformat(iso).astimezone(ZoneInfo("America/Los_Angeles"))
    return pacific.strftime("%Y-%m-%d %H:%M %Z")


def cmd_discussions_pull(
    project_name: str, config_path: Path, *, as_json: bool, show_all: bool
) -> int:
    """Pull new supervisor messages on a project's discussion thread (E2 Inc 5).

    Args:
        project_name: The project whose thread to pull.
        config_path: Path to orion.toml.
        as_json: When True, print the raw JSON response; else a human-readable listing.
        show_all: When True, show the WHOLE thread and do NOT advance the unread marker;
            when False (default), show only items newer than the watermark and advance it.

    Returns:
        Exit code: 0 on a successful pull (including "nothing new"); 1 on a config/secrets
        error, a disabled relay, or a failed pull.

    Why:
        The developer's read half of the supervisor-interaction loop. The pull is BY
        PROJECT, Bearer-authed with the ingest token, and the unread cursor is a LOCAL
        watermark (the relay stays append-only). pull_discussions is the module-global so
        a test can monkeypatch it, mirroring relay_push.
    """
    try:
        config = load_config(config_path)
        project = get_project(config, project_name)
        load_secrets(config_path)

        relay_cfg = config.relay
        if not relay_cfg.enabled:
            print(
                f"Error: cannot pull discussions for {project.name!r} — no relay is "
                f"enabled in {config_path}. The thread lives on the relay you push reports "
                f"to; enable the [relay] table to read it.",
                file=sys.stderr,
            )
            return 1

        token = get_required(relay_cfg.token_env_var)

        conn = open_state(config.state_db)
        # since_id is the unread cursor: 0 for --all (whole thread), else the stored
        # watermark (only what's newer). Keyed by (project, relay_url).
        since_id = (
            0 if show_all else get_discussion_watermark(conn, project.name, relay_cfg.url)
        )
        response = pull_discussions(relay_cfg.url, token, project.name, since_id)
    except (ConfigError, SecretsError, DeliveryError) as exc:
        # User-fixable (a config typo, a missing token, a down relay). Never advance the
        # watermark on a failed pull.
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    items = response.get("discussions", [])
    latest_id = response.get("latest_id", since_id)

    if as_json:
        print(json.dumps(response))
    else:
        _print_discussions(items, project.name, show_all)

    # Advance the watermark ONLY on a normal run — --all is an explicit re-read. Advancing
    # to latest_id is idempotent (it echoes since_id when nothing is new).
    if not show_all:
        pulled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        set_discussion_watermark(conn, project.name, relay_cfg.url, latest_id, pulled_at)

    return 0


def cmd_discussions_reply(
    project_name: str, body: str, author: str, config_path: Path
) -> int:
    """Post a developer reply to a project's discussion thread (E2 Inc 5).

    Args:
        project_name: The project whose thread to reply on.
        body: The reply text.
        author: The display name for this reply (the `--as` value), or "" to let the relay
            stamp its default "developer" label.
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 when the reply is posted; 1 on a config/secrets error, a disabled
        relay, or a failed post.

    Why:
        The developer's write half of the loop, closing it from the terminal without the
        dashboard. The reply lands as role="developer" server-side (the Bearer token IS the
        developer's authority), so `--as` only sets the display name, never the role.
        post_discussion is the module-global so a test can monkeypatch it.
    """
    try:
        config = load_config(config_path)
        project = get_project(config, project_name)
        load_secrets(config_path)

        relay_cfg = config.relay
        if not relay_cfg.enabled:
            print(
                f"Error: cannot reply on {project.name!r} — no relay is enabled in "
                f"{config_path}. Enable the [relay] table to post to the thread.",
                file=sys.stderr,
            )
            return 1

        token = get_required(relay_cfg.token_env_var)
        result = post_discussion(relay_cfg.url, token, project.name, body, author)
    except (ConfigError, SecretsError, DeliveryError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # The relay echoes the STORED author name (authoritative): an identified producer's own
    # name, or — on the legacy anonymous path — the supplied label or the "developer"
    # fallback. Prefer it so the line reflects what was actually recorded.
    shown = result.get("author") or author or "developer"
    print(f"Reply posted to {project.name!r} as {shown!r} (id {result.get('id')}).")
    # Honesty: if a --as name was given but the relay recorded a different one, the key is an
    # identified producer's and the label was ignored (identity is server-derived, not asserted).
    if author and shown != author:
        print(
            f"  Note: --as {author!r} was ignored — this key is an identified producer, "
            f"so the reply is attributed to {shown!r}."
        )
    return 0


def _print_discussions(items: list[dict], project_name: str, show_all: bool) -> None:
    """Print a project's pulled discussion items as a human-readable listing.

    Args:
        items: The item dicts from pull_discussions (role, author_name, body, created_at).
        project_name: The project the thread belongs to (for the header/empty line).
        show_all: Whether this was an --all pull, which only changes the empty-state wording.

    Returns:
        None. Writes to stdout.

    Why:
        The default human-facing output: one line per item, leading with the [role] tag so
        the developer can tell a supervisor turn from their own at a glance. An empty result
        is a friendly one-liner, with wording that distinguishes
        "no messages at all" (--all) from "nothing new since last pull" (default).
    """
    if not items:
        qualifier = "" if show_all else " new"
        print(f"No{qualifier} discussion messages for {project_name!r}.")
        return

    print(f"{len(items)} discussion message(s) for {project_name!r}:")
    for item in items:
        # role tags the turn (supervisor/developer); author_name is server-derived.
        print(
            f"  [{item['role']}] {item['author_name']} · "
            f"{_format_pacific(item['created_at'])} · {item['body']}"
        )


def cmd_bot(config_path: Path) -> int:
    """Report that the Slack bot is PARKED (its write target retired in KI-28 Stage 2).

    Args:
        config_path: Path to orion.toml (unused while parked; kept for signature parity
            with the other cmd_* handlers and for the revival that re-reads it).

    Returns:
        Exit code 1 — the bot cannot run, so this is a clean, actionable notice rather
        than starting a listener that could never deliver.

    Why:
        The bot's only job was to relay chat replies into report comments via the relay's
        POST /api/comments, which retired in KI-28 Stage 2. Repointing it at the discussion
        write must wait for per-user keys (the next slice), because that Bearer path stamps
        role "developer" and a chat reply is supervisor speech — posting it now would be
        dishonest attribution. So rather than start a live Socket Mode listener that can
        never land a message, `orion bot` prints why it is parked and exits. The pure core
        (orion.bot.core) and the Slack shell survive as the seam revival re-wires.
    """
    print(
        "The chat bot is parked (KI-28 Stage 2): its write path (relay comments) retired, "
        "and repointing it at the discussion write awaits per-user keys in the next slice "
        "so chat replies attribute honestly as supervisor speech. See "
        "docs/two-person-shared-base-kickoff.md.",
        file=sys.stderr,
    )
    return 1


def _load_relay_serve():
    """Import the relay server's serve() from the top-level relay/ package.

    Returns:
        The relay.server.serve callable.

    Why:
        The relay (the hosted half) lives in a top-level `relay/` package at the
        repo root, deliberately OUTSIDE the installed `orion` package (src/), so the
        core stays dependency-light and the relay is separately deployable.
        `relay-serve` is a convenience launcher for that bundled reference relay,
        which only exists when Orion is run from a clone of its repo. We import it
        LAZILY here (not at module top) so importing cli.py — i.e. every other
        command — never depends on the relay being present. A console-script entry
        point does not put the repo root on sys.path, so on the first ImportError we
        add it (relay/ sits at parents[2] of this file: src/orion/cli.py -> repo
        root) and retry; a remaining failure becomes a clear, actionable message
        rather than a raw ImportError.
    """
    try:
        from relay.server import serve

        return serve
    except ImportError:
        pass

    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        from relay.server import serve

        return serve
    except ImportError as exc:
        raise ConfigError(
            "Could not import the relay package. `relay-serve` runs the bundled "
            "reference relay, which is only available when running Orion from a "
            "clone of its repository (the relay/ package is not part of the "
            "installed orion distribution)."
        ) from exc


def _parse_showcase_projects(
    raw: list[str] | None,
) -> tuple[tuple[str, str], ...]:
    """Parse repeated --showcase-project NAME[:blurb] flags into (name, blurb) pairs.

    Args:
        raw: The accumulated --showcase-project values (argparse append, or None when the
            flag was never passed).

    Returns:
        Ordered (name, blurb) pairs preserving CLI order — the Showcase display order.
        A value with no colon yields an empty blurb (the serializer then falls back to
        the observed headline). Surrounding whitespace is stripped from both parts; an
        entry whose name is empty after stripping is skipped.

    Why:
        The allowlist + its curated copy come from the CLI (the relay never reads
        orion.toml), so one place turns the flat "name:blurb" strings into the structured
        pairs ShowcaseConfig holds. Splitting on the FIRST colon only lets a blurb itself
        contain colons (e.g. "orion: a tracker: observed, not authored").
    """
    pairs: list[tuple[str, str]] = []
    for value in raw or []:
        name, sep, blurb = value.partition(":")
        name = name.strip()
        if not name:
            continue
        pairs.append((name, blurb.strip() if sep else ""))
    return tuple(pairs)


def cmd_relay_serve(
    host: str,
    port: int,
    db_path: Path,
    token_env: str,
    view_token_env: str,
    require_view_auth: bool,
    timezone_name: str,
    config_path: Path,
    session_days: int = 30,
    allow_legacy_admin: bool = False,
    disable_legacy_ingest: bool = False,
    web_dir: Path | None = None,
    showcase_enabled: bool = False,
    showcase_projects: list[str] | None = None,
) -> int:
    """Run the local reference relay: ingest endpoint + read-only dashboard.

    Args:
        host: Interface to bind (default 127.0.0.1 — loopback only).
        port: Port to bind (default 8787).
        db_path: Path to the relay's own sqlite store (its own file, separate from
            Orion's state db).
        token_env: Name of the .env variable holding the shared ingest token.
        view_token_env: Name of the .env variable holding the optional dashboard read
            secret (HTTP Basic). Required by the relay's fail-closed guard when host
            is non-loopback; absent/empty leaves loopback reads open.
        require_view_auth: Force the view secret even on a loopback bind (the
            reverse-proxy topology). The guard then refuses to start without it.
        timezone_name: IANA zone name the dashboard renders timestamps in (the
            --timezone flag; default "America/Los_Angeles"). Validated here into a
            ZoneInfo before serving.
        config_path: Path to orion.toml, used only to locate the sibling .env that
            holds the secrets.

    Returns:
        Exit code: 0 on a clean shutdown (Ctrl-C); 1 on a setup error (missing ingest
        token, an invalid --timezone, the relay package can't be imported, or the
        fail-closed guard refuses a non-loopback bind without a view secret).

    Why:
        This is the thin CLI adapter over relay/server.py — it reads the ingest token
        from .env (the same secret the pushing side sends as a Bearer token) and the
        OPTIONAL dashboard read secret, then hands off to serve(). Secrets are read
        HERE, like every other secret; a missing INGEST token is a clean SecretsError
        naming the variable. The view secret is read softly (empty -> None) because on
        loopback the dashboard may serve open; the relay's guard — not this CLI — is
        what refuses a non-loopback bind without it, so the rule is enforced for every
        caller, not just this one. The display timezone is validated HERE (the relay
        does not read orion.toml, so the flag is the only source) by constructing a
        ZoneInfo — the same check the renderer does, so "valid here" means "usable
        there" — mirroring config.py's _parse_display_timezone so a typo fails loudly
        with a named error rather than a raw traceback. serve() blocks until
        interrupted, then returns.
    """
    try:
        # Load .env beside the config (like every command), then read the secrets.
        load_secrets(config_path)
        token = get_required(token_env)
        # Optional: empty/unset -> None. The fail-closed guard inside serve() refuses a
        # non-loopback bind when it is None; on loopback, None means "reads open".
        view_token = os.environ.get(view_token_env, "").strip() or None

        # Multi-party auth secrets (fixed env names; the relay does not read orion.toml).
        # Each is INDEPENDENT of the ingest/view secrets (Codex hardening): the session
        # signing key, the per-user-key pepper, and the provisioning admin token.
        session_key_raw = os.environ.get("ORION_RELAY_SESSION_KEY", "").strip()
        user_pepper_raw = os.environ.get("ORION_RELAY_USER_PEPPER", "").strip()
        admin_token = os.environ.get("ORION_RELAY_ADMIN_TOKEN", "").strip() or None
        public_origin = os.environ.get("ORION_RELAY_PUBLIC_ORIGIN", "").strip() or None

        # Validate the display zone by constructing it (ZoneInfo is internally cached,
        # so the same object is reused by the renderer). This mirrors config.py's
        # _parse_display_timezone: ZoneInfoNotFoundError = no such zone in the tz
        # database; ValueError = a malformed key (e.g. an absolute path). Both are a
        # user typo, so we surface a clear, named error instead of a raw traceback.
        try:
            display_tz = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(
                f"--timezone {timezone_name!r} is not a valid IANA zone name ({exc}). "
                'Use a name like "America/Los_Angeles" or "UTC".'
            ) from exc

        # When asked to serve the SPA, fail fast on a missing build rather than 404-ing
        # every page at request time — a clear, actionable startup error.
        if web_dir is not None and not (web_dir / "index.html").is_file():
            raise ConfigError(
                f"--web-dir {web_dir} has no index.html — build the SPA first "
                "(cd web && npm run build), or omit --web-dir to serve the legacy HTML."
            )

        serve = _load_relay_serve()
        # AuthConfig + ShowcaseConfig + the loopback test live in the relay package,
        # importable now that _load_relay_serve has put the repo root on sys.path.
        from relay.server import AuthConfig, ShowcaseConfig, _is_loopback

        # Sessions/provisioning need their secrets whenever the dashboard is access-gated
        # (a view secret is set) or provisioning is enabled (an admin token is set). Fail
        # CLOSED with a named error rather than serving a login that could never work.
        if (view_token is not None or admin_token is not None) and (
            not session_key_raw or not user_pepper_raw
        ):
            raise ConfigError(
                "multi-party auth needs ORION_RELAY_SESSION_KEY and "
                "ORION_RELAY_USER_PEPPER in .env (each a long random secret, independent "
                "of the ingest/view tokens). Set them before serving an access-gated "
                "dashboard."
            )

        # Secure cookies whenever the relay is HTTPS-exposed: a non-loopback bind, or a
        # loopback bind behind a TLS proxy (--require-view-auth). Plain loopback http
        # dev stays non-Secure so the cookie still works there.
        auth = AuthConfig(
            session_key=session_key_raw.encode("utf-8") if session_key_raw else None,
            user_pepper=user_pepper_raw.encode("utf-8") if user_pepper_raw else None,
            admin_token=admin_token,
            secure_cookie=require_view_auth or not _is_loopback(host),
            session_seconds=session_days * 24 * 3600,
            public_origin=public_origin,
            allow_legacy_admin=allow_legacy_admin,
            disable_legacy_ingest=disable_legacy_ingest,
        )

        # The public Showcase allowlist + curated blurbs come from the CLI (project names
        # are not secrets, and the relay does not read orion.toml). Disabled by default —
        # a no-op ShowcaseConfig() — so the public surface only exists when asked for.
        showcase = ShowcaseConfig(
            enabled=showcase_enabled,
            projects=_parse_showcase_projects(showcase_projects),
        )
    except (SecretsError, ConfigError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        # Blocks until Ctrl-C; serve() prints its bound address and read-auth state.
        # The guard raises ValueError BEFORE binding if host is non-loopback without a
        # view secret — surfaced here as a clean, actionable error, not a traceback.
        serve(
            host, port, db_path, token, view_token, require_view_auth, display_tz,
            auth=auth, web_dir=web_dir, showcase=showcase,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


def _load_relay_admin(config_path: Path) -> tuple[str, str]:
    """Load the relay URL + admin token for a `relay-user` command.

    Args:
        config_path: Path to orion.toml (its sibling .env holds the admin token).

    Returns:
        A (relay_url, admin_token) pair: the relay's base URL (from [relay] url) and the
        admin Bearer token (read from .env via admin_token_env_var).

    Raises:
        ConfigError: when no relay is enabled, or [relay] has no admin_token_env_var.
        SecretsError: when the named admin-token env variable is unset.

    Why:
        The three relay-user commands share the same prerequisites — an enabled relay,
        a configured admin-token env var, and the secret itself — so resolving them lives
        in one place (DRY). The admin token is SEPARATE from the ingest token (token_env_var):
        provisioning must not ride on the push credential. Reading the secret here, in the
        CLI, matches every other command; a missing one is named by get_required, never printed.
    """
    # Use the focused relay-only loader: provisioning needs the [relay] table but NOT a
    # local project list, so an admin-only operator (runs the relay, reports elsewhere)
    # isn't blocked by full load_config's "defines no projects" requirement.
    relay_cfg = load_relay_config(config_path)
    load_secrets(config_path)
    if not relay_cfg.enabled:
        raise ConfigError(
            f"no relay is enabled in {config_path}. Enable the [relay] table "
            "(url + token_env_var + admin_token_env_var) to manage relay users."
        )
    if not relay_cfg.admin_token_env_var:
        raise ConfigError(
            f"[relay] in {config_path} has no admin_token_env_var. Add it (the .env "
            "variable holding the relay admin token, e.g. "
            'admin_token_env_var = "ORION_RELAY_ADMIN_TOKEN") to manage relay users.'
        )
    admin_token = get_required(relay_cfg.admin_token_env_var)
    return relay_cfg.url, admin_token


def _member_scope_sentence(scope: list[str]) -> str:
    """Describe what a `member` account can read, given its explicit grants.

    Args:
        scope: The member's explicitly granted project names (often empty).

    Returns:
        A sentence describing the account's effective read scope.

    Why:
        Every other scoped role is grant-only, so "no grants" means "sees nothing" and the
        CLI tells the operator to grant something. A member inverts that: it is an ORG
        INSIDER that reads every org-visible project WITHOUT a grant, and grants are
        ADDITIVE on top (visibility is a floor, not a ceiling). Reusing the grant-only
        wording for a member therefore states something false and prompts the operator to
        "fix" a configuration that is already correct — zero grants is the intended shape.
        Both the provisioning and role-change paths need to say this, identically, so the
        sentence lives here rather than being written twice (they drifted once already).
    """
    if scope:
        return f"every org-visible project, plus these grants: {', '.join(scope)}"
    return "every org-visible project (a member needs no grants)."


def cmd_relay_user_add(
    name: str,
    role: str,
    projects: list[str],
    config_path: Path,
    account_kind: str = "human",
    operated_by: str | None = None,
) -> int:
    """Provision a relay user and print their one-time access key (`relay-user add`).

    Args:
        name: The new user's unique handle.
        role: "viewer" or "admin".
        projects: Project names a viewer may see (ignored for an admin).
        config_path: Path to orion.toml.
        account_kind: "human" (default) or "agent" (Unit 4a).
        operated_by: For an agent, the operating human's account name; None for a human.

    Returns:
        Exit code: 0 on success; 1 on a config/secrets error or a failed request
        (e.g. a duplicate name → the relay's 409, surfaced as a clear message).

    Why:
        The admin-facing half of provisioning: it calls the relay's POST /api/users with
        the admin token and prints the returned key ONCE. The key is shown here (to the
        operator who created it) deliberately — that is the only time it exists in the
        clear; it is never stored or logged. We print a copy-it-now warning so the operator
        knows it cannot be retrieved later (only its verifier is stored).
    """
    # Caught here rather than server-side alone so the operator gets an immediate, local
    # error instead of a round trip — the relay validates this too (never trust the client).
    is_agent = account_kind == "agent"
    if is_agent and not operated_by:
        print(
            "Error: --kind agent requires --operated-by NAME (the human it acts for).",
            file=sys.stderr,
        )
        return 1
    if not is_agent and operated_by:
        print("Error: --operated-by is only valid with --kind agent.", file=sys.stderr)
        return 1

    try:
        relay_url, admin_token = _load_relay_admin(config_path)
        result = relay_create_user(
            relay_url,
            admin_token,
            name,
            role,
            projects,
            account_kind="agent" if is_agent else None,
            operated_by=operated_by if is_agent else None,
        )
    except (ConfigError, SecretsError, DeliveryError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Provisioned user {result['name']!r} (role: {result['role']}).")
    if result.get("kind") == "agent":
        print(f"  Kind: agent, operated by {result['operated_by']!r}.")
    scope = result.get("projects") or []
    if result["role"] == "admin":
        print("  Scope: all projects (admin).")
    elif result["role"] == "member":
        print(f"  Scope: {_member_scope_sentence(scope)}")
    elif scope:
        print(f"  Scope: {', '.join(scope)}")
    elif result["role"] == "contributor":
        print("  Scope: none yet — grant projects so this contributor can push to them.")
    else:
        print("  Scope: none yet — grant projects so this viewer can see anything.")
    print()
    print("  Access key (shown ONCE — copy it now; it cannot be retrieved later):")
    print(f"    {result['key']}")
    return 0


def cmd_relay_user_list(config_path: Path) -> int:
    """List the relay's users with role, status, and scope (`relay-user list`).

    Args:
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success (including an empty roster); 1 on a config/secrets error
        or a failed request.

    Why:
        The operational view: who has access, what they can see, and who has been revoked.
        It calls GET /api/users, which returns NO credential material (no verifier, no
        key), so a listing can never surface a secret.
    """
    try:
        relay_url, admin_token = _load_relay_admin(config_path)
        result = relay_list_users(relay_url, admin_token)
    except (ConfigError, SecretsError, DeliveryError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    users = result.get("users", [])
    if not users:
        print("No relay users provisioned yet.")
        return 0
    for user in users:
        status = "active" if user.get("active") else "REVOKED"
        if user["role"] == "admin":
            scope = "all (admin)"
        else:
            scope = ", ".join(user.get("projects") or []) or "none"
        last_login = user.get("last_login_at") or "never"
        # Unit 4a: only an agent gets an extra marker, so a human roster prints exactly as
        # it did before. .get keeps this working against a pre-4a relay.
        if user.get("account_kind") == "agent":
            operator = user.get("operated_by_name") or "?"
            marker = f"  agent, operated by {operator}"
        else:
            marker = ""
        print(
            f"{user['name']}  [{user['role']}, {status}]  "
            f"scope: {scope}  last login: {last_login}{marker}"
        )
    return 0


def cmd_relay_user_revoke(name: str, config_path: Path) -> int:
    """Revoke a relay user: deactivate + force-logout (`relay-user revoke`).

    Args:
        name: The user to revoke.
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success; 1 on a config/secrets error or a failed request
        (e.g. an unknown name → the relay's 404, surfaced as a clear message).

    Why:
        Revocation is a settled Increment-1 requirement: an admin must be able to cut off
        access immediately. It calls POST /api/users/revoke, where the relay deactivates
        the user and bumps their session_version atomically — so the key stops logging in
        AND any cookie already in a browser dies on its next request.
    """
    try:
        relay_url, admin_token = _load_relay_admin(config_path)
        relay_revoke_user(relay_url, admin_token, name)
    except (ConfigError, SecretsError, DeliveryError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(
        f"Revoked user {name!r}: their key is deactivated and any live session is "
        "logged out."
    )
    return 0


def cmd_relay_user_grant(name: str, projects: list[str], config_path: Path) -> int:
    """Grant an existing user access to more projects (`relay-user grant`).

    Args:
        name: The user whose scope to widen.
        projects: Project names to grant (at least one required).
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success; 1 on no --project given, a config/secrets error, or a failed
        request (e.g. an unknown name → the relay's 404).

    Why:
        A contributor's project set was frozen at creation, so a multi-project producer couldn't
        be covered without re-provisioning (KI-31). This widens scope in place. It prints the FULL
        scope the relay returns after the grant, so the operator sees the new coverage at a glance.
    """
    if not projects:
        print(
            "Error: grant needs at least one --project (e.g. --project my-app).",
            file=sys.stderr,
        )
        return 1
    try:
        relay_url, admin_token = _load_relay_admin(config_path)
        result = relay_grant_projects(relay_url, admin_token, name, projects)
    except (ConfigError, SecretsError, DeliveryError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    scope = result.get("projects") or []
    print(f"Granted {name!r} access to: {', '.join(projects)}.")
    print(f"  Scope is now: {', '.join(scope) if scope else '(none)'}")
    return 0


def cmd_relay_user_key_add(name: str, label: str, config_path: Path) -> int:
    """Attach a new key credential to an account (`relay-user key add`).

    Args:
        name: The account to attach the key to.
        label: A short label for where the key will live (unique among active credentials).
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success; 1 on a config/secrets error or a failed request (unknown
        name → 404; revoked account or duplicate active label → the relay's 409).

    Why:
        The replacement for the retired `rotate`, and the command that makes one identity
        across two machines real. Adding does NOT disturb the account's existing keys, so the
        safe replacement sequence is add → deploy → verify → revoke: the old key keeps
        working until the new one is confirmed, with no silent-401 window on a scheduled
        push and no way to strand yourself if this output is lost.
    """
    try:
        relay_url, admin_token = _load_relay_admin(config_path)
        result = relay_add_user_key(relay_url, admin_token, name, label)
    except (ConfigError, SecretsError, DeliveryError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Added key {label!r} (id {result['id']}) to {name!r}. Existing keys still work.")
    print("  Access key (shown ONCE — copy it now; it cannot be retrieved later):")
    print(f"    {result['key']}")
    print("  Next: install it, verify a push, THEN revoke the old credential:")
    print(f"    orion relay-user key list {name}")
    return 0


def cmd_relay_user_key_list(name: str, config_path: Path) -> int:
    """List an account's credentials (`relay-user key list`).

    Args:
        name: The account whose credentials to list.
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success; 1 on a config/secrets error or a failed request.

    Why:
        The question that has to precede a revocation is "what is attached, and is it still
        being used?" — `last_used_at` answers the second half, which is otherwise unknowable
        once an account holds several keys. Key material is never shown: the relay's listing
        excludes verifiers by construction.
    """
    try:
        relay_url, admin_token = _load_relay_admin(config_path)
        result = relay_list_user_keys(relay_url, admin_token, name)
    except (ConfigError, SecretsError, DeliveryError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    credentials = result.get("credentials", [])
    if not credentials:
        print(f"{name!r} has no credentials.")
        return 0
    print(f"Credentials for {name!r}:")
    for cred in credentials:
        state = "active" if cred["active"] else "revoked"
        used = cred["last_used_at"] or "never used"
        print(f"  [{cred['id']:>3}] {cred['type']:<8} {cred['label']:<12} {state:<8} last used: {used}")
    return 0


def cmd_relay_user_key_revoke(name: str, credential_id: int, config_path: Path) -> int:
    """Revoke one credential by id (`relay-user key revoke`).

    Args:
        name: The account the credential belongs to.
        credential_id: The credential id (from `key list`).
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success; 1 on a config/secrets error or a failed request (unknown
        account, or an id that is unknown / already revoked / not owned by this account → 404).

    Why:
        Revokes exactly one credential, leaving the account and its other keys intact — the
        whole point of the credential split (a lost laptop kills one key, not an identity).
        It deliberately does not log the person out of the dashboard: a machine key and a
        browser session are different credentials with different lifecycles.
    """
    try:
        relay_url, admin_token = _load_relay_admin(config_path)
        relay_revoke_user_key(relay_url, admin_token, name, credential_id)
    except (ConfigError, SecretsError, DeliveryError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Revoked credential {credential_id} for {name!r}. Their other credentials still work.")
    return 0


def cmd_relay_user_password_set(name: str, generate: bool, config_path: Path) -> int:
    """Set or replace an interactive account's password (`relay-user password set`).

    Args:
        name: The account to set a password for.
        generate: When True, the relay mints the password and prints it once instead of
            prompting for one.
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success; 1 on a config/secrets error or a failed request; 2 when the
        two typed passwords do not match.

    Why:
        The password is read with `getpass` (hidden, prompted twice) and is never accepted as
        a command-line argument — argv is visible in shell history, in `ps`, and in CI logs.
        `--generate` exists for provisioning someone else's account, where the admin should
        not be inventing (or seeing twice) a password the person will own.

        Note the consequence printed below: once an account has a password, its access KEYS
        stop working for login. That is the intended end state — humans know a password,
        machines hold a key — but it takes effect immediately, so the person must be told.
    """
    password = None
    if not generate:
        import getpass

        try:
            password = getpass.getpass(f"New password for {name!r}: ")
            confirm = getpass.getpass("Repeat password: ")
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.", file=sys.stderr)
            return 2
        if not password:
            print("Error: password must not be empty.", file=sys.stderr)
            return 2
        if password != confirm:
            print("Error: the two passwords did not match.", file=sys.stderr)
            return 2

    try:
        relay_url, admin_token = _load_relay_admin(config_path)
        result = relay_set_user_password(relay_url, admin_token, name, password)
    except (ConfigError, SecretsError, DeliveryError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Password set for {name!r}. Any live session was logged out.")
    if "password" in result:
        print("  Password (shown ONCE — copy it now; it cannot be retrieved later):")
        print(f"    {result['password']}")
    print("  They now log in with their NAME and this password.")
    print("  Their access key no longer works for login (it stays valid for machine pushes).")
    return 0


def cmd_relay_user_password_unlock(name: str, config_path: Path) -> int:
    """Clear an account's login lockout (`relay-user password unlock`).

    Args:
        name: The account to unlock.
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success; 1 on a config/secrets error or a failed request.

    Why:
        Repeated failed logins lock an account for a short window, which means anyone who
        knows the account name can lock its owner out. Keeping the lockout strict is right —
        it is what makes online password guessing hopeless — so the counterweight is an
        instant admin unlock that does NOT require changing a password the person still knows.
    """
    try:
        relay_url, admin_token = _load_relay_admin(config_path)
        relay_unlock_user(relay_url, admin_token, name)
    except (ConfigError, SecretsError, DeliveryError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Cleared the login lockout for {name!r}. Their password is unchanged.")
    return 0


def cmd_relay_user_role(name: str, role: str, config_path: Path) -> int:
    """Change an account's role (`relay-user role`).

    Args:
        name: The account whose role to change.
        role: The new role (the relay validates it against the provisionable set).
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success; 1 on a config/secrets error or a failed request.

    Why:
        Roles were fixed at provisioning, so changing one previously meant delete + re-add —
        minting a new key the person must be re-issued and dropping their grants. The warning
        below is the operationally important part: demoting an admin to a scoped role makes
        default-deny apply, so the account sees NOTHING until it is granted projects.
    """
    try:
        relay_url, admin_token = _load_relay_admin(config_path)
        result = relay_set_user_role(relay_url, admin_token, name, role)
    except (ConfigError, SecretsError, DeliveryError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"{name!r} is now a {role}. Any live session was logged out.")
    scope = result.get("projects") or []
    if role == "member":
        # NOT the default-deny warning below: a member reads every org-visible project
        # with no grant at all, so "no grants" is its intended shape. Warning here would
        # state something false — that the account sees nothing — and push the operator
        # to "fix" a correct configuration.
        print(f"  Scope: {_member_scope_sentence(scope)}")
    elif role != "admin" and not scope:
        print("  WARNING: this account has NO project grants, so it currently sees nothing.")
        print(f"    Grant projects with: orion relay-user grant {name} <project> [<project> ...]")
    elif role != "admin":
        print(f"  Scope: {', '.join(scope)}")
    return 0


def cmd_relay_project_visibility(name: str, visibility: str, config_path: Path) -> int:
    """Set a project's visibility (`relay-project visibility`).

    Args:
        name: The project to set.
        visibility: "org" (every member may read it) or "restricted" (grant-only).
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success; 1 on a config/secrets error or a failed request (an unknown
        project → the relay's 404).

    Why:
        The KB's scoping act: 'org' is what lets a member-role account read a project without
        a per-project grant. Every project is born 'restricted', so opening one up is always
        deliberate — which is the property that keeps default-deny meaningful as the org's
        project list grows.
    """
    try:
        relay_url, admin_token = _load_relay_admin(config_path)
        relay_set_project_visibility(relay_url, admin_token, name, visibility)
    except (ConfigError, SecretsError, DeliveryError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if visibility == "org":
        print(f"{name!r} is now org-visible: any member-role account can read it.")
    else:
        print(f"{name!r} is now restricted: only accounts granted it can read it.")
    print("  Viewers and supervisors are unaffected — they always see only their grants.")
    return 0


def cmd_relay_project_lifecycle(name: str, lifecycle: str, config_path: Path) -> int:
    """Mark a project finished or still running (`relay-project lifecycle`).

    Args:
        name: The project to set.
        lifecycle: "past" (finished) or "active" (still running).
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success; 1 on a config/secrets error or a failed request (an unknown
        project → the relay's 404).

    Why:
        The KB's curation act (S2.2): a project is finished because someone SAYS so, never
        because it went quiet — quiet is not finished, and inferring it would let a paused
        project read as shipped. The flag lives on the relay (not in orion.toml) so the
        dashboard keeps remembering after the project stops being produced, which is why
        this is an admin command rather than something a producer pushes.

        Nothing about the project's record changes — reports, checklist, About and
        discussions all stay exactly as they are. What changes is how the dashboard frames
        it, and that a finished project stops being read as if it still had deadlines.
    """
    try:
        relay_url, admin_token = _load_relay_admin(config_path)
        relay_set_project_lifecycle(relay_url, admin_token, name, lifecycle)
    except (ConfigError, SecretsError, DeliveryError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if lifecycle == "past":
        print(f"{name!r} is now marked past: finished, and framed as history.")
        print("  It groups into the dashboard's 'Past projects' section.")
        print(
            "  It is excluded from every deadline view — due-soon, at-risk, slipping and "
            "Scheduling — so it can never read as overdue."
        )
        print("  Its full record (reports, checklist, About, discussion) is unchanged.")
    else:
        print(f"{name!r} is active again: back in the live sections, deadlines and all.")
    return 0


def cmd_relay_user_set_operator(name: str, operator: str, config_path: Path) -> int:
    """Repoint an agent account at a different operating human (`relay-user set-operator`).

    Args:
        name: The agent account to reassign.
        operator: The human account the agent should act on behalf of.
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success; 1 on a config/secrets error or a failed request (unknown
        agent → 404; not an agent, or an invalid operator → the relay's 400).

    Why:
        The explicit escape hatch for the blocked operator delete: an account that still
        operates active agents cannot be deleted until they are reparented, and the
        alternative (delete + re-provision the agent) would mint a new key every machine
        holding it would need re-issued. Reassignment moves DISPLAY GROUPING only — stored
        reports keep the agent's real author id, so no history moves and provenance holds.
    """
    try:
        relay_url, admin_token = _load_relay_admin(config_path)
        relay_set_user_operator(relay_url, admin_token, name, operator)
    except (ConfigError, SecretsError, DeliveryError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"{name!r} is now operated by {operator!r}.")
    print("  Past reports keep their own attribution; only display grouping moves.")
    return 0


def cmd_relay_user_rename(name: str, new_name: str, config_path: Path) -> int:
    """Rename an account (`relay-user rename`).

    Args:
        name: The account to rename.
        new_name: The new unique name.
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success; 1 on a config/secrets error or a failed request (unknown
        name → 404; taken name → the relay's 409).

    Why:
        The account is the durable identity under the credential split, so its label should be
        editable without re-provisioning. Already-recorded reports and discussion items keep
        the name they were written with — `author_name` is denormalized precisely so history
        survives the account changing or being deleted — so this is not a retroactive edit.
    """
    try:
        relay_url, admin_token = _load_relay_admin(config_path)
        relay_rename_user(relay_url, admin_token, name, new_name)
    except (ConfigError, SecretsError, DeliveryError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Renamed {name!r} to {new_name!r}. Past reports keep the name they were sent under.")
    return 0


def cmd_relay_user_delete(name: str, config_path: Path) -> int:
    """Hard-delete a user, freeing their name to be reused (`relay-user delete`).

    Args:
        name: The user to delete.
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 on success; 1 on a config/secrets error or a failed request (unknown name
        → 404).

    Why:
        `revoke` keeps the row, so the UNIQUE name stays occupied and can't be re-provisioned
        (KI-31). `delete` removes the user + grants + live per-producer checklists and frees the
        name, while their past reports/replies keep the author name already recorded on them.
    """
    try:
        relay_url, admin_token = _load_relay_admin(config_path)
        relay_delete_user(relay_url, admin_token, name)
    except (ConfigError, SecretsError, DeliveryError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(
        f"Deleted user {name!r}: the name is free to reuse. Their past reports and "
        "discussion replies keep the author name already recorded on them."
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


def _channels(project: ProjectConfig) -> list[str]:
    """Return the project's distinct recipient channels, in first-appearance order.

    Args:
        project: The project whose recipients to scan.

    Returns:
        A list of unique channel names (e.g. ["discord", "slack"]) ordered by when
        each first appears in the recipients list.

    Why:
        We compose one message per distinct channel (not per recipient), so two
        Slack recipients don't trigger two identical composes. First-appearance
        order gives a stable, predictable preview order without sorting away the
        user's intent. A dict's insertion-ordered keys make the dedup trivial.
    """
    return list({recipient.channel: None for recipient in project.recipients})


def _deliver(
    messages: dict,
    recipients: tuple[Recipient, ...],
    key_func: Callable[[Recipient], object],
) -> tuple[list[str], list[tuple[str, str]]]:
    """Send each recipient the message composed for its audience key.

    Args:
        messages: Map of audience key -> ComposedMessage. The key shape is decided
            by `key_func`: the report path keys by (channel, frozenset(signals)) so
            each filtered audience gets its own message; the intake path keys by
            channel alone (unfiltered).
        recipients: The recipients to deliver to.
        key_func: Maps a recipient to its key into `messages`. A recipient whose key
            is absent from `messages` is skipped, NOT failed — that means its
            audience had no matching activity this run (D5), so there is nothing to
            send it, which is a clean no-op rather than a delivery error.

    Returns:
        A (sent_to, failed) tuple: the names that received the message, and a list
        of (name, error) for the ones that failed. Per-recipient failures are also
        printed to stderr here.

    Why:
        Both cmd_report and cmd_intake deliver the same way — for each recipient,
        pick its audience's rendering and its sender, try the send, and let one
        failure not abort the others. Keying by a caller-supplied function (rather
        than hardcoding "channel") lets the SAME loop serve report's per-audience
        routing and intake's per-channel routing without duplicating the send/fail
        bookkeeping (DRY). Each caller still decides what "nobody received it" means
        (report does not advance markers; intake just reports it). It does NOT decide
        success/exit codes — that stays with the caller, which knows its own books.
    """
    sent_to: list[str] = []
    failed: list[tuple[str, str]] = []
    for recipient in recipients:
        key = key_func(recipient)
        # No message for this recipient's audience -> its subscribed signals had no
        # activity this run. Skip silently: nothing to send is not a failure.
        if key not in messages:
            continue
        try:
            url = get_required(recipient.webhook_env_var)
            # Route to the right channel's sender. config validation guarantees
            # recipient.channel is supported, so the sender lookup hits.
            send = _sender_for(recipient.channel)
            send(messages[key].payload, url)
            sent_to.append(recipient.name)
        except (SecretsError, DeliveryError) as exc:
            # A per-recipient failure shouldn't abort the others.
            failed.append((recipient.name, str(exc)))

    for name, err in failed:
        print(f"  ✗ {name}: {err}", file=sys.stderr)
    return sent_to, failed


def _audience_groups(
    project: ProjectConfig,
) -> dict[tuple[str, frozenset[str]], list[Recipient]]:
    """Group a project's recipients into delivery audiences (D5).

    Args:
        project: The project whose recipients to group.

    Returns:
        An ordered map of (channel, frozenset(signals)) -> the recipients sharing
        that audience, in recipient first-appearance order.

    Why:
        Two recipients on the same channel who subscribe to the same signal set
        receive byte-identical output, so we compose ONCE per distinct audience
        rather than once per recipient. The key pairs the channel (which decides the
        rendering dialect) with the signal set (which decides the filtered content) —
        the two things that make two recipients' messages identical or not.
        first-appearance order (a dict's insertion order) gives a stable, predictable
        preview/delivery order without sorting away the user's intent.
    """
    groups: dict[tuple[str, frozenset[str]], list[Recipient]] = {}
    for recipient in project.recipients:
        key = (recipient.channel, frozenset(recipient.signals))
        groups.setdefault(key, []).append(recipient)
    return groups


def _describe_group(channel: str, recipients: list[Recipient]) -> str:
    """A short human label for one audience's preview block (D5).

    Args:
        channel: The audience's channel (e.g. "discord").
        recipients: The recipients in that audience.

    Returns:
        A label like "discord → Alex, Sam" naming the channel and who receives this
        exact (filtered) block.

    Why:
        With per-audience filtering, different recipients see different content, so
        the preview must say WHO each block is for — otherwise the human gate can't
        tell which supervisor is about to receive which slice. We label by recipient
        names (not the raw signal set) because the filtered content is already shown
        in the block; what the human needs is the destination.
    """
    names = ", ".join(r.name for r in recipients)
    return f"{channel} → {names}"


def _relay_push(blob: ReportBlob, relay_cfg: RelayConfig) -> None:
    """Push the serialized portable blob to the configured relay (C1), fail-soft.

    Args:
        blob: The report blob that was just delivered (and whose state, if any, has
            already advanced). It is serialized here and POSTed verbatim.
        relay_cfg: The global relay config. When disabled, this is a no-op.

    Returns:
        None. A relay failure is reported to stderr but never raised.

    Why:
        This is C1's one outbound addition: local Orion ALSO sends the structured
        blob to a relay that stores it and serves a dashboard — in addition to the
        unchanged channel delivery. It is deliberately self-contained and fail-soft:
        it swallows its own SecretsError (token missing) and DeliveryError (relay
        down / 4xx) and only prints a warning, so by construction it can never turn
        a delivered report into a failure or block state advancement (D1). The token
        is read here, in the CLI (like every other secret), only when the relay is
        enabled — a disabled relay never touches .env. It calls the module-global
        relay_push so a test can monkeypatch cli.relay_push, mirroring discord_send.
    """
    # Disabled (or absent) relay -> pure no-op. Every pre-C1 config lands here.
    if not relay_cfg.enabled:
        return

    try:
        # The token lives in .env, named by token_env_var (never in the config).
        token = get_required(relay_cfg.token_env_var)
        relay_push(serialize_blob(blob), relay_cfg.url, token)
        print(f"Also pushed to relay: {relay_cfg.url}")
    except (SecretsError, DeliveryError) as exc:
        # Fail-soft: the report is already delivered and state advanced. A relay
        # problem is surfaced but must not change the run's outcome.
        print(
            f"  ⚠ relay push failed (report still delivered): {exc}",
            file=sys.stderr,
        )


def _sender_for(channel: str):
    """Return the delivery function for a channel.

    Args:
        channel: The channel name ("discord" or "slack").

    Returns:
        The channel's send(message, webhook_url) callable.

    Why:
        A function (not a module-level dict) so the name resolves at CALL time
        against the current module globals — which is what lets tests monkeypatch
        `cli.discord_send` / `cli.slack_send` and have delivery use the fake. A
        dict built at import time would capture the original functions and ignore
        the patch. This mirrors _collect_for's call-time dispatch, and is still an
        explicit table — deliberately NOT a plugin/registry. config validation
        guarantees the channel is supported, so the raise is a defensive guard.
    """
    if channel == "discord":
        return discord_send
    if channel == "slack":
        return slack_send
    raise ConfigError(f"Unknown channel {channel!r}.")


def _build_summarizer(cfg: SummarizerConfig, secret_getter) -> Summarizer:
    """Construct the configured summarizer backend (B4).

    Args:
        cfg: The global SummarizerConfig (provider + model + local-only fields).
        secret_getter: A callable mapping an env-var NAME to its value (the
            module's get_required). Injected so the secret READING stays in the
            CLI and only a provider that needs a key ever calls it — a local
            backend with no api_key_env never touches it.

    Returns:
        A Summarizer for the configured provider.

    Why:
        Mirrors the call-time dispatch of _sender_for / _collect_for — an explicit
        if/elif table, deliberately NOT a plugin registry — so the orchestrator
        stays a plain lookup and adding a backend is a localized change. The
        anthropic SDK is imported inside its branch so building the client (which
        holds the API key) lives in the CLI, not in summarize.py. config
        validation guarantees the provider is supported, so the final raise is a
        defensive guard, not an expected path.
    """
    if cfg.provider == "anthropic":
        # The key always lives in ANTHROPIC_API_KEY for the Anthropic backend
        # (unchanged from Phase 1); api_key_env does not apply here.
        import anthropic

        client = anthropic.Anthropic(api_key=secret_getter("ANTHROPIC_API_KEY"))
        return AnthropicSummarizer(client, cfg.model)
    if cfg.provider == "local":
        # A local endpoint usually needs no key; fetch one only if the config
        # named an api_key_env. config validation guarantees base_url and model.
        api_key = secret_getter(cfg.api_key_env) if cfg.api_key_env else None
        return LocalSummarizer(cfg.base_url, cfg.model, api_key=api_key)
    raise ConfigError(f"Unknown summarizer provider {cfg.provider!r}.")


def _build_extractor(cfg: SummarizerConfig, secret_getter) -> DisciplineExtractor:
    """Construct the configured disciplines extractor backend (E2 Inc 4 slice 4b).

    Args:
        cfg: The global SummarizerConfig — the extractor reuses the SAME provider and
            model as the summarizer (one configured backend), so there is no separate
            config surface to maintain.
        secret_getter: A callable mapping an env-var NAME to its value (the module's
            get_required). Injected so secret READING stays in the CLI, mirroring
            _build_summarizer.

    Returns:
        A DisciplineExtractor for the configured provider.

    Why:
        Mirrors _build_summarizer's call-time dispatch. Disciplines extraction needs
        reliable structured (JSON) output, which local models do not consistently
        produce, so the local backend is deferred: we build the Anthropic extractor
        now and raise a clear, actionable error for `provider == "local"` rather than
        silently shipping a flaky path. The seam (the Protocol) is in place, so adding
        a local extractor later is additive.
    """
    if cfg.provider == "anthropic":
        import anthropic

        client = anthropic.Anthropic(api_key=secret_getter("ANTHROPIC_API_KEY"))
        return AnthropicDisciplineExtractor(client, cfg.model)
    if cfg.provider == "local":
        raise ConfigError(
            "The 'disciplines' collector needs the Anthropic summarizer provider — "
            "structured extraction is not supported on the local provider yet. Set "
            "[summarizer] provider = \"anthropic\", or drop 'disciplines' from this "
            "project's collectors."
        )
    raise ConfigError(f"Unknown summarizer provider {cfg.provider!r}.")


def _summarizer_key_env(cfg: SummarizerConfig) -> str | None:
    """Return the .env variable name the configured summarizer needs, or None.

    Args:
        cfg: The global summarizer config.

    Returns:
        The env-var NAME the backend requires, or None when it needs no key
        (a local endpoint with no api_key_env).

    Why:
        `check` reports send-readiness WITHOUT building anything, so it needs to
        know which secret (if any) the summarizer requires. Anthropic always uses
        ANTHROPIC_API_KEY; a local endpoint needs a key only when api_key_env was
        set (many need none). Keeping this beside _build_summarizer means the two
        agree on the per-provider key convention (DRY).
    """
    if cfg.provider == "anthropic":
        return "ANTHROPIC_API_KEY"
    if cfg.provider == "local":
        return cfg.api_key_env  # None when the endpoint needs no key
    return None


def _report_collectors_of(project: ProjectConfig) -> list[str]:
    """List a project's collectors that are real report inputs, in config order.

    Args:
        project: The project config whose `collectors` list is being filtered.

    Returns:
        The project's collector names with every PUSH_ONLY_COLLECTORS capability
        flag removed — i.e. exactly the names `_collect_for` can dispatch.

    Why:
        A push-only name (today "disciplines") rides in the same `collectors` list
        so enabling a signal stays one edit, but it is a capability flag, not a
        report input: it has no `_collect_for` branch by design. Every caller that
        walks `project.collectors` to collect must therefore skip it first, and
        KI-39 was exactly that skip being missed. Doing the filter in ONE place
        keyed off the constant means a new push-only capability is handled
        everywhere by adding one name in config.py — a caller cannot forget a skip
        it does not have to write.
    """
    return [c for c in project.collectors if c not in PUSH_ONLY_COLLECTORS]


def _collect_for(project: ProjectConfig, collector: str, prior: str | None):
    """Run one named collector, adapting it to its (differing) call signature.

    Args:
        project: The project config (source of repo_path, share_level, file paths).
        collector: The collector name — must be one of REPORT_COLLECTORS.
        prior: That collector's last-reported marker, or None on a first run.

    Returns:
        The collector's CollectorResult.

    Raises:
        ConfigError: If `collector` is a PUSH_ONLY_COLLECTORS capability flag (the
            caller should have skipped it) or is not a known collector at all.

    Why:
        The collectors legitimately take different arguments (git wants a repo and
        a share level; the file collectors want a path), so the orchestrator's
        uniform loop needs one place that maps a name to the right call. This is a
        plain dispatch — deliberately NOT a plugin/registry — so the set of signals
        stays small, explicit, and easy to read.

        On the raises: config validation guarantees only that a name is SUPPORTED,
        which is a weaker promise than "collectible here" — SUPPORTED_COLLECTORS
        also contains push-only capability flags. Assuming those two were the same
        thing is precisely how `disciplines` reached this dispatch and broke `report`
        for every project that enabled it. So the two failure modes get two distinct
        messages: a push-only name means the CALLER forgot to skip it (an internal
        bug with a known fix), while an unrecognized name means config validation and
        this dispatch have drifted apart. Both stay defensive guards, not expected
        paths.
    """
    if collector == "git":
        return collect_git(project.repo_path, prior, project.share_level)
    if collector == "tasks":
        return collect_tasks(project.tasks_file, prior)
    if collector == "notes":
        return collect_notes(project.notes_file, prior)
    if collector == "incubator":
        return collect_incubator(project.incubator_file, prior)
    if collector == "tracker":
        return collect_tracker(project.tracker_file, prior)
    if collector in PUSH_ONLY_COLLECTORS:
        raise ConfigError(
            f"Collector {collector!r} is a push-only capability, not a report input — "
            f"it should have been skipped before dispatch. This is an Orion bug, not a "
            f"config error."
        )
    raise ConfigError(f"Unknown collector {collector!r}.")


def _preview_and_confirm(
    previews: list[tuple[str, ComposedMessage]], redaction_hits: int
) -> bool:
    """Show the composed message(s) and ask the user to confirm sending.

    Args:
        previews: Ordered (label, message) pairs — one preview block per distinct
            audience. A non-empty label (e.g. "discord → Alex") is shown in the block
            header so the human can tell which recipients receive which (possibly
            filtered) view; an empty label renders one unlabeled block, identical to
            a single-audience run before D5.
        redaction_hits: How many potential secrets were redacted in this run.

    Returns:
        True only if the user explicitly confirms (y/yes); False otherwise.

    Why:
        Preview-before-send is the human gate that makes the whole privacy story
        trustworthy — the user sees the EXACT bytes each audience will receive
        before they leave the machine. D5 means different recipients can receive
        different (filtered) content, so each audience gets its own labeled block;
        the caller supplies the labels so this stays decoupled from how audiences are
        keyed (channel, or channel+signals). One confirm covers them all. We default
        to NO (a bare Enter, EOF, or anything but yes does not send) and surface the
        redaction count so the user scrutinizes harder when the redactor fired.
    """
    bar = "=" * 60
    multi = len(previews) > 1
    for label, message in previews:
        # Label the block only when given one (the caller passes "" for a single
        # audience), so a single-audience preview is unchanged from before D5.
        suffix = f" ({label})" if label else ""
        print(bar)
        print(f"PREVIEW{suffix} — this report has NOT been sent yet")
        print(bar)
        # .preview is the faithful text rendering of the exact payload that will
        # be POSTed — the human approves what actually leaves the machine.
        print(message.preview)
    print(bar)
    if redaction_hits > 0:
        print(f"⚠  {redaction_hits} potential secret(s) were redacted from this report.")

    prompt = "Send this report to all recipients? [y/N] " if multi else "Send this report? [y/N] "
    try:
        answer = input(prompt)
    except EOFError:
        # No interactive input available -> treat as "no" (fail closed).
        return False
    return answer.strip().lower() in ("y", "yes")


# Make `python -m orion.cli ...` behave like the `orion` console script and `python -m orion`.
# Without this guard that invocation merely IMPORTS the module and exits 0 WITHOUT running
# main() — a silent no-op. That footgun masked a real failure once: `python -m orion.cli
# <cmd>` returned exit 0 while pushing nothing, so the live dashboard stayed empty.
# Forwarding main()'s exit code here means every reasonable invocation actually runs.
if __name__ == "__main__":
    sys.exit(main())

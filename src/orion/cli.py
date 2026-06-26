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
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from orion.collectors import LANE_RAW, LANE_STRUCTURED
from orion.collectors._markdown import Table, parse_tables
from orion.collectors.git import GitError
from orion.collectors.git import collect as collect_git
from orion.collectors.incubator import IncubatorError, read_index
from orion.collectors.incubator import collect as collect_incubator
from orion.collectors.notes import NotesError
from orion.collectors.notes import collect as collect_notes
from orion.collectors.tasks import ChecklistItem, TasksError
from orion.collectors.tasks import collect as collect_tasks
from orion.collectors.tasks import snapshot as snapshot_tasks
from orion.collectors.tracker import TrackerError
from orion.collectors.tracker import collect as collect_tracker
from orion.collectors.tracker import snapshot as snapshot_tracker
from orion.compose import ComposedMessage, compose
from orion.config import (
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
    list_users as relay_list_users,
    pull_comments,
    push as relay_push,
    push_checklist,
    revoke_user as relay_revoke_user,
)
from orion.delivery.slack import send as slack_send
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
    get_comment_watermark,
    get_last_report_time,
    get_marker,
    open_state,
    record_report,
    set_comment_watermark,
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

    checklist_parser = subparsers.add_parser(
        "checklist-push",
        help="Push a project's current checklist to the relay (no report); --watch for live updates.",
    )
    checklist_parser.add_argument(
        "project", help="Project name as defined in orion.toml (must enable `checklist`)."
    )
    checklist_parser.add_argument(
        "--config",
        default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )
    checklist_parser.add_argument(
        "--watch",
        action="store_true",
        help=(
            "Run a foreground loop that polls the tasks_file and pushes the checklist "
            "whenever it changes, until Ctrl-C (near-real-time edit tracking)."
        ),
    )
    checklist_parser.add_argument(
        "--interval",
        type=float,
        default=3.0,
        help="Seconds between polls in --watch mode (default: 3.0).",
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

    comments_parser = subparsers.add_parser(
        "comments",
        help="Pull supervisor comments on your reports back from the relay (C2).",
    )
    comments_parser.add_argument("project", help="Project name as defined in orion.toml.")
    comments_parser.add_argument(
        "--config",
        default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )
    comments_parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help=(
            "Emit the raw JSON response (for the Claude session skill) instead of the "
            "human-readable listing."
        ),
    )
    comments_parser.add_argument(
        "--all",
        dest="show_all",
        action="store_true",
        help=(
            "Show ALL comments without advancing the unread marker (the default shows "
            "only comments new since your last check, and advances the marker)."
        ),
    )

    bot_parser = subparsers.add_parser(
        "bot",
        help="Run the always-on Slack bot: relay channel replies into report comments (C2-bots).",
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
        choices=("viewer", "admin"),
        default="viewer",
        help="The user's role (default: viewer). An admin sees all projects.",
    )
    ru_add.add_argument(
        "--project",
        action="append",
        default=[],
        dest="projects",
        metavar="PROJECT",
        help=(
            "A project this viewer may see (repeatable: --project a --project b). "
            "Ignored for an admin (which sees all). A viewer with none sees nothing."
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

    args = parser.parse_args(argv)
    if args.command == "report":
        return cmd_report(args.project, Path(args.config), args.yes, args.all_projects)
    if args.command == "intake":
        return cmd_intake(args.project, Path(args.config), args.message, args.yes)
    if args.command == "checklist-push":
        return cmd_checklist_push(
            args.project, Path(args.config), args.watch, args.interval
        )
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
    if args.command == "comments":
        return cmd_comments(
            args.project, Path(args.config), as_json=args.as_json, show_all=args.show_all
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
        )
    if args.command == "relay-user":
        if args.relay_user_command == "add":
            return cmd_relay_user_add(
                args.name, args.role, args.projects, Path(args.config)
            )
        if args.relay_user_command == "list":
            return cmd_relay_user_list(Path(args.config))
        if args.relay_user_command == "revoke":
            return cmd_relay_user_revoke(args.name, Path(args.config))
    return 1  # Unreachable: subparsers are required.


def cmd_report(
    project_name: str | None,
    config_path: Path,
    assume_yes: bool,
    all_projects: bool,
) -> int:
    """Set up shared state, then run the report pipeline for one or all projects.

    Args:
        project_name: The project to report on, or None when --all is used.
        config_path: Path to orion.toml.
        assume_yes: True for a non-interactive run (the `--yes` flag). Passed
            through to _run_report, which combines it with each project's
            auto_send to decide whether the human preview is bypassed.
        all_projects: True for `--all` (report on every configured project).

    Returns:
        Exit code: 2 for a usage error (neither or both of project / --all); 1 if
        ANY project genuinely FAILED; otherwise 0. No-activity, skipped, and
        human-aborted projects are all clean exit 0 — so a scheduler alerts only
        on a real failure, not on the routine "nothing to send" cases.

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

    # Fail-soft loop: _run_report catches its own per-project errors and returns
    # STATUS_FAILED, so one bad project never stops the rest of an --all run.
    statuses = [
        _run_report(
            project, conn, assume_yes, config.summarizer, config.relay,
            config.display_timezone,
        )
        for project in projects
    ]

    if all_projects:
        _print_all_summary(statuses)

    # Only a real FAILED is a non-zero exit; NO_ACTIVITY / SKIPPED / ABORTED are
    # all routine, intended outcomes that should not look like an error to cron.
    return 1 if any(status == STATUS_FAILED for status in statuses) else 0


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
    }
    for status in statuses:
        counts[status] += 1

    print(
        f"\n{len(statuses)} project(s): "
        f"{counts[STATUS_SENT]} sent, "
        f"{counts[STATUS_NO_ACTIVITY]} no activity, "
        f"{counts[STATUS_SKIPPED_NOT_OPTED]} skipped, "
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

        for collector_name in project.collectors:
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

        full_blob = build_report(
            project,
            safe_body,
            lane,
            generated_at,
            sections=tuple(full_pairs),
            checklist=checklist,
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
            # it rides through untouched — but the rebuild must preserve it (and key) or
            # they would be lost before reaching the wire. The `key` (a title) IS user text,
            # so it is redacted here too as a safety net; its hits are NOT re-counted,
            # because the title is a substring of `text` whose redaction already counted
            # any secret. None (tasks/table items) stays None.
            safe_key = redact(item.key).text if item.key is not None else None
            items.append(
                ChecklistItem(
                    text=safe_text,
                    done=item.done,
                    due_date=item.due_date,
                    key=safe_key,
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


def _watch_tick(
    project: ProjectConfig, relay_cfg: RelayConfig, token: str, last_pushed: list | None
) -> tuple[list[dict], bool]:
    """One watch iteration: snapshot the checklist and push it only if it changed.

    Args:
        project: The project being watched.
        relay_cfg: The relay config (url to push to).
        token: The relay ingest Bearer token.
        last_pushed: The payload pushed on the previous successful tick, or None on the
            first tick (so the first snapshot always pushes).

    Returns:
        A (payload, pushed) pair: the current checklist payload, and whether THIS tick
        pushed it (True when it differed from last_pushed). On no change, returns
        (last_pushed, False) and performs no network call.

    Why:
        Factoring one iteration out of the loop makes the "push on change, skip when
        unchanged" rule unit-testable without an infinite loop. Content-compare (rather
        than file mtime) is robust to how editors save and self-dedupes a touch that
        did not actually change the checklist. May raise DeliveryError (the loop treats
        that as transient and retries next tick).
    """
    payload = _checklist_payload(project)
    if payload == last_pushed:
        return last_pushed, False
    push_checklist(relay_cfg.url, project.name, payload, token)
    return payload, True


def _watch_checklist(
    project: ProjectConfig, relay_cfg: RelayConfig, token: str, interval: float
) -> int:
    """Poll the project's checklist source and push the checklist whenever it changes.

    Args:
        project: The project to watch (its tasks_file and/or tracker_file is polled).
        relay_cfg: The relay config (push target).
        token: The relay ingest Bearer token.
        interval: Seconds between polls.

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
                last_pushed, pushed = _watch_tick(project, relay_cfg, token, last_pushed)
                if pushed:
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


def cmd_checklist_push(
    project_name: str, config_path: Path, watch: bool, interval: float
) -> int:
    """Push a project's current checklist to the relay (one-shot, or a --watch loop).

    Args:
        project_name: The project whose checklist to push.
        config_path: Path to orion.toml.
        watch: When True, run a foreground poll loop that pushes on every change to the
            project's checklist source until interrupted; when False, push once and exit.
        interval: Seconds between polls in --watch mode.

    Returns:
        Process exit code: 0 on success / clean stop, 1 on a setup or delivery error.

    Why:
        The dedicated checklist-only push (E2 Inc 2 follow-up): it updates ONLY the
        project's live checklist on the dashboard — no report — so a checklist edit can
        reach the dashboard in near-real-time. It reuses the report path's redaction
        (_redacted_checklist) and the relay's ingest token, and requires the project to
        have `checklist` enabled (with a tasks_file or tracker_file) and an enabled
        [relay].
    """
    try:
        config = load_config(config_path)
        load_secrets(config_path)
        project = get_project(config, project_name)
        relay_cfg = config.relay
        if not relay_cfg.enabled:
            raise ConfigError(
                f"checklist-push needs an enabled [relay] in {config_path} — it pushes "
                f"the checklist to the dashboard relay."
            )
        if not project.checklist:
            raise ConfigError(
                f"Project {project.name!r} does not enable `checklist`. Set "
                f"`checklist = true` (with a 'tasks' or 'tracker' collector) to push "
                f"its checklist."
            )
        if not _checklist_source_files(project):
            raise ConfigError(
                f"Project {project.name!r} has no tasks_file or tracker_file to read a "
                f"checklist from."
            )
        token = get_required(relay_cfg.token_env_var)
    except (ConfigError, SecretsError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if watch:
        return _watch_checklist(project, relay_cfg, token, interval)

    # One-shot: push the current checklist once. A delivery failure is fatal here
    # (unlike the watch loop, which retries), so the user sees a non-zero exit.
    try:
        payload = _checklist_payload(project)
        push_checklist(relay_cfg.url, project.name, payload, token)
    except DeliveryError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Pushed checklist for {project.name!r} ({len(payload)} item(s)).")
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
    print(f"  Then: python -m orion check {project_name}")
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
        for collector_name in project.collectors:
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
    for collector_name in project.collectors:
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


def cmd_comments(
    project_name: str, config_path: Path, *, as_json: bool, show_all: bool
) -> int:
    """Pull supervisor comments on a project's reports back from the relay (C2).

    Args:
        project_name: The project whose comments to pull.
        config_path: Path to orion.toml.
        as_json: When True, print the raw JSON response (for the session skill);
            otherwise print a human-readable listing.
        show_all: When True, show ALL comments and do NOT advance the unread marker
            (an explicit re-read); when False (default), show only comments newer than
            the stored watermark and advance it afterward.

    Returns:
        Exit code: 0 on a successful pull (including "nothing new"); 1 on a config /
        secrets error, a disabled relay, or a failed pull.

    Why:
        This closes the C2 loop into the developer's workflow: supervisor replies that
        previously lived only on the dashboard are pulled to the machine where you
        work. The pull is BY PROJECT (the local side never recorded the relay's comment
        ids), authenticated with the SAME Bearer token the push uses (whoever can push
        a project's reports can read its replies), and "unread" is a LOCAL watermark —
        the relay stays a dumb append-only store. The default advances that watermark so
        each run shows only what's new; --all is the escape hatch to re-read everything
        without disturbing the cursor. The token is read here, in the CLI (like every
        other secret), only when the relay is enabled, and a missing one is named, never
        printed. pull_comments is the module-global so a test can monkeypatch it,
        mirroring relay_push.
    """
    try:
        config = load_config(config_path)
        project = get_project(config, project_name)
        load_secrets(config_path)

        relay_cfg = config.relay
        # Pulling comments is only meaningful when a relay is configured — that is
        # where comments live. A disabled/absent relay is a clean, actionable error
        # (not a crash), mirroring how the push path treats a disabled relay as a no-op.
        if not relay_cfg.enabled:
            print(
                f"Error: cannot pull comments for {project.name!r} — no relay is "
                f"enabled in {config_path}. Comments live on the relay you push reports "
                f"to; enable the [relay] table to read replies back.",
                file=sys.stderr,
            )
            return 1

        # The Bearer token lives in .env, named by token_env_var (never in the config).
        # A missing one is named by get_required and surfaces as a clean SecretsError.
        token = get_required(relay_cfg.token_env_var)

        conn = open_state(config.state_db)
        # since_id is the unread cursor: 0 for --all (fetch the full history), else the
        # stored watermark (fetch only what's newer). Keyed by (project, relay_url).
        since_id = (
            0 if show_all else get_comment_watermark(conn, project.name, relay_cfg.url)
        )
        response = pull_comments(relay_cfg.url, token, project.name, since_id)
    except (ConfigError, SecretsError, DeliveryError) as exc:
        # All three are user-fixable (a config typo, a missing token, a down relay).
        # Print cleanly and fail; never advance the watermark on a failed pull.
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    comments = response.get("comments", [])
    # latest_id lets us advance the watermark even when the listing is empty; fall back
    # to since_id defensively if the relay omitted it.
    latest_id = response.get("latest_id", since_id)

    if as_json:
        # Emit the response verbatim for the skill to parse (it reads `comments`).
        print(json.dumps(response))
    else:
        _print_comments(comments, project.name, show_all)

    # Advance the watermark ONLY on a normal run — --all is an explicit re-read that
    # must not move the cursor. Advancing to latest_id is idempotent (it echoes
    # since_id when nothing is new), so a no-new-comments run is a safe no-op write.
    if not show_all:
        pulled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        set_comment_watermark(conn, project.name, relay_cfg.url, latest_id, pulled_at)

    return 0


def _print_comments(comments: list[dict], project_name: str, show_all: bool) -> None:
    """Print a project's pulled comments as a human-readable listing.

    Args:
        comments: The comment dicts from pull_comments (id, author, body, created_at).
        project_name: The project the comments belong to (for the header/empty line).
        show_all: Whether this was an --all pull, which only changes the empty-state
            wording ("no comments" vs "no new comments").

    Returns:
        None. Writes to stdout.

    Why:
        The default, human-facing output: one line per comment as
        "author · <Pacific time> · body". An empty result is a friendly one-liner
        rather than silence, so a run that found nothing reads as a deliberate result.
        The wording distinguishes "no comments at all" (--all) from "nothing new since
        last check" (default) so the user knows which question was answered.
    """
    if not comments:
        qualifier = "" if show_all else " new"
        print(f"No{qualifier} comments for {project_name!r}.")
        return

    print(f"{len(comments)} comment(s) for {project_name!r}:")
    for comment in comments:
        # author is the self-entered display name and may be "" (anonymous); show a
        # placeholder so the line never starts with a bare separator.
        author = comment["author"] or "(anonymous)"
        print(f"  {author} · {_format_pacific(comment['created_at'])} · {comment['body']}")


def _format_pacific(iso: str) -> str:
    """Render a stored UTC ISO-8601 timestamp as human Pacific wall-clock time.

    Args:
        iso: An ISO-8601 timestamp with an offset (as the relay stores it, e.g.
            "2026-06-19T19:30:00+00:00").

    Returns:
        A human string in America/Los_Angeles, e.g. "2026-06-19 12:30 PDT".

    Why:
        Comments are shown in a fixed Pacific zone (matching the dashboard's display
        choice) so timestamps read consistently regardless of the machine's local
        timezone. ZoneInfo is internally cached, so constructing it per call is cheap;
        doing it HERE rather than at module import keeps a missing tzdata from breaking
        every other command — only the `comments` listing depends on it. This
        deliberately does NOT import relay/render.py's formatter: orion/ shares no code
        with relay/, the same independence the duplicated busy-timeout constant reflects.
    """
    # fromisoformat parses the stored "+00:00" offset; astimezone converts that absolute
    # instant to California wall-clock time (zoneinfo applies DST -> PDT/PST).
    pacific = datetime.fromisoformat(iso).astimezone(ZoneInfo("America/Los_Angeles"))
    return pacific.strftime("%Y-%m-%d %H:%M %Z")


def _load_run_bot():
    """Import the Slack bot's run_bot() from the installed orion.bot package.

    Returns:
        The orion.bot.slack_bot.run_bot callable.

    Why:
        Unlike _load_relay_serve (which reaches a top-level, out-of-wheel package),
        orion.bot IS part of the installed distribution, so this is a plain import —
        no sys.path hack needed. We still import it through a tiny loader (rather than
        at module top) for two reasons: it keeps cli.py importable without touching the
        bot package on every other command, and it gives tests a single seam to
        monkeypatch run_bot. The import here does NOT pull in slack-bolt — slack_bot.py
        imports that lazily inside run_bot — so a missing optional dependency surfaces
        only when the bot actually starts, as a clean ConfigError cmd_bot reports.
    """
    from orion.bot.slack_bot import run_bot

    return run_bot


def cmd_bot(config_path: Path) -> int:
    """Run the always-on Slack bot: relay channel replies into report comments (C2-bots).

    Args:
        config_path: Path to orion.toml (also locates the sibling .env for secrets).

    Returns:
        Exit code: 0 on a clean shutdown (Ctrl-C); 1 on a setup error — the bot or the
        relay is disabled, a required token is missing, or the optional slack-bolt
        dependency is not installed.

    Why:
        This is the thin CLI adapter over the bot shell, mirroring cmd_relay_serve. The
        bot WRITES INTO the relay (a chat reply becomes a comment on the relay), so both
        [bot] and [relay] must be enabled — a disabled either is a clean, actionable
        error, not a crash. Three secrets are read HERE (like every other secret), only
        when enabled, and a missing one is named by get_required, never printed: the
        Slack bot token, the Socket Mode app-level token, and the relay Bearer token
        (reused from [relay]). The channel→project bindings become the runtime map. The
        run_bot import is the module seam tests monkeypatch; the missing-dependency case
        surfaces as a ConfigError from inside run_bot and is reported the same way.
    """
    try:
        config = load_config(config_path)
        load_secrets(config_path)

        bot_cfg = config.bot
        # The bot is opt-in; without an enabled [bot] there is nothing to run.
        if not bot_cfg.enabled:
            print(
                f"Error: no bot is enabled in {config_path}. Add an enabled [bot] "
                f"table (platform, token env vars, and [[bot.channels]]) to run it.",
                file=sys.stderr,
            )
            return 1

        relay_cfg = config.relay
        # The bot's whole job is to land replies in the relay's comment store, so a
        # relay must be configured — its url + token are the bot's write target.
        if not relay_cfg.enabled:
            print(
                f"Error: the bot writes replies into the relay, but no [relay] is "
                f"enabled in {config_path}. Enable [relay] (the bot reuses its url and "
                f"token) before running the bot.",
                file=sys.stderr,
            )
            return 1

        # Secrets live in .env, named by the *_env_var fields (never in the config). A
        # missing one is named by get_required and surfaces as a clean SecretsError.
        bot_token = get_required(bot_cfg.token_env_var)
        app_token = get_required(bot_cfg.app_token_env_var)
        relay_token = get_required(relay_cfg.token_env_var)

        # The frozen (channel_id, project) pairs become the runtime lookup map.
        channel_map = dict(bot_cfg.channel_bindings)
        run_bot = _load_run_bot()
    except (SecretsError, ConfigError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        # Blocks until Ctrl-C. run_bot raises ConfigError if slack-bolt is missing
        # (the optional extra) — surfaced here as a clean, actionable error.
        run_bot(bot_token, app_token, channel_map, relay_cfg.url, relay_token)
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


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

        serve = _load_relay_serve()
        # AuthConfig + the loopback test live in the relay package, importable now that
        # _load_relay_serve has put the repo root on sys.path if it was needed.
        from relay.server import AuthConfig, _is_loopback

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
            auth=auth,
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


def cmd_relay_user_add(
    name: str, role: str, projects: list[str], config_path: Path
) -> int:
    """Provision a relay user and print their one-time access key (`relay-user add`).

    Args:
        name: The new user's unique handle.
        role: "viewer" or "admin".
        projects: Project names a viewer may see (ignored for an admin).
        config_path: Path to orion.toml.

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
    try:
        relay_url, admin_token = _load_relay_admin(config_path)
        result = relay_create_user(relay_url, admin_token, name, role, projects)
    except (ConfigError, SecretsError, DeliveryError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Provisioned user {result['name']!r} (role: {result['role']}).")
    scope = result.get("projects") or []
    if result["role"] == "admin":
        print("  Scope: all projects (admin).")
    elif scope:
        print(f"  Scope: {', '.join(scope)}")
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
        print(
            f"{user['name']}  [{user['role']}, {status}]  "
            f"scope: {scope}  last login: {last_login}"
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


def _collect_for(project: ProjectConfig, collector: str, prior: str | None):
    """Run one named collector, adapting it to its (differing) call signature.

    Args:
        project: The project config (source of repo_path, share_level, file paths).
        collector: The collector name ("git", "tasks", or "notes").
        prior: That collector's last-reported marker, or None on a first run.

    Returns:
        The collector's CollectorResult.

    Why:
        The collectors legitimately take different arguments (git wants a repo and
        a share level; the file collectors want a path), so the orchestrator's
        uniform loop needs one place that maps a name to the right call. This is a
        plain dispatch — deliberately NOT a plugin/registry — so the set of signals
        stays small, explicit, and easy to read. config validation guarantees the
        name is supported and that an enabled file collector has its path set, so
        the final raise is a defensive guard, not an expected path.
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

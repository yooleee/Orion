# =============================================================================
# cli/__init__.py
# -----------------------------------------------------------------------------
# Responsible for: The `orion` command: main() builds the parser (cli/_parser.py),
#                  parses, and dispatches to the command modules below. This package
#                  replaced the single 5,600-line cli.py in CS-O PR10 with a MECHANICAL
#                  split into cohesive domains — no behavior changed, pinned by the
#                  --help snapshot, the entry-point tests and the full suite.
# Role in project: Ties every other module together:
#   config -> secrets -> state -> collect -> redact -> (LLM | passthrough)
#   -> redact -> build report -> compose -> preview/confirm -> deliver -> advance.
#   The modules: report (the pipeline + delivery core), push (checklist/disciplines
#   carriers), intake, project_setup (add-project/install-hook), inspect (status/
#   projects/check/baseline), discussions, relay_serve, relay_admin (relay-user /
#   relay-project), with _parser, _console, _status and _checklist as shared parts.
# Import surface: `from orion import cli; cli.<name>` keeps working for every name the
#   suite reaches (re-exported below). PATCH SEAM RULE: monkeypatch the SUBMODULE that
#   calls a name (cli.report.relay_push, cli.relay_serve._load_relay_serve, ...) —
#   patching the re-export on this package cannot reach a submodule's own binding.
# =============================================================================
from __future__ import annotations

import os
import sys
from pathlib import Path

from ._console import _ensure_utf8_output, _reconfigure_stream_utf8
from ._parser import DEFAULT_CONFIG, build_parser
from ._status import (
    STATUS_ABORTED,
    STATUS_FAILED,
    STATUS_NOT_DUE,
    STATUS_NO_ACTIVITY,
    STATUS_NO_CHANGE,
    STATUS_NO_CHECKLIST,
    STATUS_PUSHED,
    STATUS_SENT,
    STATUS_SKIPPED_NOT_OPTED,
)
from ._checklist import (
    _checklist_payload,
    _redacted_about,
)
from .report import (
    _collect_for,
    _is_due,
    _is_due_at,
    _relay_push,
    _report_collectors_of,
    _run_report,
    cmd_report,
)
from .push import (
    _checklist_content_hash,
    _watch_tick,
    cmd_checklist_push,
    cmd_disciplines_push,
    push_checklist,
    push_disciplines,
)
from .intake import (
    cmd_intake,
)
from .project_setup import (
    _seed_checklist_from_doc,
    _status_is_done,
    cmd_add_project,
    cmd_install_hook,
)
from .inspect import (
    cmd_baseline,
    cmd_check,
    cmd_projects,
    cmd_status,
)
from .discussions import (
    _print_discussions,
    cmd_discussions_pull,
    cmd_discussions_reply,
)
from .relay_serve import (
    _relay_serve_overrides,
    cmd_relay_serve,
)
from .relay_admin import (
    cmd_relay_project_lifecycle,
    cmd_relay_project_visibility,
    cmd_relay_user_add,
    cmd_relay_user_deactivate,
    cmd_relay_user_delete,
    cmd_relay_user_grant,
    cmd_relay_user_key_add,
    cmd_relay_user_key_list,
    cmd_relay_user_key_revoke,
    cmd_relay_user_list,
    cmd_relay_user_password_set,
    cmd_relay_user_password_unlock,
    cmd_relay_user_rename,
    cmd_relay_user_role,
    cmd_relay_user_set_operator,
    cmd_relay_user_ungrant,
    relay_add_user_key,
    relay_delete_user,
    relay_list_user_keys,
    relay_rename_user,
    relay_revoke_user,
    relay_revoke_user_key,
    relay_set_user_role,
)
from .report import (
    _build_extractor,
    _build_summarizer,
    compose,
    discord_send,
    relay_push,
    slack_send,
)
from .relay_serve import (
    _load_relay_serve,
)
from .discussions import (
    post_discussion,
    pull_discussions,
)
from .relay_admin import (
    relay_create_user,
    relay_grant_projects,
    relay_list_users,
    relay_set_project_lifecycle,
    relay_ungrant_projects,
)

# The package's import surface, stated explicitly (see the header). Private names are
# listed on purpose: the test suite reaches them through this package.
__all__ = [
    "DEFAULT_CONFIG",
    "STATUS_ABORTED",
    "STATUS_FAILED",
    "STATUS_NOT_DUE",
    "STATUS_NO_ACTIVITY",
    "STATUS_NO_CHANGE",
    "STATUS_NO_CHECKLIST",
    "STATUS_PUSHED",
    "STATUS_SENT",
    "STATUS_SKIPPED_NOT_OPTED",
    "_build_extractor",
    "_build_summarizer",
    "_checklist_content_hash",
    "_checklist_payload",
    "_collect_for",
    "_ensure_utf8_output",
    "_is_due",
    "_is_due_at",
    "_load_relay_serve",
    "_print_discussions",
    "_reconfigure_stream_utf8",
    "_redacted_about",
    "_relay_push",
    "_relay_serve_overrides",
    "_report_collectors_of",
    "_run_report",
    "_seed_checklist_from_doc",
    "_status_is_done",
    "_watch_tick",
    "build_parser",
    "cmd_add_project",
    "cmd_baseline",
    "cmd_check",
    "cmd_checklist_push",
    "cmd_disciplines_push",
    "cmd_discussions_pull",
    "cmd_discussions_reply",
    "cmd_install_hook",
    "cmd_intake",
    "cmd_projects",
    "cmd_relay_project_lifecycle",
    "cmd_relay_project_visibility",
    "cmd_relay_serve",
    "cmd_relay_user_add",
    "cmd_relay_user_deactivate",
    "cmd_relay_user_delete",
    "cmd_relay_user_grant",
    "cmd_relay_user_key_add",
    "cmd_relay_user_key_list",
    "cmd_relay_user_key_revoke",
    "cmd_relay_user_list",
    "cmd_relay_user_password_set",
    "cmd_relay_user_password_unlock",
    "cmd_relay_user_rename",
    "cmd_relay_user_role",
    "cmd_relay_user_set_operator",
    "cmd_relay_user_ungrant",
    "cmd_report",
    "cmd_status",
    "compose",
    "discord_send",
    "main",
    "post_discussion",
    "pull_discussions",
    "push_checklist",
    "push_disciplines",
    "relay_add_user_key",
    "relay_create_user",
    "relay_delete_user",
    "relay_grant_projects",
    "relay_list_user_keys",
    "relay_list_users",
    "relay_push",
    "relay_rename_user",
    "relay_revoke_user",
    "relay_revoke_user_key",
    "relay_set_project_lifecycle",
    "relay_set_user_role",
    "relay_ungrant_projects",
    "slack_send",
]


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

    parser = build_parser(default_config)

    args = parser.parse_args(argv)
    if args.command == "report":
        return cmd_report(
            args.project, Path(args.config), args.yes, args.all_projects, args.due
        )
    if args.command == "intake":
        return cmd_intake(
            args.project,
            Path(args.config),
            args.message,
            args.yes,
            body_file=Path(args.body_file) if args.body_file is not None else None,
            relay_only=args.relay_only,
            generated_at=args.generated_at,
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
            grant=args.grant,
        )
    if args.command == "projects":
        return cmd_projects(Path(args.config), args.project)
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
    if args.command == "relay-serve":
        return cmd_relay_serve(
            _relay_serve_overrides(args),
            Path(args.config),
            allow_legacy_admin=args.allow_legacy_admin,
            init_secrets=args.init_secrets,
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
                key_only=args.key_only,
            )
        if args.relay_user_command == "list":
            return cmd_relay_user_list(Path(args.config))
        if args.relay_user_command == "deactivate":
            return cmd_relay_user_deactivate(args.name, Path(args.config))
        if args.relay_user_command == "grant":
            return cmd_relay_user_grant(args.name, args.projects, Path(args.config))
        if args.relay_user_command == "ungrant":
            return cmd_relay_user_ungrant(args.name, args.projects, Path(args.config))
        if args.relay_user_command == "key":
            if args.relay_user_key_command == "add":
                return cmd_relay_user_key_add(
                    args.name, args.label, Path(args.config), key_only=args.key_only
                )
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

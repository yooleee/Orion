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
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from orion.collectors import LANE_RAW, LANE_STRUCTURED
from orion.collectors.git import GitError
from orion.collectors.git import collect as collect_git
from orion.collectors.notes import NotesError
from orion.collectors.notes import collect as collect_notes
from orion.collectors.tasks import TasksError
from orion.collectors.tasks import collect as collect_tasks
from orion.compose import ComposedMessage, compose
from orion.config import (
    ConfigError,
    ProjectConfig,
    Recipient,
    RelayConfig,
    SummarizerConfig,
    get_project,
    load_config,
)
from orion.delivery import DeliveryError
from orion.delivery.discord import send as discord_send
from orion.delivery.relay import pull_comments, push as relay_push
from orion.delivery.slack import send as slack_send
from orion.hooks import SUPPORTED_HOOKS, build_hook_script, resolve_hooks_dir
from orion.merge import merge_sections
from orion.redact import redact
from orion.report import ReportBlob, build_report, serialize_blob
from orion.secrets import SecretsError, get_required, load_secrets
from orion.state import (
    get_comment_watermark,
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
        "--config",
        default=default_config,
        help=f"Path to the config file, used only to locate .env (default: {default_config}; or set $ORION_CONFIG).",
    )

    args = parser.parse_args(argv)
    if args.command == "report":
        return cmd_report(args.project, Path(args.config), args.yes, args.all_projects)
    if args.command == "intake":
        return cmd_intake(args.project, Path(args.config), args.message, args.yes)
    if args.command == "install-hook":
        return cmd_install_hook(
            args.project, Path(args.config), args.hook, args.print_only, args.force
        )
    if args.command == "projects":
        return cmd_projects(Path(args.config))
    if args.command == "show":
        return cmd_show(args.project, Path(args.config))
    if args.command == "check":
        return cmd_check(Path(args.config))
    if args.command == "baseline":
        return cmd_baseline(args.project, Path(args.config))
    if args.command == "comments":
        return cmd_comments(
            args.project, Path(args.config), as_json=args.as_json, show_all=args.show_all
        )
    if args.command == "relay-serve":
        return cmd_relay_serve(
            args.host,
            args.port,
            Path(args.db),
            args.token_env,
            args.view_token_env,
            args.require_view_auth,
            Path(args.config),
        )
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
        sections: list[tuple[str, str]] = []          # (title, finished body)
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

            sections.append((_COLLECTOR_TITLES[collector_name], body))
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
        redacted_sections: list[tuple[str, str]] = []
        for title, section_body in sections:
            pass2 = redact(section_body)
            redaction_hits += pass2.hit_count
            safe_section = pass2.text.strip()
            if not safe_section:
                continue
            redacted_sections.append((title, safe_section))

        merged = merge_sections(redacted_sections)
        safe_body = merged
        if not safe_body.strip():
            print(
                f"Refusing to send {project.name!r}: the report body is empty "
                f"after redaction.",
                file=sys.stderr,
            )
            return STATUS_FAILED

        # --- Build the portable report blob ---
        # lane is provenance: RAW if the LLM touched any part of this run, else
        # STRUCTURED. source_marker is now per-collector (in state), so the blob's
        # single field is no longer meaningful — pass "" (see KI-8). The
        # twice-redacted sections ride along for B3's structured rendering.
        generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        lane = LANE_RAW if any_raw_lane else LANE_STRUCTURED
        blob = build_report(
            project, safe_body, lane, "", generated_at, sections=tuple(redacted_sections)
        )

        # --- Compose once per distinct channel ---
        # Same body, formatted for each channel's Markdown dialect; recipients are
        # routed to their channel's rendering in _deliver.
        messages = {
            ch: compose(blob, ch, display_timezone) for ch in _channels(project)
        }

        # --- Preview gate ---
        # By construction, if assume_yes is True here then project.auto_send is
        # also True (the not-opted case returned above), so this branch is the
        # BOTH-required bypass. Otherwise we always show the human preview — which
        # is why auto_send alone (no --yes) can never skip it.
        if assume_yes:
            print(
                f"Auto-sending {project.name!r} "
                f"(preview skipped: --yes and auto_send=true)."
            )
        elif not _preview_and_confirm(messages, redaction_hits):
            print("Aborted. Nothing was sent; state unchanged.")
            return STATUS_ABORTED

        # --- Deliver to each recipient's webhook (per their channel) ---
        sent_to, failed = _deliver(messages, project)
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
        # the C3 multi-party model (KI-11). See docs/known-issues.md KI-1.
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
        # never affect the delivered-report outcome or the markers.
        _relay_push(blob, relay_cfg)
        return STATUS_SENT

    except (GitError, SummarizerError, TasksError, NotesError, SecretsError) as exc:
        # Per-project, fail-soft: print a clean message and report FAILED so an
        # --all run can continue with the next project. SecretsError here is the
        # ANTHROPIC key fetch on the raw lane (the webhook fetch is handled inside
        # _deliver); setup-time config/secrets errors are caught by the caller.
        print(f"Error reporting {project.name!r}: {exc}", file=sys.stderr)
        return STATUS_FAILED


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

        # A pushed update is already audience-ready: structured lane, empty marker.
        generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        blob = build_report(project, safe_body, LANE_STRUCTURED, "", generated_at)

        # Compose per distinct channel and route each recipient accordingly —
        # identical delivery path to cmd_report (just no markers afterward).
        messages = {
            ch: compose(blob, ch, config.display_timezone)
            for ch in _channels(project)
        }
        # --yes skips the terminal preview (the skill already showed the summary
        # for in-session approval); otherwise preview-before-send as usual.
        if assume_yes:
            print(f"Sending {project.name!r} (preview skipped: --yes).")
        elif not _preview_and_confirm(messages, redaction_hits):
            print("Aborted. Nothing was sent.")
            return 0

        sent_to, failed = _deliver(messages, project)
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
        for collector, path in (("tasks", project.tasks_file), ("notes", project.notes_file)):
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
        except (GitError, TasksError, NotesError) as exc:
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
    config_path: Path,
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
        config_path: Path to orion.toml, used only to locate the sibling .env that
            holds the secrets.

    Returns:
        Exit code: 0 on a clean shutdown (Ctrl-C); 1 on a setup error (missing ingest
        token, the relay package can't be imported, or the fail-closed guard refuses a
        non-loopback bind without a view secret).

    Why:
        This is the thin CLI adapter over relay/server.py — it reads the ingest token
        from .env (the same secret the pushing side sends as a Bearer token) and the
        OPTIONAL dashboard read secret, then hands off to serve(). Secrets are read
        HERE, like every other secret; a missing INGEST token is a clean SecretsError
        naming the variable. The view secret is read softly (empty -> None) because on
        loopback the dashboard may serve open; the relay's guard — not this CLI — is
        what refuses a non-loopback bind without it, so the rule is enforced for every
        caller, not just this one. serve() blocks until interrupted, then returns.
    """
    try:
        # Load .env beside the config (like every command), then read the secrets.
        load_secrets(config_path)
        token = get_required(token_env)
        # Optional: empty/unset -> None. The fail-closed guard inside serve() refuses a
        # non-loopback bind when it is None; on loopback, None means "reads open".
        view_token = os.environ.get(view_token_env, "").strip() or None
        serve = _load_relay_serve()
    except (SecretsError, ConfigError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        # Blocks until Ctrl-C; serve() prints its bound address and read-auth state.
        # The guard raises ValueError BEFORE binding if host is non-loopback without a
        # view secret — surfaced here as a clean, actionable error, not a traceback.
        serve(host, port, db_path, token, view_token, require_view_auth)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
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
    messages: dict[str, ComposedMessage], project: ProjectConfig
) -> tuple[list[str], list[tuple[str, str]]]:
    """Send each recipient the message composed for that recipient's channel.

    Args:
        messages: Map of channel name -> ComposedMessage for that channel.
        project: The project whose recipients receive it.

    Returns:
        A (sent_to, failed) tuple: the names that received the message, and a list
        of (name, error) for the ones that failed. Per-recipient failures are also
        printed to stderr here.

    Why:
        Both cmd_report and cmd_intake deliver the same way — for each recipient,
        pick their channel's rendering and its sender, try the send, and let one
        failure not abort the others. Routing lives here (one place, DRY); each
        caller decides what "nobody received it" means (report does not advance
        markers; intake just reports the failure). It does NOT decide success/exit
        codes — that stays with the caller, which knows its own bookkeeping.
    """
    sent_to: list[str] = []
    failed: list[tuple[str, str]] = []
    for recipient in project.recipients:
        try:
            url = get_required(recipient.webhook_env_var)
            # Route to the right channel's message + sender. config validation
            # guarantees recipient.channel is supported, so both lookups hit.
            send = _sender_for(recipient.channel)
            send(messages[recipient.channel].payload, url)
            sent_to.append(recipient.name)
        except (SecretsError, DeliveryError) as exc:
            # A per-recipient failure shouldn't abort the others.
            failed.append((recipient.name, str(exc)))

    for name, err in failed:
        print(f"  ✗ {name}: {err}", file=sys.stderr)
    return sent_to, failed


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
    raise ConfigError(f"Unknown collector {collector!r}.")


def _preview_and_confirm(messages: dict[str, ComposedMessage], redaction_hits: int) -> bool:
    """Show the composed message(s) and ask the user to confirm sending.

    Args:
        messages: Map of channel name -> the message that channel will receive.
            One preview block is shown per channel.
        redaction_hits: How many potential secrets were redacted in this run.

    Returns:
        True only if the user explicitly confirms (y/yes); False otherwise.

    Why:
        Preview-before-send is the human gate that makes the whole privacy story
        trustworthy — the user sees the EXACT bytes each platform will receive
        before they leave the machine. With more than one channel we show a block
        per channel (Slack and Discord render differently), labeled so it's clear
        which is which; a single-channel run shows one unlabeled block, identical
        to before. One confirm covers all channels. We default to NO (a bare
        Enter, EOF, or anything but yes does not send) and surface the redaction
        count so the user scrutinizes harder when the redactor fired.
    """
    bar = "=" * 60
    multi = len(messages) > 1
    for channel, message in messages.items():
        # Label the block only when there's more than one channel, so a
        # single-channel preview is unchanged from Phase 1/2.
        label = f" ({channel})" if multi else ""
        print(bar)
        print(f"PREVIEW{label} — this report has NOT been sent yet")
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

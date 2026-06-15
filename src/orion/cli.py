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
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from orion.collectors import LANE_RAW
from orion.collectors.git import GitError
from orion.collectors.git import collect as collect_git
from orion.compose import compose
from orion.config import ConfigError, get_project, load_config
from orion.delivery import DeliveryError
from orion.delivery.discord import send as discord_send
from orion.redact import redact
from orion.report import build_report
from orion.secrets import SecretsError, get_required, load_secrets
from orion.state import advance_state, get_last_reported, open_state, record_report
from orion.summarize import SummarizerError, summarize_raw

DEFAULT_CONFIG = "orion.toml"


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
    parser = argparse.ArgumentParser(
        prog="orion",
        description="Turn local git activity into supervisor-ready progress updates.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    report_parser = subparsers.add_parser(
        "report", help="Generate and (after preview) send a progress report."
    )
    report_parser.add_argument("project", help="Project name as defined in orion.toml.")
    report_parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help=f"Path to the config file (default: {DEFAULT_CONFIG}).",
    )

    args = parser.parse_args(argv)
    if args.command == "report":
        return cmd_report(args.project, Path(args.config))
    return 1  # Unreachable: subparsers are required.


def cmd_report(project_name: str, config_path: Path) -> int:
    """Run the full report pipeline for one project.

    Args:
        project_name: The project to report on.
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 if a report was sent or there was nothing to report; 1 on
        any error or if no delivery succeeded.

    Why:
        This function encodes the pipeline order and the safety rules (two-pass
        redaction, preview-before-send, advance-only-after-success). Known,
        user-fixable errors are caught and printed cleanly instead of dumping a
        traceback.
    """
    try:
        config = load_config(config_path)
        project = get_project(config, project_name)
        load_secrets()
        conn = open_state(config.state_db)

        since = get_last_reported(conn, project.name)

        # --- Collect (raw lane: git) ---
        result = collect_git(project.repo_path, since, project.share_level)
        if not result.has_activity:
            print(f"No new activity for {project.name!r} since the last report.")
            return 0

        # --- Redaction pass 1: before anything leaves for the LLM ---
        pass1 = redact(result.raw_text)

        # --- The conditional-LLM seam ---
        if result.lane == LANE_RAW:
            # Build the client here (not in summarize.py) so secret handling stays
            # in the CLI. Construction is local-only; the key is validated/fetched
            # by get_required, which raises a clear error if missing.
            client = anthropic.Anthropic(api_key=get_required("ANTHROPIC_API_KEY"))
            body = summarize_raw(pass1.text, project.share_level, client=client)
        else:
            # Phase 2 structured lane: pass through, no LLM. Dead in Phase 1.
            body = pass1.text

        # --- Redaction pass 2: safety net on the final body before send ---
        pass2 = redact(body)
        safe_body = pass2.text
        if not safe_body.strip():
            print(
                "Refusing to send: the report body is empty after redaction.",
                file=sys.stderr,
            )
            return 1
        redaction_hits = pass1.hit_count + pass2.hit_count

        # --- Build the portable report blob ---
        generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        blob = build_report(
            project, safe_body, result.lane, result.new_marker, generated_at
        )

        # --- Compose + preview (Phase 1: single channel, so preview once) ---
        message = compose(blob, "discord")
        if not _preview_and_confirm(message, redaction_hits):
            print("Aborted. Nothing was sent; state unchanged.")
            return 0

        # --- Deliver to each recipient's webhook ---
        sent_to: list[str] = []
        failed: list[tuple[str, str]] = []
        for recipient in project.recipients:
            try:
                url = get_required(recipient.webhook_env_var)
                discord_send(message, url)
                sent_to.append(recipient.name)
            except (SecretsError, DeliveryError) as exc:
                # A per-recipient failure shouldn't abort the others.
                failed.append((recipient.name, str(exc)))

        for name, err in failed:
            print(f"  ✗ {name}: {err}", file=sys.stderr)

        if not sent_to:
            print("No deliveries succeeded; state not advanced.", file=sys.stderr)
            return 1

        # --- Advance state ONLY after at least one successful send ---
        advance_state(conn, project.name, result.new_marker, generated_at)
        record_report(conn, project.name, safe_body, sent_to, generated_at)

        print(f"Sent to: {', '.join(sent_to)}.")
        if failed:
            print(
                f"(Note: {len(failed)} recipient(s) failed; state advanced because "
                f"at least one delivery succeeded.)"
            )
        return 0

    except (ConfigError, SecretsError, GitError, SummarizerError) as exc:
        # All of these are user-fixable setup/operational problems. Print a clean
        # message and fail closed (nothing sent, state untouched).
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def _preview_and_confirm(message: str, redaction_hits: int) -> bool:
    """Show the composed message and ask the user to confirm sending.

    Args:
        message: The composed message that would be sent.
        redaction_hits: How many potential secrets were redacted in this run.

    Returns:
        True only if the user explicitly confirms (y/yes); False otherwise.

    Why:
        Preview-before-send is the human gate that makes the whole privacy story
        trustworthy — the user sees the exact bytes before they leave the machine.
        We default to NO (a bare Enter, EOF, or anything but yes does not send),
        and surface the redaction count so the user scrutinizes harder when the
        redactor fired.
    """
    bar = "=" * 60
    print(bar)
    print("PREVIEW — this report has NOT been sent yet")
    print(bar)
    print(message)
    print(bar)
    if redaction_hits > 0:
        print(f"⚠  {redaction_hits} potential secret(s) were redacted from this report.")

    try:
        answer = input("Send this report? [y/N] ")
    except EOFError:
        # No interactive input available -> treat as "no" (fail closed).
        return False
    return answer.strip().lower() in ("y", "yes")

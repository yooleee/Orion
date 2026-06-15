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

from orion.collectors import LANE_RAW, LANE_STRUCTURED
from orion.collectors.git import GitError
from orion.collectors.git import collect as collect_git
from orion.collectors.notes import NotesError
from orion.collectors.notes import collect as collect_notes
from orion.collectors.tasks import TasksError
from orion.collectors.tasks import collect as collect_tasks
from orion.compose import compose
from orion.config import ConfigError, ProjectConfig, get_project, load_config
from orion.delivery import DeliveryError
from orion.delivery.discord import send as discord_send
from orion.delivery.slack import send as slack_send
from orion.merge import merge_sections
from orion.redact import redact
from orion.report import build_report
from orion.secrets import SecretsError, get_required, load_secrets
from orion.state import get_marker, open_state, record_report, set_marker
from orion.summarize import SummarizerError, summarize_raw

# Section title shown in the report for each collector. Kept as an explicit table
# (NOT a registry) so the orchestrator stays a plain dict lookup and adding a
# signal is a one-line change here.
_COLLECTOR_TITLES = {
    "git": "Code activity",
    "tasks": "Completed tasks",
    "notes": "Notes",
}


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

    intake_parser = subparsers.add_parser(
        "intake",
        help="Send a pushed/hand-written update for a project (skips collectors & LLM).",
    )
    intake_parser.add_argument("project", help="Project name as defined in orion.toml.")
    intake_parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help=f"Path to the config file (default: {DEFAULT_CONFIG}).",
    )
    intake_parser.add_argument(
        "--message",
        "-m",
        default=None,
        help="The update body. If omitted, the body is read from stdin.",
    )

    args = parser.parse_args(argv)
    if args.command == "report":
        return cmd_report(args.project, Path(args.config))
    if args.command == "intake":
        return cmd_intake(args.project, Path(args.config), args.message)
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

        # --- Collect from each enabled signal, in config order ---
        # Each collector becomes one titled section; each tracks its own delta
        # marker independently (advancing git must not disturb tasks/notes).
        sections: list[tuple[str, str]] = []          # (title, finished body)
        pending_markers: list[tuple[str, str]] = []   # (collector, new_marker) to advance on send
        redaction_hits = 0
        any_raw_lane = False
        # Built lazily, and only if a RAW collector actually has activity, so a
        # structured-only run never needs the Anthropic key. Local construction
        # keeps secret handling in the CLI (not in summarize.py).
        client = None

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
                if client is None:
                    client = anthropic.Anthropic(api_key=get_required("ANTHROPIC_API_KEY"))
                body = summarize_raw(pass1.text, project.share_level, client=client)
                any_raw_lane = True
            else:
                # Structured lane: pass through, NO LLM. This is the seam Phase 1
                # left dead; structured collectors now fill it.
                body = pass1.text

            sections.append((_COLLECTOR_TITLES[collector_name], body))
            pending_markers.append((collector_name, result.new_marker))

        if not sections:
            print(f"No new activity for {project.name!r} since the last report.")
            return 0

        # --- Merge per-collector bodies into one report body ---
        merged = merge_sections(sections)

        # --- Redaction pass 2: safety net on the final merged body before send ---
        pass2 = redact(merged)
        safe_body = pass2.text
        redaction_hits += pass2.hit_count
        if not safe_body.strip():
            print(
                "Refusing to send: the report body is empty after redaction.",
                file=sys.stderr,
            )
            return 1

        # --- Build the portable report blob ---
        # lane is provenance: RAW if the LLM touched any part of this run, else
        # STRUCTURED. source_marker is now per-collector (in state), so the blob's
        # single field is no longer meaningful — pass "" (see KI-8).
        generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        lane = LANE_RAW if any_raw_lane else LANE_STRUCTURED
        blob = build_report(project, safe_body, lane, "", generated_at)

        # --- Compose once per distinct channel, then preview all + one confirm ---
        # Same body, formatted for each channel's Markdown dialect; recipients are
        # routed to their channel's rendering in _deliver.
        messages = {ch: compose(blob, ch) for ch in _channels(project)}
        if not _preview_and_confirm(messages, redaction_hits):
            print("Aborted. Nothing was sent; state unchanged.")
            return 0

        # --- Deliver to each recipient's webhook (per their channel) ---
        sent_to, failed = _deliver(messages, project)
        if not sent_to:
            print("No deliveries succeeded; state not advanced.", file=sys.stderr)
            return 1

        # --- Advance markers ONLY after at least one successful send, and ONLY
        # for the collectors that had activity this run. ---
        for collector_name, marker in pending_markers:
            set_marker(conn, project.name, collector_name, marker, generated_at)
        record_report(conn, project.name, safe_body, sent_to, generated_at)

        print(f"Sent to: {', '.join(sent_to)}.")
        if failed:
            print(
                f"(Note: {len(failed)} recipient(s) failed; state advanced because "
                f"at least one delivery succeeded.)"
            )
        return 0

    except (ConfigError, SecretsError, GitError, SummarizerError, TasksError, NotesError) as exc:
        # All of these are user-fixable setup/operational problems. Print a clean
        # message and fail closed (nothing sent, state untouched).
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_intake(project_name: str, config_path: Path, message: str | None) -> int:
    """Send a pushed/hand-written update for a project, skipping collectors.

    Args:
        project_name: The project to send the update for.
        config_path: Path to orion.toml.
        message: The update body, or None to read it from stdin.

    Returns:
        Exit code: 0 if the update was sent or the user declined; 1 on any error
        or if no delivery succeeded.

    Why:
        Intake is the structured lane in its purest form: the body IS the update,
        already audience-ready, so there is NO collector, NO LLM, and NO delta
        marker (running intake twice deliberately sends twice — it is a push, not
        a delta). This is the same entry point the Phase-6 Claude session skill
        will use, so building it now unblocks that. It still runs the full safety
        path — two redaction passes and preview-before-send — because a pushed
        body can contain a secret just as easily as a git diff can.
    """
    try:
        config = load_config(config_path)
        project = get_project(config, project_name)
        load_secrets()
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
        messages = {ch: compose(blob, ch) for ch in _channels(project)}
        if not _preview_and_confirm(messages, redaction_hits):
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
        return 0

    except (ConfigError, SecretsError) as exc:
        # Git/Summarizer/Tasks/Notes errors cannot occur on this path (no
        # collectors, no LLM), so only config/secrets setup errors are expected.
        print(f"Error: {exc}", file=sys.stderr)
        return 1


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
    messages: dict[str, str], project: ProjectConfig
) -> tuple[list[str], list[tuple[str, str]]]:
    """Send each recipient the message composed for that recipient's channel.

    Args:
        messages: Map of channel name -> composed message for that channel.
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
            send(messages[recipient.channel], url)
            sent_to.append(recipient.name)
        except (SecretsError, DeliveryError) as exc:
            # A per-recipient failure shouldn't abort the others.
            failed.append((recipient.name, str(exc)))

    for name, err in failed:
        print(f"  ✗ {name}: {err}", file=sys.stderr)
    return sent_to, failed


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


def _preview_and_confirm(messages: dict[str, str], redaction_hits: int) -> bool:
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
        print(message)
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

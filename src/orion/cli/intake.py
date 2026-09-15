# =============================================================================
# cli/intake.py
# -----------------------------------------------------------------------------
# Responsible for: The `intake` command: a hand-written update delivered through the report path,
#                  including the relay-only backdated recovery mode (absorbed relay-backfill).
# Role in project: Reuses report.py's delivery core; keeps the relay-only recovery lane explicit and
#                  separate from multi-lane delivery so neither can blur into the other.
# Assumptions: Relay-only intake never touches local report history or pending-state markers.
# =============================================================================
from __future__ import annotations


import sys
from datetime import datetime, timezone
from pathlib import Path

from orion.collectors import LANE_STRUCTURED
from orion.compose import compose
from orion.config import (
    ConfigError,
    get_project,
    load_config,
)
from orion.delivery import DeliveryError
from orion.delivery.relay import (
    push as relay_push,
)
from orion.redact import redact
from orion.report import (
    build_report,
    serialize_blob,
)
from orion.secrets import SecretsError, get_required, load_secrets
from orion.state import (
    open_state,
    record_report,
)

from .report import _channels, _deliver, _preview_and_confirm, _relay_push


def _resolve_intake_body(message: str | None, body_file: Path | None) -> str | None:
    """Resolve an intake body from --message, --body-file, or stdin (in that order).

    Args:
        message: The --message value, or None.
        body_file: The --body-file path, or None. (The caller has already rejected
            the message+body_file pairing, so at most one of these is set.)

    Returns:
        The body text, or None when --body-file could not be read (the error is
        already printed; the caller exits 1).

    Why:
        The one body-precedence rule for BOTH intake modes (CS-O PR6 contract:
        "--message/--body-file/stdin precedence identical in both modes"). Living in
        one helper is what keeps the ordinary and --relay-only lanes from drifting;
        each mode calls it at its own point so per-mode error ORDER (e.g. backfill's
        timestamp-before-body validation) is preserved.
    """
    if message is not None:
        return message
    if body_file is not None:
        try:
            return body_file.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Error: could not read --body-file {body_file}: {exc}", file=sys.stderr)
            return None
    return sys.stdin.read()


def cmd_intake(
    project_name: str,
    config_path: Path,
    message: str | None,
    assume_yes: bool,
    body_file: Path | None = None,
    relay_only: bool = False,
    generated_at: str | None = None,
) -> int:
    """Send a pushed/hand-written update for a project, skipping collectors.

    Args:
        project_name: The project to send the update for.
        config_path: Path to orion.toml.
        message: The update body, or None to read it from --body-file, else stdin.
        assume_yes: True for a non-interactive send (the `--yes` flag). When set,
            the terminal preview is skipped; otherwise it shows as usual.
        body_file: A file holding the exact body (CS-O PR6; mutually exclusive with
            `message`).
        relay_only: The recovery mode formerly `relay-backfill` (CS-O PR6): push the
            body onto the relay ONLY — no chat, no local history — at `generated_at`.
            Dispatches to _intake_relay_only; requires `generated_at`.
        generated_at: The ORIGINAL send time for a relay-only push (required exactly
            then, rejected otherwise — a backdated ordinary send would falsify chat
            history).

    Returns:
        Exit code: 0 if the update was sent or the user declined; 1 on any error
        or if no delivery succeeded; 2 for a flag-pairing usage error.

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

        The --relay-only mode boundary is a hard line: everything below the dispatch
        is the multi-lane delivery path (chat + history + fail-soft relay), and the
        relay-only recovery lane lives ENTIRELY in _intake_relay_only — so neither
        mode's failure policy can bleed into the other.
    """
    # Flag pairings are usage errors (exit 2), checked before any config/secrets
    # load — argparse cannot express either rule itself.
    if relay_only and generated_at is None:
        print(
            "Error: --relay-only requires --generated-at (the report's ORIGINAL send "
            "time — read it off the delivered message).",
            file=sys.stderr,
        )
        return 2
    if generated_at is not None and not relay_only:
        print(
            "Error: --generated-at only applies with --relay-only (a backdated "
            "ordinary send would falsify chat history).",
            file=sys.stderr,
        )
        return 2
    if message is not None and body_file is not None:
        print(
            "Error: give ONE body source — --message or --body-file, not both.",
            file=sys.stderr,
        )
        return 2

    if relay_only:
        return _intake_relay_only(
            project_name, config_path, message, body_file, generated_at, assume_yes
        )

    try:
        config = load_config(config_path)
        project = get_project(config, project_name)
        load_secrets(config_path)
        conn = open_state(config.state_db)

        # The body comes from --message, --body-file, or stdin (so a skill or shell
        # pipe can feed a summary in: `summarize | orion intake p`).
        body = _resolve_intake_body(message, body_file)
        if body is None:
            return 1
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
    """Preview a relay-only intake push (the former relay-backfill) and ask to confirm.

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
    print("PREVIEW — relay-only push to the dashboard (NOT pushed yet)")
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


def _intake_relay_only(
    project_name: str,
    config_path: Path,
    message: str | None,
    body_file: Path | None,
    generated_at: str,
    assume_yes: bool,
) -> int:
    """Push an already-sent report's content onto the relay dashboard (relay-only, chat-silent).

    Args:
        project_name: The project the report belongs to.
        config_path: Path to orion.toml.
        message: The exact report body, or None to read it from body_file, else stdin.
        body_file: A file holding the exact report body (mutually exclusive with
            `message`; cmd_intake already rejected the pairing).
        generated_at: ISO 8601 timestamp of when the report was ORIGINALLY sent (the
            user reads it off the delivered message). A naive value is treated as UTC.
        assume_yes: True to skip the preview/confirm (the content was already delivered
            once, so a knowing re-push is sufficient); redaction still runs.

    Returns:
        Exit code: 2 for a malformed --generated-at; 1 on a setup error, an empty
        body, an unreadable --body-file, or a relay push failure; 0 on a successful
        push or a user-declined preview.

    Why:
        The recovery lane, formerly the `relay-backfill` command (folded into
        `intake --relay-only` in CS-O PR6 — same semantics, new spelling). Reports
        sent to a project BEFORE its relay grant landed never reached the dashboard —
        ingest 404'd and the fail-soft _relay_push dropped them (KI-36). This takes
        the exact report content (which the user still has in Slack/Discord) and
        pushes it onto the relay's append-only history, at the original timestamp,
        WITHOUT re-delivering to any chat recipient and WITHOUT touching local
        report_history or pending-state markers. It reuses the report path's two-pass
        redaction, build_report, and the relay transport; sections=() so the relay
        renders `body` as one untitled section, exactly like an intake push.
        lane=structured because no LLM runs here — the supplied body is pushed
        verbatim. Deliberately one report per invocation (a batch/history-replay mode
        is a recorded follow-on).

        THE MODE BOUNDARY: this function is the whole --relay-only lane. Unlike the
        multi-lane path's fail-soft relay push, a failure here is fatal (exit 1) —
        landing the report on the relay IS the point — and nothing here may compose,
        deliver to chat, or write local state. Keeping the lane a separate function
        (rather than branches inside cmd_intake's delivery flow) is what stops a
        future edit from silently making it fail-soft or re-enabling chat.
    """
    try:
        config = load_config(config_path)
        project = get_project(config, project_name)
        load_secrets(config_path)
        relay_cfg = config.relay
        if not relay_cfg.enabled:
            raise ConfigError(
                f"intake --relay-only needs an enabled [relay] in {config_path} — it "
                f"pushes the report to the dashboard relay."
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

    # The body comes from --message, --body-file, or stdin — the same precedence as
    # ordinary intake, via the same helper (the modes must not drift).
    body_text = _resolve_intake_body(message, body_file)
    if body_text is None:
        return 1
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
        print(f"Pushing {project.name!r} to the relay, relay-only (preview skipped: --yes).")
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
        f"Pushed 1 relay-only report for {project.name!r} "
        f"(generated_at={generated_at_norm})."
    )
    return 0

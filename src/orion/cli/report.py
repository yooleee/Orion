# =============================================================================
# cli/report.py
# -----------------------------------------------------------------------------
# Responsible for: The `report` command and the end-to-end orchestration of one report run —
#                  the ONLY module that knows the full pipeline order — plus the delivery core
#                  (channels, preview/confirm, senders, relay push) intake reuses.
# Role in project: config -> secrets -> state -> collect -> redact -> (LLM | passthrough) -> redact
#                  -> build report -> compose -> preview/confirm -> deliver -> advance.
#                  Fail-closed: any error before delivery aborts the run and does NOT advance
#                  state, so a retry re-reports the same delta.
# Assumptions: Tests monkeypatch the sender/relay names ON THIS MODULE (cli.report.relay_push).
# =============================================================================
from __future__ import annotations


import sqlite3
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orion.collectors import LANE_RAW, LANE_STRUCTURED
from orion.collectors.git import GitError
from orion.collectors.git import collect as collect_git
from orion.collectors.incubator import IncubatorError
from orion.collectors.incubator import collect as collect_incubator
from orion.collectors.notes import NotesError
from orion.collectors.notes import collect as collect_notes
from orion.collectors.tasks import ChecklistItem, TasksError
from orion.collectors.tasks import collect as collect_tasks
from orion.collectors.tracker import TrackerError
from orion.collectors.tracker import collect as collect_tracker
from orion.compose import ComposedMessage, compose
from orion.config import (
    PUSH_ONLY_COLLECTORS,
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
from orion.delivery.relay import (
    push as relay_push,
)
from orion.delivery.slack import send as slack_send
from orion.extract import (
    AnthropicDisciplineExtractor,
    DisciplineExtractor,
)
from orion.merge import merge_sections
from orion.redact import redact
from orion.report import (
    ReportBlob,
    build_report,
    serialize_blob,
)
from orion.secrets import SecretsError, get_required, load_secrets
from orion.state import (
    get_last_report_time,
    get_marker,
    open_state,
    record_report,
    set_marker,
)
from orion.summarize import AnthropicSummarizer, LocalSummarizer, Summarizer, SummarizerError

from ._checklist import _checklist_source_files, _redacted_about, _redacted_checklist
from ._status import (
    STATUS_ABORTED,
    STATUS_FAILED,
    STATUS_NOT_DUE,
    STATUS_NO_ACTIVITY,
    STATUS_SENT,
    STATUS_SKIPPED_NOT_OPTED,
)


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
            message = messages[key]
            # One report may span several webhook POSTs (KI-2: Discord splits an
            # over-cap report instead of truncating). POST them in order; a
            # failure partway marks the recipient failed like any other, and the
            # already-delivered prefix is strictly no worse than the old
            # truncation (which delivered only a prefix on every over-cap run).
            send(message.payload, url)
            for continuation in message.continuations:
                send(continuation, url)
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

# =============================================================================
# cli/push.py
# -----------------------------------------------------------------------------
# Responsible for: The two report-less carriers: `checklist-push` (one project, --all [--due], or
#                  --watch) and `disciplines-push`, with their redaction and change-gating.
# Role in project: Pushes user-authored structured content straight to the relay dashboard with no
#                  LLM stage and no preview (the 2026-07-17 preview-scope decision).
# Assumptions: The scheduled `checklist-push --all --due` launchd job calls into here by name.
# =============================================================================
from __future__ import annotations


import hashlib
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from orion.collectors.disciplines import snapshot as snapshot_disciplines
from orion.config import (
    ConfigError,
    ProjectConfig,
    RelayConfig,
    get_project,
    load_config,
)
from orion.delivery import DeliveryError
from orion.delivery.relay import (
    push_checklist,
    push_disciplines,
)
from orion.extract import (
    Discipline,
    DisciplineExtractor,
)
from orion.redact import redact
from orion.secrets import SecretsError, get_required, load_secrets
from orion.state import (
    get_cache,
    get_last_checklist_push,
    open_state,
    record_checklist_push,
    set_cache,
)

from ._checklist import _checklist_payload, _checklist_source_files, _redacted_about
from ._status import (
    STATUS_FAILED,
    STATUS_NOT_DUE,
    STATUS_NO_CHANGE,
    STATUS_NO_CHECKLIST,
    STATUS_PUSHED,
)
from .report import _build_extractor, _is_due_at


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
        and the relay push credential. E1.3 adds `--all [--due]` so one scheduled entry can
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

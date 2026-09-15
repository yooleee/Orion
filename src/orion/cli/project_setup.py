# =============================================================================
# cli/project_setup.py
# -----------------------------------------------------------------------------
# Responsible for: Registering a project (`add-project`, with the optional relay grant and the
#                  seeded starter checklist) and installing its git hook (`install-hook`).
# Role in project: The only commands that WRITE the config or the repo's hooks directory; both
#                  preview and confirm before writing.
# Assumptions: Config writes append a stanza; they never rewrite what the user authored.
# =============================================================================
from __future__ import annotations


import re
import subprocess
import sys
from pathlib import Path

from orion.collectors._markdown import Table, parse_tables
from orion.collectors.git import GitError
from orion.config import (
    ConfigError,
    Recipient,
    get_project,
    load_config,
    load_relay_config,
)
from orion.delivery.relay import (
    grant_projects as relay_grant_projects,
)
from orion.hooks import build_hook_script, resolve_hooks_dir
from orion.scaffold import parse_recipient_spec, render_project_stanza

from .relay_admin import _ADMIN_CALL_FAILED, _run_admin_command


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


def _grant_new_project_scope(
    project_name: str, grant: str | None, config_path: Path, assume_yes: bool
) -> int:
    """Grant (or offer to grant) a relay account push scope for a just-registered project.

    Args:
        project_name: The project that was just registered.
        grant: The account named by --grant, or None (which may open the opt-in prompt).
        config_path: Path to orion.toml (locates the relay config + admin secret).
        assume_yes: The registration's --yes flag; True suppresses the prompt entirely.

    Returns:
        Exit code for the whole add: 0 when nothing was requested or the grant
        succeeded; 1 when a REQUESTED grant (flag or answered prompt) failed. The
        registration is never rolled back — the failure message says so and gives the
        manual command.

    Why:
        The KI-36 forward-fix. A new project's first reports can push under a
        contributor key with no grant, 404 fail-soft, and silently vanish from the
        dashboard record; the cheapest prevention is closing the gap at add-project
        time. Everything here is opt-in by kickoff contract: the flag is explicit, the
        prompt appears only on an interactive run (TTY, no --yes) with a
        provisioning-configured relay, and every other path is at most one printed
        hint — scripted/automated behavior must not change. The relay probe swallows
        ConfigError deliberately: this step may never introduce a new failure mode
        into a command that just succeeded.
    """
    # Probe the relay config without raising: no relay (or a broken one) simply means
    # none of this applies — add-project's own success has already been reported.
    try:
        relay_cfg = load_relay_config(config_path)
    except ConfigError:
        return 0
    if not relay_cfg.enabled:
        return 0
    admin_available = bool(relay_cfg.admin_token_env_var)

    account = grant
    if account is None and admin_available and not assume_yes and sys.stdin.isatty():
        # Interactive opt-in (default No). Blank account = changed their mind.
        if _confirm(
            f"Also grant a relay account push scope for {project_name!r} now? [y/N] "
        ):
            account = input("Account name (blank to skip): ").strip() or None

    if account is not None:
        result = _run_admin_command(
            config_path,
            lambda url, token: relay_grant_projects(url, token, account, [project_name]),
        )
        if result is _ADMIN_CALL_FAILED:
            print(
                f"The project is registered; the grant did not happen. Grant later "
                f"with: orion relay-user grant {account} --project {project_name}",
                file=sys.stderr,
            )
            return 1
        scope = result.get("projects") or []
        print(f"  Granted {account!r} push scope for {project_name!r}.")
        print(f"    Scope is now: {', '.join(scope) if scope else '(none)'}")
        return 0

    # No grant requested or taken: leave the pointer so the gap is at least visible
    # (the DF2 finding — next-steps never mentioned the relay).
    print(
        f"  Also: grant your push account scope for it — "
        f"orion relay-user grant <account> --project {project_name}"
    )
    return 0


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
    grant: str | None = None,
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
        grant: A relay account to grant push scope for the new project after
            registration (the KI-36 forward-fix), or None. Without it, an interactive
            run may offer the same grant as an opt-in prompt — scripted runs (--yes,
            redirected stdin) are never prompted. Incompatible with print_only
            (nothing is registered, so nothing can be granted).

    Returns:
        Exit code: 0 on success or a declined preview; 1 on any error (including a
        REQUESTED grant that failed — the registration itself stands either way).

    Why:
        This is Orion's ONLY config writer, added to kill the onboarding friction
        the dogfood surfaced (no way to register a project from its own directory).
        It keeps the invariant's spirit — config is never written as a side effect
        of a run — by being explicit, preview-gated, and append-only (it never
        rewrites existing content, so hand-written comments and ordering survive).
        The validating/rendering lives in scaffold.py; this function owns inference
        and file I/O, mirroring the cmd_install_hook / build_hook_script split.
    """
    # --grant acts on the just-registered project; --print registers nothing, so the
    # pairing is a contradiction worth refusing up front rather than silently ignoring.
    if print_only and grant is not None:
        print(
            "Error: --grant has nothing to act on with --print (nothing is registered). "
            "Drop --print to register and grant.",
            file=sys.stderr,
        )
        return 1

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
    # KI-36 forward-fix: offer to close the scoping gap right where it opens. The
    # helper owns the flag/prompt/hint gating so scripted runs stay untouched.
    return _grant_new_project_scope(project_name, grant, config_path, assume_yes)

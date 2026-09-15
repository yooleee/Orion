# =============================================================================
# cli/_parser.py
# -----------------------------------------------------------------------------
# Responsible for: Declaring the whole `orion` command surface: every subparser, group and flag
#                  (build_parser), plus the shared --config argument helpers.
# Role in project: Pure argparse — no command logic. main() builds the parser here and dispatches;
#                  scripts/help_snapshot.py renders it for the surface pin.
# Assumptions: Adding or renaming a command is a deliberate edit here (no registry).
# =============================================================================
from __future__ import annotations


import argparse

from orion.config import (
    SHARE_LEVELS,
    RelayServeSettings,
)
from orion.hooks import SUPPORTED_HOOKS


def build_parser(default_config: str) -> argparse.ArgumentParser:
    """Build the complete `orion` argument parser: every command, group and flag.

    Args:
        default_config: The --config default shown in help and used when the flag is
            omitted ($ORION_CONFIG when set, else DEFAULT_CONFIG; main() resolves it).

    Returns:
        The fully-declared ArgumentParser, ready for parse_args.

    Why:
        Declaring the surface is a different job from running it (CS-O PR10 split this
        out of main() verbatim). One function owning every subparser keeps the command
        tree readable top to bottom — the same order the --help snapshot renders it — and
        lets scripts/help_snapshot.py and the tests build the parser without dispatching.
        The command tree is hand-kept on purpose: adding a command is a deliberate edit
        here, never a registry lookup (the plugin system the project declined).
    """
    parser = argparse.ArgumentParser(
        prog="orion",
        description="Turn local git activity into supervisor-ready progress updates.",
        epilog=(
            "Every command takes --config PATH (default: orion.toml in the working "
            "directory). Set ORION_CONFIG=/abs/path/orion.toml once in your environment to "
            "make that the default everywhere — hooks, schedulers and the session skill "
            "then need no --config; a --config flag still wins for that run."
        ),
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
    _add_config_arg(report_parser, default_config)
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
    _add_config_arg(checklist_parser, default_config)
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
    _add_config_arg(disciplines_parser, default_config)
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
    _add_config_arg(intake_parser, default_config)
    intake_parser.add_argument(
        "--message",
        "-m",
        default=None,
        help=(
            "The update body. If omitted, the body comes from --body-file, else stdin. "
            "Giving both --message and --body-file is an error."
        ),
    )
    intake_parser.add_argument(
        "--body-file",
        default=None,
        help="Path to a file holding the exact update body (alternative to --message/stdin).",
    )
    intake_parser.add_argument(
        "--relay-only",
        dest="relay_only",
        action="store_true",
        help=(
            "Recovery mode (was `relay-backfill`): push the body onto the relay "
            "dashboard ONLY — no chat delivery, no local history — at the ORIGINAL "
            "send time. Requires --generated-at; a relay failure is fatal."
        ),
    )
    intake_parser.add_argument(
        "--generated-at",
        default=None,
        help=(
            "With --relay-only (required there, rejected elsewhere): ISO 8601 timestamp "
            "of when the report was ORIGINALLY sent (read it off the delivered message). "
            "Sets the card's time on the dashboard; a naive value is treated as UTC."
        ),
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
    _add_config_arg(hook_parser, default_config)
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

    # add-project's flags live on a parent parser (the KI-43 fix). Its second child,
    # graduate-idea, was removed in CS-O PR5 — the parent stays as the registration seam
    # a future second registration command would re-attach to.
    registration_flags = _project_registration_parser(default_config)

    # The ONE config-writing command. It is explicit, append-only, and previews
    # before writing — so the "config is never written as a side effect of a run"
    # invariant holds (see config.py header).
    add_parser = subparsers.add_parser(
        "add-project",
        parents=[registration_flags],
        help="Register a new project in orion.toml (the only command that writes config).",
    )
    add_parser.add_argument(
        "name",
        nargs="?",
        default=None,
        help="Project name (default: the repo directory's name).",
    )
    add_parser.add_argument(
        "--incubator-file",
        dest="incubator_file",
        default=None,
        help="Path to the incubator index.md (required if 'incubator' is in --collectors).",
    )

    # Read-only inspect commands (B6). They print config; they never write it.
    projects_parser = subparsers.add_parser(
        "projects",
        help=(
            "List the projects defined in the config, or show ONE project's resolved "
            "config when a name is given (read-only; absorbs the former `show`)."
        ),
    )
    projects_parser.add_argument(
        "project",
        nargs="?",
        default=None,
        help="A project name for the detail view (omit to list every project).",
    )
    _add_config_arg(projects_parser, default_config)

    check_parser = subparsers.add_parser(
        "check",
        help="Validate the config and report send-readiness (read-only).",
    )
    _add_config_arg(check_parser, default_config)

    status_parser = subparsers.add_parser(
        "status",
        help="Show which projects have unreported activity across the config (read-only).",
    )
    _add_config_arg(status_parser, default_config)

    baseline_parser = subparsers.add_parser(
        "baseline",
        help=(
            "Mark a project's current state as already-reported WITHOUT sending, so the "
            "first real report covers only new activity (skips a giant first report)."
        ),
    )
    baseline_parser.add_argument("project", help="Project name as defined in orion.toml.")
    _add_config_arg(baseline_parser, default_config)

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
    _add_config_arg(disc_pull, default_config)
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
    _add_config_arg(disc_reply, default_config)


    # `relay-serve` settings resolve flag > [relay.serve] in orion.toml > default (CS-O PR8).
    # Every SETTING flag defaults to argparse.SUPPRESS: an omitted flag then leaves no
    # attribute on the namespace at all, which is what lets the dispatcher tell "not given"
    # from "given as the default value" and hand only the flags actually typed to the
    # resolver (a parser default would erase that distinction and always win over config).
    # The canonical defaults live on RelayServeSettings (config.py); the help quotes them so
    # each default is written once. --allow-legacy-admin and --config are NOT settings.
    _rs = RelayServeSettings()
    relay_parser = subparsers.add_parser(
        "relay-serve",
        help="Run the local relay: receive pushed reports and serve a read-only dashboard.",
        description=(
            "Run the relay: receive pushed reports and serve the dashboard. Every setting "
            "below can also live in orion.toml under [relay.serve] (same names, snake_case); "
            "a flag given here overrides the file, and the file overrides the default."
        ),
    )
    relay_parser.add_argument(
        "--host",
        default=argparse.SUPPRESS,
        help=f"Interface to bind (default: {_rs.host} — loopback only, not world-reachable).",
    )
    relay_parser.add_argument(
        "--port",
        type=int,
        default=argparse.SUPPRESS,
        help=f"Port to bind (default: {_rs.port}).",
    )
    relay_parser.add_argument(
        "--db",
        default=argparse.SUPPRESS,
        help=(
            f"Path to the relay's own sqlite store (default: {_rs.db}). A relative flag "
            "value is taken from the working directory; a relative [relay.serve] db is "
            "taken from beside orion.toml."
        ),
    )
    relay_parser.add_argument(
        "--view-token-env",
        default=argparse.SUPPRESS,
        help=(
            "Name of the .env variable holding the dashboard view secret — the "
            "bootstrap-admin login key and the non-loopback bind guard (default: "
            f"{_rs.view_token_env}). REQUIRED when --host is non-loopback; optional on "
            "loopback (reads stay open)."
        ),
    )
    relay_parser.add_argument(
        "--require-view-auth",
        dest="require_view_auth",
        action="store_true",
        default=argparse.SUPPRESS,
        help=(
            "Demand the dashboard view secret even on a loopback bind. Use this when "
            "the relay runs on loopback behind a reverse proxy (the proxy exposes it, "
            "so 'loopback' isn't actually private) — a forgotten secret then fails "
            "closed instead of serving an open dashboard. See docs/deployment.md."
        ),
    )
    relay_parser.add_argument(
        "--no-require-view-auth",
        dest="require_view_auth",
        action="store_false",
        default=argparse.SUPPRESS,
        help="Turn --require-view-auth off for this run even if [relay.serve] sets it.",
    )
    relay_parser.add_argument(
        "--timezone",
        default=argparse.SUPPRESS,
        help=(
            f"IANA zone the dashboard renders timestamps in (default: {_rs.timezone}). "
            'Examples: --timezone UTC, --timezone "Europe/London". An unknown zone is '
            "rejected at startup."
        ),
    )
    relay_parser.add_argument(
        "--session-days",
        type=int,
        default=argparse.SUPPRESS,
        help=f"Dashboard login session length in days (default: {_rs.session_days}).",
    )
    relay_parser.add_argument(
        "--allow-legacy-admin",
        action="store_true",
        help=(
            "Keep the legacy shared view key usable as an admin login even after "
            "per-user accounts exist (default: off — it is bootstrap-only, usable "
            "only while no users have been provisioned). Flag-only on purpose: it cannot "
            "be set in [relay.serve], so a bootstrap exception never outlives the run "
            "that needed it."
        ),
    )
    relay_parser.add_argument(
        "--init-secrets",
        action="store_true",
        help=(
            "Generate any MISSING relay secrets into the .env beside --config, then exit "
            "without serving. Always: ORION_RELAY_USER_PEPPER, ORION_RELAY_SESSION_KEY, "
            "ORION_RELAY_ADMIN_TOKEN; the view token too when the resolved settings bind "
            "beyond loopback or set --require-view-auth. Never overwrites a non-empty value, "
            "never prints a value, keeps the rest of the file byte-for-byte."
        ),
    )
    relay_parser.add_argument(
        "--web-dir",
        default=argparse.SUPPRESS,
        help=(
            "Path to the built SPA assets (e.g. web/dist) to serve single-host: the relay "
            "then serves the React dashboard (static assets + an index.html fallback). "
            "Omit to run API-only (no front-end). Same relative-path rule as --db."
        ),
    )
    relay_parser.add_argument(
        "--showcase",
        dest="showcase",
        action="store_true",
        default=argparse.SUPPRESS,
        help=(
            "Enable the public, no-login Showcase surface (default: off). When on, "
            "GET /api/showcase serves ONLY the projects named with --showcase-project; "
            "while off it 404s and the dashboard hides the Public showcase link."
        ),
    )
    relay_parser.add_argument(
        "--no-showcase",
        dest="showcase",
        action="store_false",
        default=argparse.SUPPRESS,
        help="Take the Showcase offline for this run even if [relay.serve] enables it.",
    )
    relay_parser.add_argument(
        "--showcase-project",
        action="append",
        default=argparse.SUPPRESS,
        dest="showcase_projects",
        metavar='NAME[:"blurb"]',
        help=(
            "Add a project to the public Showcase allowlist (repeatable; order is the "
            'display order). Optionally append a curated one-line blurb after a colon, '
            'e.g. --showcase-project \'orion:A local-first tracker that observes & '
            "reframes'. Without a blurb the project's latest report headline is shown. "
            "Only effective with --showcase. Given at all, these REPLACE the whole "
            "[relay.serve] showcase_projects list (never append to it)."
        ),
    )
    relay_parser.add_argument(
        "--config",
        default=default_config,
        help=(
            f"Path to orion.toml (default: {default_config}; or set $ORION_CONFIG). Locates "
            "the sibling .env that holds the relay's secrets (and that --init-secrets writes) "
            "and the optional [relay.serve] settings table; a missing file simply means "
            "flags + defaults."
        ),
    )
    # `relay-user` is a command GROUP with add/list/deactivate/... subcommands — the admin-side
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
        "--key-only",
        action="store_true",
        dest="key_only",
        help=(
            "Scripting mode: print ONLY the new access key (plus a newline) on stdout — "
            "no provisioning summary. Errors still go to stderr."
        ),
    )
    _add_config_arg(ru_add, default_config)

    ru_list = relay_user_subs.add_parser(
        "list", help="List the relay's users (no credential material is shown)."
    )
    _add_config_arg(ru_list, default_config)

    ru_deactivate = relay_user_subs.add_parser(
        "deactivate",
        help=(
            "Deactivate an account: its keys stop authenticating and any live session "
            "is logged out. Keeps the name (use `delete` to free it)."
        ),
    )
    ru_deactivate.add_argument("name", help="The user to deactivate (by name).")
    _add_config_arg(ru_deactivate, default_config)

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
    _add_config_arg(ru_grant, default_config)

    ru_ungrant = relay_user_subs.add_parser(
        "ungrant",
        help="Remove one or more projects from an existing user's scope (grant's inverse).",
    )
    ru_ungrant.add_argument("name", help="The user whose scope to narrow (by name).")
    ru_ungrant.add_argument(
        "--project",
        action="append",
        default=[],
        dest="projects",
        metavar="PROJECT",
        help="A project to ungrant (repeatable: --project a --project b). At least one required.",
    )
    _add_config_arg(ru_ungrant, default_config)

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
        "--label", default="key",
        help=(
            "A short label for where this key lives, e.g. 'mac' or 'wsl2' (default: "
            "'key'). Labels must be unique among an account's ACTIVE keys — adding a "
            "second key without --label is rejected by the relay (409), so name each "
            "additional key."
        ),
    )
    ru_key_add.add_argument(
        "--key-only",
        action="store_true",
        dest="key_only",
        help=(
            "Scripting mode: print ONLY the new access key (plus a newline) on stdout — "
            "no summary or next-steps. Errors still go to stderr."
        ),
    )
    _add_config_arg(ru_key_add, default_config)

    ru_key_list = ru_key_subs.add_parser(
        "list", help="List an account's credentials (never shows the key material itself)."
    )
    ru_key_list.add_argument("name", help="The account whose credentials to list.")
    _add_config_arg(ru_key_list, default_config)

    ru_key_revoke = ru_key_subs.add_parser(
        "revoke", help="Revoke ONE credential by id; the account's other keys keep working."
    )
    ru_key_revoke.add_argument("name", help="The account the credential belongs to.")
    ru_key_revoke.add_argument(
        "--id", required=True, type=int,
        help="The credential id to revoke (from `key list` — labels are reusable, ids are not).",
    )
    _add_config_arg(ru_key_revoke, default_config)

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
    _add_config_arg(ru_pw_set, default_config)

    ru_pw_unlock = ru_pw_subs.add_parser(
        "unlock",
        help="Clear a login lockout after failed attempts (does not change the password).",
    )
    ru_pw_unlock.add_argument("name", help="The account to unlock.")
    _add_config_arg(ru_pw_unlock, default_config)

    ru_role = relay_user_subs.add_parser(
        "role",
        help="Change an account's role (logs out live sessions; a scoped role needs grants).",
    )
    ru_role.add_argument("name", help="The account whose role to change.")
    ru_role.add_argument(
        "role",
        help="The new role: admin | viewer | supervisor | member | contributor.",
    )
    _add_config_arg(ru_role, default_config)

    ru_rename = relay_user_subs.add_parser(
        "rename",
        help="Rename an account (already-recorded history keeps the name it was written with).",
    )
    ru_rename.add_argument("name", help="The account to rename.")
    ru_rename.add_argument("new_name", help="The new (unique) name.")
    _add_config_arg(ru_rename, default_config)

    ru_set_operator = relay_user_subs.add_parser(
        "set-operator",
        help="Repoint an agent account at a different operating human.",
    )
    ru_set_operator.add_argument("name", help="The agent account to reassign.")
    ru_set_operator.add_argument(
        "operator", help="The human account this agent should act on behalf of."
    )
    _add_config_arg(ru_set_operator, default_config)

    ru_delete = relay_user_subs.add_parser(
        "delete",
        help="Hard-delete a user, freeing their name to be reused (deactivate keeps the name).",
    )
    ru_delete.add_argument("name", help="The user to delete (by name).")
    _add_config_arg(ru_delete, default_config)

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
    _add_config_arg(rp_visibility, default_config)

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
    _add_config_arg(rp_lifecycle, default_config)
    return parser


DEFAULT_CONFIG = "orion.toml"


def _add_config_arg(parser: argparse.ArgumentParser, default_config: str) -> None:
    """Add the standard `--config` flag to one subparser (AU1-R P3).

    Args:
        parser: The subparser to add the flag to.
        default_config: The config path to show as the default (resolved in main() from
            $ORION_CONFIG or DEFAULT_CONFIG).

    Returns:
        None. Mutates `parser`, matching argparse's own add_argument style.

    Why:
        Thirty subparsers declared a byte-identical four-line `--config` block (AU1's 31×
        finding, one of which is not identical — see below). One definition means the flag's
        default and its help text cannot drift between commands, which is the drift a reader
        cannot see because the copies are hundreds of lines apart.

        TWO deliberate non-users, both of which would be bugs to "fix":

        1. `add-project` gets `--config` from `_project_registration_parser` via
           `parents=[...]`. Calling this helper on it as well would raise
           `argparse.ArgumentError: conflicting option string: --config` at parser-BUILD
           time — i.e. on every single invocation of the CLI, not just add-project's.
           This helper is called on the shared parent instead, which is where it belongs.
        2. `relay-serve` keeps its own `--config` with different help text ("used only to
           locate .env"). That is an accurate statement about that command specifically —
           relay-serve reads no project list — so it is a real distinction, not a copy that
           drifted, and collapsing it into the standard wording would make the help WRONG.
    """
    parser.add_argument(
        "--config",
        default=default_config,
        help=f"Path to the config file (default: {default_config}; or set $ORION_CONFIG).",
    )


def _project_registration_parser(default_config: str) -> argparse.ArgumentParser:
    """Build the parent parser holding `add-project`'s registration flags.

    Args:
        default_config: The config path to show as the `--config` default (resolved in
            main() from $ORION_CONFIG or DEFAULT_CONFIG).

    Returns:
        An ArgumentParser with `add_help=False`, meant only to be passed as
        `parents=[...]` to the two real subparsers — never parsed on its own.

    Why:
        Born as the KI-43 fix: `graduate-idea` was built by copying `add-project`'s flags,
        the copy drifted, and one shared parent meant a flag could only be added to both at
        once. `graduate-idea` was removed in CS-O PR5 (decision 8), so this parent now has
        ONE child — it stays deliberately as the registration seam: a future second
        registration command re-attaches here instead of re-copying the flag list, which is
        exactly the drift class KI-43 recorded. `--incubator-file` stays declared on
        `add-project` itself (historically the two commands gave the same option string
        different meanings, so the parent never owned it).

        Note for the later cli.py DRY pass: `--config` lives here for these two commands, so
        a repo-wide `add_config_arg(parser)` helper must skip them or it will collide.
    """
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--repo-path",
        dest="repo_path",
        default=None,
        help="Path to the git repo (default: the current repo's top level, else cwd).",
    )
    shared.add_argument(
        "--like",
        default=None,
        metavar="PROJECT",
        help="Copy recipients from this existing project (combine with or use instead of --recipient).",
    )
    shared.add_argument(
        "--recipient",
        dest="recipients",
        action="append",
        default=[],
        metavar='"Name:channel:ENV_VAR"',
        help='Add a recipient (repeatable). Channel is "discord" or "slack"; the last field NAMES a .env variable.',
    )
    shared.add_argument(
        "--share-level",
        dest="share_level",
        choices=SHARE_LEVELS,
        default="high_level",
        help="How much git detail to expose (default: high_level — no code diff).",
    )
    shared.add_argument(
        "--collectors",
        default="git",
        help="Comma-separated signals to enable (default: git). Any of: git,tasks,notes,incubator,tracker.",
    )
    shared.add_argument(
        "--tasks-file",
        dest="tasks_file",
        default=None,
        help="Path to the tasks checklist. If 'tasks' is enabled and this is omitted, "
        "defaults to <repo>/TODO.md and creates a starter checklist there.",
    )
    shared.add_argument(
        "--notes-file",
        dest="notes_file",
        default=None,
        help="Path to the notes file (required if 'notes' is in --collectors).",
    )
    shared.add_argument(
        "--tracker-file",
        dest="tracker_file",
        default=None,
        help="Path to the status-aware tracker doc (required if 'tracker' is in --collectors).",
    )
    shared.add_argument(
        "--seed-tasks-from",
        dest="seed_tasks_from",
        default=None,
        metavar="DOC",
        help="When a tasks_file is being created (no --tasks-file given), seed its "
        "checklist from this doc's Markdown tables instead of an empty starter.",
    )
    shared.add_argument(
        "--grant",
        default=None,
        metavar="ACCOUNT",
        help="After registering, grant this relay account push scope for the new "
        "project (KI-36 forward-fix). Needs [relay] admin_token_env_var in the config "
        "and the admin token in .env. Without it, an interactive run offers a prompt.",
    )
    _add_config_arg(shared, default_config)
    shared.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="Print the stanza that would be written, and write nothing (review first).",
    )
    shared.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Write without the preview confirmation (for non-interactive callers).",
    )
    return shared

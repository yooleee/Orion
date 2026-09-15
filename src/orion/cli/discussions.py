# =============================================================================
# cli/discussions.py
# -----------------------------------------------------------------------------
# Responsible for: The developer's half of the supervisor discussion loop: `discussions pull`
#                  (Bearer read with a local watermark) and `discussions reply`.
# Role in project: The machine sibling of the dashboard's discussion thread; identity is server-
#                  derived from the contributor key, so --as is a label the relay may ignore.
# Assumptions: Needs an enabled [relay] and this machine's contributor key in .env.
# =============================================================================
from __future__ import annotations


import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from orion.config import (
    ConfigError,
    get_project,
    load_config,
)
from orion.delivery import DeliveryError
from orion.delivery.relay import (
    post_discussion,
    pull_discussions,
)
from orion.secrets import SecretsError, get_required, load_secrets
from orion.state import (
    get_discussion_watermark,
    open_state,
    set_discussion_watermark,
)


def _format_display_time(iso: str, display_timezone: str) -> str:
    """Render a stored UTC ISO-8601 timestamp as human wall-clock time in a chosen zone.

    Args:
        iso: An ISO-8601 timestamp with an offset (as the relay stores it, e.g.
            "2026-06-19T19:30:00+00:00").
        display_timezone: The IANA zone name to render in — `config.display_timezone`,
            already validated at config load (see config._parse_display_timezone).

    Returns:
        A human string in that zone, e.g. "2026-06-19 12:30 PDT" for America/Los_Angeles
        or "2026-06-19 19:30 UTC" for UTC.

    Why:
        The zone is a CONFIGURED choice (KI-20), not a property of this command. Every
        delivered message already honors `display_timezone`; this listing used to hardcode
        America/Los_Angeles, so a user who set the knob got their zone everywhere except
        here — the one surface that reads back what a supervisor wrote (AU1-R P4).

        ZoneInfo is internally cached, so constructing it per call is cheap; doing it HERE
        rather than at module import keeps a missing tzdata from breaking every other
        command — only the `discussions` listing depends on it. This stays its own small
        formatter rather than a shared one: orion/ shares no code with relay/, the same
        independence the duplicated busy-timeout constant reflects.
    """
    # fromisoformat parses the stored "+00:00" offset; astimezone converts that absolute
    # instant to wall-clock time in the configured zone (zoneinfo applies DST -> PDT/PST).
    local = datetime.fromisoformat(iso).astimezone(ZoneInfo(display_timezone))
    return local.strftime("%Y-%m-%d %H:%M %Z")


def cmd_discussions_pull(
    project_name: str, config_path: Path, *, as_json: bool, show_all: bool
) -> int:
    """Pull new supervisor messages on a project's discussion thread (E2 Inc 5).

    Args:
        project_name: The project whose thread to pull.
        config_path: Path to orion.toml.
        as_json: When True, print the raw JSON response; else a human-readable listing.
        show_all: When True, show the WHOLE thread and do NOT advance the unread marker;
            when False (default), show only items newer than the watermark and advance it.

    Returns:
        Exit code: 0 on a successful pull (including "nothing new"); 1 on a config/secrets
        error, a disabled relay, or a failed pull.

    Why:
        The developer's read half of the supervisor-interaction loop. The pull is BY
        PROJECT, Bearer-authed with the contributor key, and the unread cursor is a LOCAL
        watermark (the relay stays append-only). pull_discussions is the module-global so
        a test can monkeypatch it, mirroring relay_push.
    """
    try:
        config = load_config(config_path)
        project = get_project(config, project_name)
        load_secrets(config_path)

        relay_cfg = config.relay
        if not relay_cfg.enabled:
            print(
                f"Error: cannot pull discussions for {project.name!r} — no relay is "
                f"enabled in {config_path}. The thread lives on the relay you push reports "
                f"to; enable the [relay] table to read it.",
                file=sys.stderr,
            )
            return 1

        token = get_required(relay_cfg.token_env_var)

        conn = open_state(config.state_db)
        # since_id is the unread cursor: 0 for --all (whole thread), else the stored
        # watermark (only what's newer). Keyed by (project, relay_url).
        since_id = (
            0 if show_all else get_discussion_watermark(conn, project.name, relay_cfg.url)
        )
        response = pull_discussions(relay_cfg.url, token, project.name, since_id)
    except (ConfigError, SecretsError, DeliveryError) as exc:
        # User-fixable (a config typo, a missing token, a down relay). Never advance the
        # watermark on a failed pull.
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    items = response.get("discussions", [])
    latest_id = response.get("latest_id", since_id)

    if as_json:
        print(json.dumps(response))
    else:
        _print_discussions(items, project.name, show_all, config.display_timezone)

    # Advance the watermark ONLY on a normal run — --all is an explicit re-read. Advancing
    # to latest_id is idempotent (it echoes since_id when nothing is new).
    if not show_all:
        pulled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        set_discussion_watermark(conn, project.name, relay_cfg.url, latest_id, pulled_at)

    return 0


def cmd_discussions_reply(
    project_name: str, body: str, author: str, config_path: Path
) -> int:
    """Post a developer reply to a project's discussion thread (E2 Inc 5).

    Args:
        project_name: The project whose thread to reply on.
        body: The reply text.
        author: The display name for this reply (the `--as` value), or "" to let the relay
            stamp its default "developer" label.
        config_path: Path to orion.toml.

    Returns:
        Exit code: 0 when the reply is posted; 1 on a config/secrets error, a disabled
        relay, or a failed post.

    Why:
        The developer's write half of the loop, closing it from the terminal without the
        dashboard. The reply lands as role="developer" server-side (the Bearer token IS the
        developer's authority), so `--as` only sets the display name, never the role.
        post_discussion is the module-global so a test can monkeypatch it.
    """
    try:
        config = load_config(config_path)
        project = get_project(config, project_name)
        load_secrets(config_path)

        relay_cfg = config.relay
        if not relay_cfg.enabled:
            print(
                f"Error: cannot reply on {project.name!r} — no relay is enabled in "
                f"{config_path}. Enable the [relay] table to post to the thread.",
                file=sys.stderr,
            )
            return 1

        token = get_required(relay_cfg.token_env_var)
        result = post_discussion(relay_cfg.url, token, project.name, body, author)
    except (ConfigError, SecretsError, DeliveryError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # The relay echoes the STORED author name (authoritative): an identified producer's own
    # name, or — on the legacy anonymous path — the supplied label or the "developer"
    # fallback. Prefer it so the line reflects what was actually recorded.
    shown = result.get("author") or author or "developer"
    print(f"Reply posted to {project.name!r} as {shown!r} (id {result.get('id')}).")
    # Honesty: if a --as name was given but the relay recorded a different one, the key is an
    # identified producer's and the label was ignored (identity is server-derived, not asserted).
    if author and shown != author:
        print(
            f"  Note: --as {author!r} was ignored — this key is an identified producer, "
            f"so the reply is attributed to {shown!r}."
        )
    return 0


def _print_discussions(
    items: list[dict], project_name: str, show_all: bool, display_timezone: str
) -> None:
    """Print a project's pulled discussion items as a human-readable listing.

    Args:
        items: The item dicts from pull_discussions (role, author_name, body, created_at).
        project_name: The project the thread belongs to (for the header/empty line).
        show_all: Whether this was an --all pull, which only changes the empty-state wording.
        display_timezone: The configured IANA zone to render timestamps in (KI-20), passed
            down from the caller's loaded config rather than read here — this function does
            no I/O, which is what keeps it trivially testable.

    Returns:
        None. Writes to stdout.

    Why:
        The default human-facing output: one line per item, leading with the [role] tag so
        the developer can tell a supervisor turn from their own at a glance. An empty result
        is a friendly one-liner, with wording that distinguishes
        "no messages at all" (--all) from "nothing new since last pull" (default).
    """
    if not items:
        qualifier = "" if show_all else " new"
        print(f"No{qualifier} discussion messages for {project_name!r}.")
        return

    print(f"{len(items)} discussion message(s) for {project_name!r}:")
    for item in items:
        # role tags the turn (supervisor/developer); author_name is server-derived.
        print(
            f"  [{item['role']}] {item['author_name']} · "
            f"{_format_display_time(item['created_at'], display_timezone)} · {item['body']}"
        )

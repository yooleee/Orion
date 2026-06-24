# =============================================================================
# scaffold.py
# -----------------------------------------------------------------------------
# Responsible for: Building the TOML text for a NEW project stanza (and parsing
#                  a CLI "--recipient" spec into a Recipient). Pure string work —
#                  no file I/O, no reading the existing config.
# Role in project: The write-side counterpart to config.py's read side. The
#                  `orion add-project` command (cli.cmd_add_project) calls these
#                  to render a stanza, then does the actual file read/append/write
#                  and re-validates by loading the result. This mirrors how
#                  hooks.build_hook_script is a pure builder and cli.cmd_install_hook
#                  does the I/O.
# Assumptions: Inputs are rendered as TOML *basic strings*; values that would need
#              escaping (a literal '"' or '\' or a control char) are rejected with
#              a ConfigError rather than escaped — we never hand-roll a TOML escaper
#              (explicit over clever). Project names are restricted to a safe bare
#              key so no key-quoting is needed either.
# =============================================================================

from __future__ import annotations

import re
from pathlib import Path

# Reuse config.py's single sources of truth so validation can never drift between
# the read side (loading orion.toml) and the write side (creating a stanza).
from .config import (
    COLLECTOR_FILE_KEYS,
    DEFAULT_STATE_DB,
    SHARE_LEVELS,
    SUPPORTED_CHANNELS,
    SUPPORTED_COLLECTORS,
    ConfigError,
    Recipient,
    _validate_env_var_name,
)

# A project name becomes a TOML *bare key* (`[projects.<name>]`). Restricting it to
# letters, digits, underscore and hyphen means we never have to quote or escape the
# key, and the name stays a clean `orion report <name>` argument. Anything else
# (spaces, dots, slashes) is rejected so the user picks an explicit, safe name.
PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def parse_recipient_spec(spec: str) -> Recipient:
    """Parse a CLI ``"Name:channel:ENV_VAR"`` string into a validated Recipient.

    Args:
        spec: A single ``--recipient`` value, three colon-separated fields:
            the human name, the channel ("discord"/"slack"), and the NAME of the
            .env variable holding the webhook URL (never the URL itself).

    Returns:
        A validated Recipient.

    Why:
        This is the explicit, new-user / override path for supplying recipients on
        the command line (the copy-from-existing path is `--like`, handled in the
        CLI). We split on a fixed three-field shape rather than something cleverer
        because it is predictable and easy to document; a name containing a colon
        is rejected with a clear message instead of being silently mis-split.
    """
    parts = spec.split(":")
    if len(parts) != 3:
        raise ConfigError(
            f"recipient {spec!r} is not in the form \"Name:channel:ENV_VAR\" "
            f"(exactly three colon-separated fields; a name may not contain ':'). "
            f'Example: "Alex (supervisor):discord:ORION_DISCORD_WEBHOOK_ALEX".'
        )

    name, channel, webhook_env_var = (p.strip() for p in parts)
    where = f"recipient {spec!r}"

    if not name:
        raise ConfigError(f"{where} is missing a non-empty name.")
    if channel not in SUPPORTED_CHANNELS:
        raise ConfigError(
            f"{where} has invalid channel={channel!r}. Supported now: {SUPPORTED_CHANNELS}."
        )
    if not webhook_env_var:
        raise ConfigError(f"{where} is missing a non-empty webhook env-var name.")
    # Reuse the loader's check so "pasted the URL where the var name goes" fails the
    # same way here as it would on load (and never echoes a pasted secret).
    _validate_env_var_name(webhook_env_var, "webhook_env_var", where)

    return Recipient(name=name, channel=channel, webhook_env_var=webhook_env_var)


def _toml_basic_string(value: str, field: str, where: str) -> str:
    """Render a string as a TOML basic string, rejecting anything needing escaping.

    Args:
        value: The raw string to embed in the TOML.
        field: The field name, for the error message (e.g. "repo_path").
        where: A locating string for the error message (e.g. "project 'demo'").

    Returns:
        The value wrapped in double quotes, ready to drop into TOML.

    Why:
        stdlib has no TOML *writer*, so we emit TOML by hand. A literal double
        quote, backslash, or control character would need escaping to stay valid;
        rather than hand-roll an escaper (easy to get subtly wrong), we reject those
        rare inputs with a clear error. Paths are rendered with forward slashes
        before they reach here, so a Windows backslash never trips this.
    """
    if '"' in value or "\\" in value or any(ord(c) < 0x20 for c in value):
        raise ConfigError(
            f"{where} has a {field} containing a quote, backslash, or control "
            f"character, which Orion will not write into orion.toml. Use a simpler "
            f"value (for paths, forward slashes work on every OS), or edit the "
            f"config by hand."
        )
    return f'"{value}"'


def render_project_stanza(
    name: str,
    repo_path: Path | str,
    share_level: str,
    collectors: tuple[str, ...],
    recipients: tuple[Recipient, ...],
    tasks_file: Path | str | None = None,
    notes_file: Path | str | None = None,
    *,
    with_state_db: bool = False,
) -> str:
    """Build the TOML text for one new project (table + its recipient sub-tables).

    Args:
        name: The project key; must match PROJECT_NAME_RE.
        repo_path: Path to the local git repo (rendered with forward slashes).
        share_level: One of SHARE_LEVELS.
        collectors: Enabled signal names; non-empty, all in SUPPORTED_COLLECTORS.
        recipients: At least one Recipient (a project with none would be rejected
            on load, so we refuse to write one).
        tasks_file: Required iff "tasks" is in `collectors`; ignored otherwise.
        notes_file: Required iff "notes" is in `collectors`; ignored otherwise.
        with_state_db: When True (creating a brand-new orion.toml), prepend the
            global `state_db` line so the file is valid on its own. When appending
            to an existing config, leave it off (state_db already lives there).

    Returns:
        TOML text ending in a single newline, ready to write (create) or append.

    Why:
        Keeping this pure (no I/O) makes it trivially testable: a test renders a
        stanza, writes it to a temp file, and asserts config.load_config accepts it
        — the real loader is the oracle, so the writer can never drift from the
        reader. It mirrors hooks.build_hook_script: build the text here, let the CLI
        own the file I/O. auto_send is always written False: enabling unattended
        send is a deliberate manual edit, never a side effect of registering.
    """
    where = f"project {name!r}"

    if not PROJECT_NAME_RE.match(name):
        raise ConfigError(
            f"{where} is not a valid project name. Use letters, digits, underscores "
            f"or hyphens only (no spaces or dots) so it stays a clean TOML key and "
            f"`orion report <name>` argument."
        )
    if share_level not in SHARE_LEVELS:
        raise ConfigError(
            f"{where} has invalid share_level={share_level!r}. Expected one of {SHARE_LEVELS}."
        )
    if not collectors:
        raise ConfigError(f"{where} needs at least one collector.")
    for c in collectors:
        if c not in SUPPORTED_COLLECTORS:
            raise ConfigError(
                f"{where} lists unsupported collector {c!r}. Supported now: {SUPPORTED_COLLECTORS}."
            )
    if not recipients:
        raise ConfigError(f"{where} needs at least one recipient.")

    # Pair each enabled file-backed collector with its required file path, mirroring
    # config._parse_collector_file's contract (enabled ⇒ a path is required).
    collector_files = {"tasks": tasks_file, "notes": notes_file}

    lines: list[str] = []
    if with_state_db:
        lines.append(f'state_db = {_toml_basic_string(DEFAULT_STATE_DB, "state_db", where)}')
        lines.append("")

    lines.append(f"[projects.{name}]")
    lines.append(f'repo_path = {_toml_basic_string(Path(repo_path).as_posix(), "repo_path", where)}')
    lines.append(f'share_level = {_toml_basic_string(share_level, "share_level", where)}')
    # auto_send is intentionally always false here — see the docstring.
    lines.append("auto_send = false")
    rendered_collectors = ", ".join(
        _toml_basic_string(c, "collectors", where) for c in collectors
    )
    lines.append(f"collectors = [{rendered_collectors}]")

    for collector, key in COLLECTOR_FILE_KEYS.items():
        if collector not in collectors:
            continue
        path = collector_files[collector]
        if path is None or not str(path).strip():
            raise ConfigError(
                f"{where} enables the {collector!r} collector but no {key} was given."
            )
        lines.append(f'{key} = {_toml_basic_string(Path(path).as_posix(), key, where)}')

    for recipient in recipients:
        lines.append("")
        lines.append(f"[[projects.{name}.recipients]]")
        lines.append(f'name = {_toml_basic_string(recipient.name, "name", where)}')
        lines.append(f'channel = {_toml_basic_string(recipient.channel, "channel", where)}')
        lines.append(
            f'webhook_env_var = {_toml_basic_string(recipient.webhook_env_var, "webhook_env_var", where)}'
        )

    return "\n".join(lines) + "\n"

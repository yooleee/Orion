# =============================================================================
# config.py
# -----------------------------------------------------------------------------
# Responsible for: Loading and validating the project registry (orion.toml) into
#                  typed dataclasses, and looking up a single project by name.
# Role in project: The first step of every `orion report` run. Everything
#                  downstream (collector, summarizer, delivery) reads from the
#                  ProjectConfig this module produces.
# Assumptions: Python 3.11+ for stdlib `tomllib`. The config is READ-ONLY here;
#              Orion never writes it (that is why TOML's read-only nature is fine).
# =============================================================================

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Allowed values, kept as named constants so validation and error messages share
# one source of truth (DRY) and adding a value later is a one-line change.
SHARE_LEVELS = ("high_level", "detailed")  # "high_level" sends no code diff (safest).
SUPPORTED_COLLECTORS = ("git", "tasks", "notes")  # Phase 2: structured lane added.
SUPPORTED_CHANNELS = ("discord", "slack")  # Phase 3: Slack added alongside Discord.

# Each file-backed structured collector reads ONE local file, named per project by
# this TOML key. The map keeps validation DRY: one loop adds a clear "you enabled
# X but gave no X_file" check for every entry, so adding a future file collector
# is a one-line change here rather than another bespoke validation block.
COLLECTOR_FILE_KEYS = {
    "tasks": "tasks_file",  # a Markdown checklist (- [x] / - [ ])
    "notes": "notes_file",  # a hand-written "current note" file
}

DEFAULT_STATE_DB = "orion.sqlite3"


class ConfigError(Exception):
    """Raised when the config file is missing, malformed, or fails validation.

    Why:
        A dedicated exception lets the CLI catch *config* problems specifically
        and print a clear, fixable message instead of a raw traceback. Config
        errors are user errors (a typo in orion.toml), so they deserve a kind,
        actionable message rather than a stack trace.
    """


@dataclass(frozen=True)
class Recipient:
    """One named person who receives a project's reports over one channel.

    Args:
        name: Human-readable recipient name (e.g. "Alex (supervisor)").
        channel: Delivery channel — one of SUPPORTED_CHANNELS ("discord" or "slack").
        webhook_env_var: Name of the .env variable that holds this recipient's
            webhook URL. The URL itself is never stored in the config.

    Why:
        Modeling recipients explicitly (rather than assuming a single implicit
        "me") keeps the door open for multi-supervisor delivery later without a
        rewrite. The env-var indirection keeps secrets out of the shareable
        config file — a privacy requirement, not just a style choice.
    """

    name: str
    channel: str
    webhook_env_var: str


@dataclass(frozen=True)
class ProjectConfig:
    """Everything Orion needs to produce one project's report.

    Args:
        name: The project key (what the user passes to `orion report <name>`).
        repo_path: Absolute, user-expanded path to the local git repo.
        share_level: One of SHARE_LEVELS; controls how much detail is exposed.
        collectors: Enabled signal names (e.g. "git", "tasks", "notes").
        recipients: Who receives the report.
        tasks_file: Path to the Markdown checklist, or None when the "tasks"
            collector is not enabled. Resolved absolute at load time.
        notes_file: Path to the hand-written notes file, or None when the "notes"
            collector is not enabled. Resolved absolute at load time.

    Why:
        A frozen dataclass gives a typed, immutable bundle to pass down the
        pipeline, so no downstream function has to re-parse raw TOML or guess at
        defaults — they were all resolved and validated here, once. The file
        paths are Optional because they only exist when their collector is on; a
        downstream collector receiving None would be a config-validation bug, not
        a runtime surprise (we guarantee the path is set whenever its collector is).
    """

    name: str
    repo_path: Path
    share_level: str
    collectors: tuple[str, ...]
    recipients: tuple[Recipient, ...]
    tasks_file: Path | None = None
    notes_file: Path | None = None


@dataclass(frozen=True)
class Config:
    """The whole registry: the state-DB location plus every tracked project.

    Args:
        state_db: Absolute path to the sqlite state store.
        projects: Map of project name -> ProjectConfig.

    Why:
        Bundling the global state_db path with the projects means the CLI loads
        config once and has everything it needs to open state and pick a project.
    """

    state_db: Path
    projects: dict[str, ProjectConfig] = field(default_factory=dict)


def load_config(path: Path) -> Config:
    """Read and validate orion.toml into a typed Config.

    Args:
        path: Path to the TOML config file.

    Returns:
        A validated Config with all defaults resolved and paths expanded.

    Why:
        Validating once, up front, means the rest of the pipeline can trust its
        inputs and never has to defend against a missing key or bad value. We
        fail loudly here (ConfigError) with a message that names the fix, because
        a config typo should be a five-second correction, not a debugging session.
    """
    if not path.exists():
        raise ConfigError(
            f"Config file not found: {path}\n"
            f"Copy orion.toml.example to {path.name} and edit it."
        )

    # tomllib.load requires a binary file handle (it decodes UTF-8 itself).
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Could not parse {path}: {exc}") from exc

    # state_db: a relative path is resolved next to the config file, so the user
    # can point at "orion.sqlite3" without worrying about the working directory.
    state_db_raw = raw.get("state_db", DEFAULT_STATE_DB)
    state_db = Path(state_db_raw)
    if not state_db.is_absolute():
        state_db = (path.parent / state_db).resolve()

    projects_table = raw.get("projects")
    if not isinstance(projects_table, dict) or not projects_table:
        raise ConfigError(
            f"{path} defines no projects. Add at least one [projects.<name>] table."
        )

    projects: dict[str, ProjectConfig] = {}
    for name, body in projects_table.items():
        projects[name] = _parse_project(name, body, path)

    return Config(state_db=state_db, projects=projects)


def _parse_project(name: str, body: object, config_path: Path) -> ProjectConfig:
    """Validate one [projects.<name>] table into a ProjectConfig.

    Args:
        name: The project key from the TOML table header.
        body: The raw value under that key (expected to be a dict/table).
        config_path: Path to the config, used to make error messages locatable.

    Returns:
        A fully validated, defaults-resolved ProjectConfig.

    Why:
        Pulling per-project validation into its own function keeps load_config
        readable and means each project gets identical, consistent checks.
    """
    where = f"[projects.{name}] in {config_path}"

    if not isinstance(body, dict):
        raise ConfigError(f"{where} must be a table.")

    # repo_path is required — without it there is nothing to report on.
    repo_path_raw = body.get("repo_path")
    if not isinstance(repo_path_raw, str) or not repo_path_raw.strip():
        raise ConfigError(f"{where} is missing a non-empty `repo_path`.")
    # expanduser() resolves a leading "~"; we keep it absolute for git/subprocess.
    repo_path = Path(repo_path_raw).expanduser()

    # share_level defaults to the safest option and must be a known value.
    share_level = body.get("share_level", "high_level")
    if share_level not in SHARE_LEVELS:
        raise ConfigError(
            f"{where} has invalid share_level={share_level!r}. "
            f"Expected one of {SHARE_LEVELS}."
        )

    # collectors defaults to ["git"]; every entry must be supported in this phase.
    collectors_raw = body.get("collectors", ["git"])
    if not isinstance(collectors_raw, list) or not collectors_raw:
        raise ConfigError(f"{where} `collectors` must be a non-empty list.")
    for c in collectors_raw:
        if c not in SUPPORTED_COLLECTORS:
            raise ConfigError(
                f"{where} lists unsupported collector {c!r}. "
                f"Supported now: {SUPPORTED_COLLECTORS}."
            )

    recipients = _parse_recipients(body.get("recipients"), where)

    # Structured file-backed collectors each need their file path — but only when
    # that collector is actually enabled. _parse_collector_file enforces exactly
    # that pairing and resolves the path relative to the config file.
    tasks_file = _parse_collector_file(
        body, "tasks", collectors_raw, config_path, where
    )
    notes_file = _parse_collector_file(
        body, "notes", collectors_raw, config_path, where
    )

    return ProjectConfig(
        name=name,
        repo_path=repo_path,
        share_level=share_level,
        collectors=tuple(collectors_raw),
        recipients=recipients,
        tasks_file=tasks_file,
        notes_file=notes_file,
    )


def _parse_collector_file(
    body: dict,
    collector: str,
    enabled: list,
    config_path: Path,
    where: str,
) -> Path | None:
    """Resolve the file path for one file-backed collector, if it is enabled.

    Args:
        body: The raw [projects.<name>] table.
        collector: The collector key (e.g. "tasks", "notes").
        enabled: The project's list of enabled collector names.
        config_path: Path to the config file, used to resolve relative paths and
            to locate error messages.
        where: A locating string for error messages.

    Returns:
        An absolute Path when the collector is enabled, or None when it is not.

    Why:
        Pairing "collector enabled" with "its file is configured" at load time
        turns a would-be confusing run-time failure ("no file to read") into a
        clear config error pointing at the exact key to add. We do NOT check that
        the file exists here: the file may legitimately be created after config
        (e.g. a TODO.md a user fills in next), and existence is the collector's
        job to report clearly at run time. Relative paths resolve against the
        config file's directory, mirroring state_db, so a user can write
        `tasks_file = "TODO.md"` without worrying about the working directory.
    """
    key = COLLECTOR_FILE_KEYS[collector]

    # If the collector is off, the path is irrelevant — ignore it (even if present)
    # and return None so ProjectConfig records "no path" for this signal.
    if collector not in enabled:
        return None

    raw = body.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(
            f"{where} enables the {collector!r} collector but is missing a "
            f"non-empty `{key}`."
        )

    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (config_path.parent / path).resolve()
    return path


def _parse_recipients(raw: object, where: str) -> tuple[Recipient, ...]:
    """Validate the list of recipients for one project.

    Args:
        raw: The raw value under `recipients` (expected list of tables).
        where: A locating string for error messages.

    Returns:
        A tuple of validated Recipient objects.

    Why:
        Delivery is pointless with no recipient, and a half-specified recipient
        (missing channel or webhook var) would fail confusingly at send time —
        far from the typo. Validating here surfaces the problem at load time.
    """
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{where} needs at least one [[projects.<name>.recipients]] entry.")

    recipients: list[Recipient] = []
    for i, item in enumerate(raw):
        rwhere = f"{where} recipient #{i + 1}"
        if not isinstance(item, dict):
            raise ConfigError(f"{rwhere} must be a table.")

        name = item.get("name")
        channel = item.get("channel")
        webhook_env_var = item.get("webhook_env_var")

        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{rwhere} is missing a non-empty `name`.")
        if channel not in SUPPORTED_CHANNELS:
            raise ConfigError(
                f"{rwhere} has invalid channel={channel!r}. "
                f"Supported now: {SUPPORTED_CHANNELS}."
            )
        if not isinstance(webhook_env_var, str) or not webhook_env_var.strip():
            raise ConfigError(f"{rwhere} is missing a non-empty `webhook_env_var`.")

        recipients.append(
            Recipient(name=name, channel=channel, webhook_env_var=webhook_env_var)
        )

    return tuple(recipients)


def get_project(config: Config, name: str) -> ProjectConfig:
    """Look up one project by name, with a helpful error if absent.

    Args:
        config: The loaded Config.
        name: The project name requested on the command line.

    Returns:
        The matching ProjectConfig.

    Why:
        A user who typos a project name should immediately see the list of names
        that DO exist, rather than a generic KeyError — it turns a dead end into
        a one-glance fix.
    """
    try:
        return config.projects[name]
    except KeyError:
        known = ", ".join(sorted(config.projects)) or "(none)"
        raise ConfigError(
            f"Unknown project {name!r}. Known projects: {known}."
        ) from None

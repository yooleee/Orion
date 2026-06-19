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

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Allowed values, kept as named constants so validation and error messages share
# one source of truth (DRY) and adding a value later is a one-line change.
SHARE_LEVELS = ("high_level", "detailed")  # "high_level" sends no code diff (safest).
SUPPORTED_COLLECTORS = ("git", "tasks", "notes")  # Phase 2: structured lane added.
SUPPORTED_CHANNELS = ("discord", "slack")  # Phase 3: Slack added alongside Discord.

# Which summarizer backends Orion can drive (B4). "anthropic" is the default and
# uses the Anthropic Messages API; "local" targets any OpenAI-compatible chat
# endpoint (Ollama / llama.cpp / LM Studio) over stdlib urllib. Kept as a named
# constant so validation and messages share one source of truth, mirroring
# SHARE_LEVELS / SUPPORTED_CHANNELS, and adding a backend later is a one-line edit.
SUMMARIZER_PROVIDERS = ("anthropic", "local")

# The default model for the default (anthropic) provider — the lightest model
# adequate for summarization, per the project's "lightest adequate model" rule.
# A config with no [summarizer] table resolves to exactly this, so existing
# configs keep their current behavior unchanged.
DEFAULT_SUMMARIZER_MODEL = "claude-haiku-4-5"

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
        auto_send: Whether this project may be delivered WITHOUT the human preview
            during an unattended run. Defaults to False (opt-in). It only takes
            effect when the `report` command is also given `--yes`; on its own it
            never bypasses the preview (see cli._run_report). Defense in depth.

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
    auto_send: bool = False


@dataclass(frozen=True)
class SummarizerConfig:
    """Which LLM backend summarizes the raw (git) lane, chosen by config (B4).

    Args:
        provider: One of SUMMARIZER_PROVIDERS ("anthropic" or "local").
        model: The model id to request (e.g. "claude-haiku-4-5" for Anthropic, or
            a local model name like "llama3.1" for the local backend).
        base_url: For the "local" provider only: the base URL of an
            OpenAI-compatible chat endpoint (e.g. "http://localhost:11434/v1").
            None for the "anthropic" provider.
        api_key_env: For the "local" provider only: the NAME of an optional .env
            variable holding an API key, when the endpoint requires one. Most
            local servers need none, so this is None unless explicitly set. The
            "anthropic" provider always uses ANTHROPIC_API_KEY (not this field).

    Why:
        This is the provider-agnostic seam B4 introduces: the summarizer step is
        no longer hardwired to one Anthropic model. A frozen, validated bundle
        lets cli.py build the configured backend without re-parsing TOML or
        guessing defaults. It is GLOBAL (one [summarizer] table) for now — the
        smallest surface that lets the model/provider vary; a per-project override
        stays an additive change later (build seams, not futures).
    """

    provider: str
    model: str
    base_url: str | None = None
    api_key_env: str | None = None


@dataclass(frozen=True)
class RelayConfig:
    """Whether (and where) to push the portable report blob to a hosted relay (C1).

    Args:
        enabled: Master opt-in switch. When False, the relay is a pure no-op — no
            outbound push, no behavior change — so every existing config is
            unaffected.
        url: The relay's ingest URL the serialized blob is POSTed to. Empty when
            disabled; required (non-empty) when enabled.
        token_env_var: NAME of the .env variable holding the Bearer token sent with
            the push. The token itself never lives in the config — same env-var
            indirection as a recipient's webhook_env_var, so the shareable config
            carries no secret. Empty when disabled; required when enabled.

    Why:
        C1 adds one outbound seam: "serialize the blob + a token → POST to a URL."
        Modeling it as a global, opt-in [relay] table (one relay for every project,
        like [summarizer] is one summarizer) is the smallest surface that turns the
        push on; a per-project relay stays an additive change later. Keeping the
        token as an env-var name (not the value) preserves the privacy rule that
        secrets never enter orion.toml.
    """

    enabled: bool
    url: str
    token_env_var: str


@dataclass(frozen=True)
class Config:
    """The whole registry: state-DB location, summarizer/relay config, every project.

    Args:
        state_db: Absolute path to the sqlite state store.
        summarizer: The configured summarizer backend (B4). Defaults to
            Anthropic/Haiku when no [summarizer] table is present.
        relay: The hosted-relay push config (C1). Defaults to disabled (a no-op)
            when no [relay] table is present.
        projects: Map of project name -> ProjectConfig.

    Why:
        Bundling the global state_db path, summarizer, and relay config with the
        projects means the CLI loads config once and has everything it needs to
        open state, build the summarizer, decide whether to push to a relay, and
        pick a project.
    """

    state_db: Path
    summarizer: SummarizerConfig
    relay: RelayConfig
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

    # The summarizer backend is global (one [summarizer] table). An absent table
    # resolves to the Anthropic/Haiku default, so existing configs are unchanged.
    summarizer = _parse_summarizer(raw.get("summarizer"), path)

    # The relay is global (one [relay] table) and opt-in. An absent or disabled
    # table resolves to a no-op, so existing configs push nowhere and are unchanged.
    relay = _parse_relay(raw.get("relay"), path)

    return Config(
        state_db=state_db, summarizer=summarizer, relay=relay, projects=projects
    )


# A valid environment-variable NAME: a letter or underscore, then letters, digits, or
# underscores (POSIX-ish). The *_env_var config fields must be NAMES — the secret VALUE
# lives in .env — so we validate them against this.
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_env_var_name(value: str, field: str, where: str) -> None:
    """Raise ConfigError if `value` is not shaped like an environment variable name.

    Args:
        value: the configured string (caller has already confirmed a non-empty str).
        field: the config key, e.g. "token_env_var", for the error message.
        where: a human location, e.g. "[relay]" or "recipient 'Alex'".

    Returns:
        None. Raises ConfigError when `value` is not a valid variable name.

    Why:
        Each *_env_var field NAMES a .env variable; the secret itself never belongs in the
        config. Validating the name shape here turns the easy "pasted the secret value
        where the variable name goes" slip into a clear error at config load — and avoids a
        later "secret '<value>' is not set" path that would ECHO the pasted secret. We
        deliberately do NOT put `value` in the message (it may be the secret). Heuristic,
        not airtight (a value that happens to be a valid identifier would still pass), but
        it catches the common shapes: leading digits, hyphens in base64url tokens, spaces,
        and the punctuation of a pasted webhook URL.
    """
    if not _ENV_VAR_NAME_RE.match(value):
        raise ConfigError(
            f"{where} has a `{field}` that is not a valid environment variable name. "
            f"It must NAME a .env variable — letters, digits and underscores, not starting "
            f'with a digit (e.g. "ORION_RELAY_TOKEN") — not the secret value itself, which '
            f"belongs in .env."
        )


def _parse_summarizer(raw: object, config_path: Path) -> SummarizerConfig:
    """Validate the optional [summarizer] table into a SummarizerConfig (B4).

    Args:
        raw: The raw value under the top-level `summarizer` key, or None when the
            table is absent.
        config_path: Path to the config, used to locate error messages.

    Returns:
        A validated SummarizerConfig. When the table is absent, the default
        Anthropic/Haiku backend (so a config with no [summarizer] keeps its
        current behavior exactly).

    Why:
        Centralizing this here means cli.py can trust the backend choice without
        re-checking it, and a typo (unknown provider, a local backend with no
        base_url) fails loudly at load time with a message naming the fix —
        mirroring how share_level and collectors are validated. We require an
        explicit `model` for the local backend because there is no universal
        default local model name, but default the Anthropic model to the lightest
        adequate one so the common case needs no model line.
    """
    where = f"[summarizer] in {config_path}"

    # Absent table -> the default backend. This is the backward-compatible path.
    if raw is None:
        return SummarizerConfig(provider="anthropic", model=DEFAULT_SUMMARIZER_MODEL)

    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a table.")

    provider = raw.get("provider", "anthropic")
    if provider not in SUMMARIZER_PROVIDERS:
        raise ConfigError(
            f"{where} has invalid provider={provider!r}. "
            f"Expected one of {SUMMARIZER_PROVIDERS}."
        )

    if provider == "anthropic":
        # Anthropic: model defaults to the lightest adequate one; base_url and
        # api_key_env do not apply (the key is always ANTHROPIC_API_KEY).
        model = raw.get("model", DEFAULT_SUMMARIZER_MODEL)
        if not isinstance(model, str) or not model.strip():
            raise ConfigError(f"{where} has an invalid `model` (must be a non-empty string).")
        return SummarizerConfig(provider="anthropic", model=model.strip())

    # provider == "local": an OpenAI-compatible endpoint. base_url and an explicit
    # model are both required (no sensible universal default for either); an API
    # key is optional and named by api_key_env only when the endpoint needs one.
    base_url = raw.get("base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        raise ConfigError(
            f"{where} uses provider='local' but is missing a non-empty `base_url` "
            f"(e.g. base_url = \"http://localhost:11434/v1\")."
        )

    model = raw.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ConfigError(
            f"{where} uses provider='local' but is missing a non-empty `model` "
            f"(the local model name, e.g. model = \"llama3.1\")."
        )

    api_key_env = raw.get("api_key_env")
    if api_key_env is not None and (not isinstance(api_key_env, str) or not api_key_env.strip()):
        raise ConfigError(
            f"{where} has an invalid `api_key_env` (must be a non-empty string when set)."
        )
    if isinstance(api_key_env, str) and api_key_env.strip():
        _validate_env_var_name(api_key_env.strip(), "api_key_env", where)

    return SummarizerConfig(
        provider="local",
        model=model.strip(),
        base_url=base_url.strip(),
        api_key_env=api_key_env.strip() if isinstance(api_key_env, str) else None,
    )


def _parse_relay(raw: object, config_path: Path) -> RelayConfig:
    """Validate the optional [relay] table into a RelayConfig (C1).

    Args:
        raw: The raw value under the top-level `relay` key, or None when the table
            is absent.
        config_path: Path to the config, used to locate error messages.

    Returns:
        A validated RelayConfig. Absent or `enabled = false` resolves to a disabled
        no-op (empty url/token), so a config with no relay — or one that has turned
        it off — pushes nowhere and behaves exactly as before.

    Why:
        Mirrors _parse_summarizer: centralizing validation here means the CLI can
        trust the relay config without re-checking it, and a half-specified relay
        (enabled but no url, or no token var) fails loudly at load time naming the
        exact key to add — rather than failing confusingly at push time. We only
        require url/token_env_var when the relay is enabled; when it is off they are
        irrelevant and ignored even if present (the same "ignore when off" rule
        _parse_collector_file uses for a disabled collector's file path).
    """
    where = f"[relay] in {config_path}"

    # Absent table -> disabled no-op. This is the backward-compatible path: every
    # config that predates C1 has no [relay] and must keep pushing nowhere.
    if raw is None:
        return RelayConfig(enabled=False, url="", token_env_var="")

    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a table.")

    # enabled defaults to False (opt-in) and must be a real boolean. isinstance
    # rejects ints/strings, so `enabled = 1` or `enabled = "yes"` is caught here
    # rather than silently treated as truthy — same strictness as auto_send.
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError(
            f"{where} has invalid enabled={enabled!r}. Expected true or false."
        )

    # Disabled -> a pure no-op; url/token_env_var are irrelevant, so we ignore them
    # (even if present) and return empty values rather than validating them.
    if not enabled:
        return RelayConfig(enabled=False, url="", token_env_var="")

    # Enabled -> both the destination URL and the token's env-var name are required.
    url = raw.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ConfigError(
            f"{where} is enabled but missing a non-empty `url` "
            f'(the relay ingest endpoint, e.g. url = "http://127.0.0.1:8787/ingest").'
        )

    token_env_var = raw.get("token_env_var")
    if not isinstance(token_env_var, str) or not token_env_var.strip():
        raise ConfigError(
            f"{where} is enabled but missing a non-empty `token_env_var` "
            f'(the .env variable holding the ingest token, e.g. token_env_var = "ORION_RELAY_TOKEN").'
        )
    _validate_env_var_name(token_env_var.strip(), "token_env_var", where)

    return RelayConfig(
        enabled=True, url=url.strip(), token_env_var=token_env_var.strip()
    )


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

    # auto_send defaults to False (opt-in, safest) and must be a real boolean.
    # isinstance(x, bool) rejects ints and strings, so TOML `auto_send = 1` or
    # `auto_send = "yes"` is caught here rather than silently treated as truthy.
    auto_send = body.get("auto_send", False)
    if not isinstance(auto_send, bool):
        raise ConfigError(
            f"{where} has invalid auto_send={auto_send!r}. Expected true or false."
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
        auto_send=auto_send,
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
        _validate_env_var_name(webhook_env_var.strip(), "webhook_env_var", rwhere)

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

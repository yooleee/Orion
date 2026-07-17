# =============================================================================
# config.py
# -----------------------------------------------------------------------------
# Responsible for: Loading and validating the project registry (orion.toml) into
#                  typed dataclasses, and looking up a single project by name.
# Role in project: The first step of every `orion report` run. Everything
#                  downstream (collector, summarizer, delivery) reads from the
#                  ProjectConfig this module produces.
# Assumptions: Python 3.11+ for stdlib `tomllib`. This module is READ-ONLY — it
#              only loads and validates. Config is never written as a SIDE EFFECT
#              of a run; the one writer is the explicit `orion add-project` command
#              (scaffold.py + cli.cmd_add_project), which appends/creates and
#              previews first. So `tomllib` having no writer is all this needs.
# =============================================================================

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Allowed values, kept as named constants so validation and error messages share
# one source of truth (DRY) and adding a value later is a one-line change.
SHARE_LEVELS = ("high_level", "detailed")  # "high_level" sends no code diff (safest).
SUPPORTED_COLLECTORS = ("git", "tasks", "notes", "incubator", "tracker", "disciplines")  # E2 Inc 4 4b: disciplines added.
SUPPORTED_CHANNELS = ("discord", "slack")  # Phase 3: Slack added alongside Discord.

# Which chat platforms the native two-way bot can listen on (C2-bots). Slack
# (Socket Mode) is the first slice; Discord (Gateway) is the planned next platform,
# at which point this becomes a one-line addition — the [bot] config, the pure
# decision core, and the relay write endpoint are all platform-neutral, so adding a
# platform is additive (a new shell module + this tuple), not a rewrite.
SUPPORTED_BOT_PLATFORMS = ("slack",)

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
    "incubator": "incubator_file",  # an idea-pipeline Markdown table (index.md)
    "tracker": "tracker_file",  # a status-per-section "rich" doc (E2 Inc 2.6)
}

# Collectors that feed the dashboard's LIVE checklist surface ({text, done}). Both
# read a local file and produce ChecklistItems via their snapshot(); the `checklist`
# flag requires AT LEAST ONE of these. Kept as one constant so the config validation
# and the CLI's checklist-push path agree on what counts as a checklist source.
CHECKLIST_COLLECTORS = ("tasks", "tracker")

# E2 Inc 4: the dashboard home splits real software "project"s from general "tracker"s
# (checklists that are not a project, e.g. an applications to-do list). This is an EXPLICIT,
# observed fact from the user's own config — not inferred — so it is a named flag with a
# safe default of "project". An open enum (like share_level) so future kinds are additive.
PROJECT_KINDS = ("project", "tracker")
DEFAULT_PROJECT_KIND = "project"

# E1.2: the per-project report CADENCE presets — the minimum spacing between unattended
# reports, consumed by `report --all --due` (Unit 2) to skip a project that reported too
# recently. OPTIONAL: unlike share_level/kind, the field has NO default preset — an absent
# key means "no cadence set", which `--due` treats as always due (backward compatible).
# Validated against this tuple only when present, mirroring SHARE_LEVELS. The preset →
# minimum-interval mapping (with DST/jitter slack) lives with the consumer, not here, so
# Unit 1 stays pure config. An open enum so an hours-granularity knob stays additive later.
CADENCES = ("daily", "weekly")

# E1.2: bounds for the optional per-project `due_soon_days` knob — how many days ahead a
# checklist item's due date is flagged "due soon" on the dashboard. OPTIONAL (absent ⇒
# the relay's 7-day default). 1 is the tightest useful window; 365 caps it at a year so a
# typo like 3650 is caught at load rather than silently flattening the at-risk view. Kept
# as named constants so the validation and its error message share one source of truth.
DUE_SOON_DAYS_MIN = 1
DUE_SOON_DAYS_MAX = 365

DEFAULT_STATE_DB = "orion.sqlite3"

# The display time zone for human-facing timestamps in delivered messages (KI-20).
# Defaults to America/Los_Angeles so a delivered chat message reads in the SAME zone
# the relay dashboard already renders (PDT/PST, DST-correct), instead of UTC — the two
# surfaces agreed by default. An IANA zone name; a user with non-Pacific recipients can
# set `display_timezone = "UTC"` (or any zone) in orion.toml. Validated at load time.
DEFAULT_DISPLAY_TIMEZONE = "America/Los_Angeles"


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
        signals: The signal types this recipient receives — a subset of the
            project's enabled collectors (e.g. ("git",) for a recipient who should
            only see code activity). Resolved at load time: omitting `signals` in
            the config means the recipient gets ALL of the project's collectors
            (today's behavior), so it is populated with the project's collectors
            then. Defaults to () on the dataclass purely so direct construction
            (scaffold, tests) stays valid; config-loaded recipients always carry a
            concrete, non-empty subset.

    Why:
        Modeling recipients explicitly (rather than assuming a single implicit
        "me") keeps the door open for multi-supervisor delivery later without a
        rewrite. The env-var indirection keeps secrets out of the shareable
        config file — a privacy requirement, not just a style choice. `signals`
        adds lightweight, audience-typed routing (D5): different supervisors can
        receive different slices of the same project's report (a mentor sees the
        incubator/idea signal, a teammate sees git) without any per-recipient
        identity or state — it is pure config-level CONTENT filtering on today's
        named-recipient seam, orthogonal to the per-recipient delivery state that
        C3 defers. Omitting it preserves the existing "everyone gets everything".
    """

    name: str
    channel: str
    webhook_env_var: str
    # Defaulted last (() ) so existing keyword/positional construction stays valid;
    # the loader replaces () with the project's collectors when the key is omitted.
    signals: tuple[str, ...] = ()


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
        incubator_file: Path to the idea-pipeline Markdown table (an incubator's
            index.md), or None when the "incubator" collector is not enabled.
            Resolved absolute at load time.
        tracker_file: Path to a status-per-section "rich" tracker doc (e.g. an
            applications to-do list), or None when the "tracker" collector is not
            enabled. Resolved absolute at load time. Like tasks_file, it can back the
            dashboard checklist (see `checklist`).
        auto_send: Whether this project may be delivered WITHOUT the human preview
            during an unattended run. Defaults to False (opt-in). It only takes
            effect when the `report` command is also given `--yes`; on its own it
            never bypasses the preview (see cli._run_report). Defense in depth.
        checklist: Whether to surface this project's LIVE checklist (open + done
            items) on the dashboard. Defaults to False (opt-in). Requires at least
            one CHECKLIST_COLLECTORS collector — `tasks` (reads tasks_file) or
            `tracker` (reads tracker_file) — to be enabled (validated at load), since
            that is what resolves the file the checklist is read from.
        kind: One of PROJECT_KINDS ("project" | "tracker"). Splits the dashboard home
            into real software projects vs. general trackers. Defaults to "project".
        cadence: One of CADENCES ("daily" | "weekly") or None. The minimum spacing
            between unattended reports for `report --all --due`; None (the default, and
            what an absent key resolves to) means the project is always due. Purely
            local — it is never sent to the relay.
        due_soon_days: How many days ahead a checklist item's due date counts as
            "due soon" on the dashboard, or None to use the relay's default (7). An
            int in 1..365 when set. Unlike `cadence`, this one IS sent to the relay —
            it rides both checklist carriers (the ingest blob and the /checklist
            push), omitted from the wire entirely when None (back-compatible).
        discipline_docs: The instruction/design/decision docs the "disciplines"
            collector reads (absolute paths), or () when that collector is not enabled.
            Resolved absolute at load time. Unlike the single-file collectors this is a
            LIST — disciplines are observed across several of the user's own docs (e.g.
            CLAUDE.md, design/README.md). The docs are read UNMODIFIED (observe-not-
            originate); the optional LLM step reframes their stated principles.

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
    incubator_file: Path | None = None
    tracker_file: Path | None = None
    auto_send: bool = False
    checklist: bool = False
    kind: str = DEFAULT_PROJECT_KIND
    cadence: str | None = None
    due_soon_days: int | None = None
    discipline_docs: tuple[Path, ...] = ()


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
        admin_token_env_var: NAME of the .env variable holding the SEPARATE admin token
            the `relay-user` provisioning commands authenticate with (C3). Independent
            of the ingest token (the ingest token must not create users), so it is its
            own env-var name. OPTIONAL even when the relay is enabled: a push-only relay
            needs no provisioning, so this is empty unless the operator runs `relay-user`,
            which fails with a clear error when it is unset.

    Why:
        C1 adds one outbound seam: "serialize the blob + a token → POST to a URL."
        Modeling it as a global, opt-in [relay] table (one relay for every project,
        like [summarizer] is one summarizer) is the smallest surface that turns the
        push on; a per-project relay stays an additive change later. Keeping the
        token as an env-var name (not the value) preserves the privacy rule that
        secrets never enter orion.toml. C3's admin token follows the SAME env-var-name
        indirection, added as an optional field so existing configs are unaffected.
    """

    enabled: bool
    url: str
    token_env_var: str
    admin_token_env_var: str = ""


@dataclass(frozen=True)
class BotConfig:
    """Whether (and how) to run the native two-way chat bot (C2-bots).

    Args:
        enabled: Master opt-in switch. When False, the bot is a pure no-op — `orion
            bot` refuses to start and nothing else changes — so every existing config
            is unaffected.
        platform: Which chat platform to listen on — one of SUPPORTED_BOT_PLATFORMS
            ("slack" in this slice). Empty when disabled.
        token_env_var: NAME of the .env variable holding the platform BOT token
            (Slack's `xoxb-…`). The token itself never lives in the config — the same
            env-var indirection as a recipient's webhook_env_var. Empty when disabled.
        app_token_env_var: NAME of the .env variable holding the platform APP-LEVEL
            token (Slack's `xapp-…`), which Socket Mode needs to open its outbound
            WebSocket. Distinct from the bot token. Empty when disabled.
        channel_bindings: Ordered (channel_id, project) pairs — which chat channel
            maps to which project. A supervisor's reply in a bound channel becomes a
            comment on that project's latest report. A tuple (not a dict) to keep the
            dataclass frozen/hashable; the runtime turns it into a dict. Empty when
            disabled.

    Why:
        Modeling the bot as a global, opt-in [bot] table (one bot process serves every
        project, like [relay] is one relay) is the smallest surface that turns it on.
        The bot writes INTO the relay (reusing [relay]'s url + token as its target), so
        BotConfig carries no relay URL/token of its own — only what is bot-specific:
        the platform, its two tokens' env-var NAMES, and the channel→project map. A
        later platform (Discord) or per-project bot is an additive change, not a rewrite.
    """

    enabled: bool
    platform: str
    token_env_var: str
    app_token_env_var: str
    channel_bindings: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Config:
    """The whole registry: state-DB location, summarizer/relay config, every project.

    Args:
        state_db: Absolute path to the sqlite state store.
        summarizer: The configured summarizer backend (B4). Defaults to
            Anthropic/Haiku when no [summarizer] table is present.
        relay: The hosted-relay push config (C1). Defaults to disabled (a no-op)
            when no [relay] table is present.
        bot: The native two-way chat-bot config (C2-bots). Defaults to disabled (a
            no-op) when no [bot] table is present.
        display_timezone: IANA zone name for human-facing timestamps in delivered
            messages (KI-20). Defaults to America/Los_Angeles so messages match the
            dashboard's Pacific display; overridable per config (e.g. "UTC").
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
    bot: BotConfig
    display_timezone: str = DEFAULT_DISPLAY_TIMEZONE
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

    # The bot is global (one [bot] table) and opt-in. Parsed AFTER projects so its
    # channel→project bindings can be validated against the real project names; an
    # absent or disabled table resolves to a no-op, so existing configs are unchanged.
    bot = _parse_bot(raw.get("bot"), path, set(projects))

    # Global display time zone for delivered-message timestamps (KI-20). Absent ->
    # the Pacific default, so existing configs simply start matching the dashboard.
    display_timezone = _parse_display_timezone(raw.get("display_timezone"), path)

    return Config(
        state_db=state_db,
        summarizer=summarizer,
        relay=relay,
        bot=bot,
        display_timezone=display_timezone,
        projects=projects,
    )


def load_relay_config(path: Path) -> RelayConfig:
    """Load ONLY the [relay] table from the config, without requiring any projects.

    Args:
        path: Path to the TOML config file.

    Returns:
        The validated RelayConfig (enabled / url / token_env_var / admin_token_env_var).

    Raises:
        ConfigError: when the file is missing or unparseable, or the [relay] table is
            invalid (the same validation full load_config applies to it).

    Why:
        The `relay-user` admin commands talk only to the relay; they need the [relay]
        table but NOT a local project list. Reusing full load_config would reject a
        config that legitimately has no `[projects.<name>]` (an admin-only operator who
        runs the relay but reports from elsewhere) with an unrelated "defines no projects"
        error. This reads the same TOML and runs the same `_parse_relay` validator, so the
        relay config is validated identically — just without the project requirement.
    """
    if not path.exists():
        raise ConfigError(
            f"Config file not found: {path}\n"
            f"Copy orion.toml.example to {path.name} and edit it."
        )
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Could not parse {path}: {exc}") from exc
    return _parse_relay(raw.get("relay"), path)


def _parse_display_timezone(raw: object, config_path: Path) -> str:
    """Validate the optional top-level `display_timezone` into a known IANA zone (KI-20).

    Args:
        raw: The raw value under the top-level `display_timezone` key, or None when
            the key is absent.
        config_path: The config file path, for a located error message.

    Returns:
        A validated IANA zone name (the default America/Los_Angeles when absent).

    Why:
        Timestamps in delivered messages are rendered in this zone (compose), so a
        bad value must fail LOUDLY at load time — naming the offending string — rather
        than surface as a confusing time (or a crash) on the pre-send path. We validate
        by actually constructing a ZoneInfo: that is the same check the formatter will
        do, so "valid here" means "usable there." An absent key resolves to the Pacific
        default so every pre-KI-20 config keeps working and simply starts matching the
        dashboard. We do NOT keep the ZoneInfo object (it is cheap and cached to
        re-create at use); storing the string keeps Config plain and serializable.
    """
    if raw is None:
        return DEFAULT_DISPLAY_TIMEZONE
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(
            f"`display_timezone` in {config_path} must be a non-empty IANA zone name "
            f'(e.g. display_timezone = "America/Los_Angeles" or "UTC").'
        )
    zone = raw.strip()
    try:
        # Construct it purely to validate; the formatter re-creates it (ZoneInfo is
        # internally cached, so this is not wasted work).
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        # ZoneInfoNotFoundError: no such zone in the tz database. ValueError: a
        # malformed key (e.g. an absolute path). Both are a user typo -> name it.
        raise ConfigError(
            f"`display_timezone` in {config_path} is not a valid IANA zone name: "
            f"{zone!r} ({exc}). Use a name like \"America/Los_Angeles\" or \"UTC\"."
        ) from exc
    return zone


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

    # admin_token_env_var is OPTIONAL (provisioning is a separate, opt-in capability):
    # absent -> "" (the relay-user commands then error clearly if invoked). When present
    # it must be a legal env-var name, validated like token_env_var so a typo fails at
    # load time rather than at provisioning time.
    admin_token_env_var = raw.get("admin_token_env_var", "")
    if not isinstance(admin_token_env_var, str):
        raise ConfigError(
            f"{where} has invalid admin_token_env_var={admin_token_env_var!r}. "
            "Expected the NAME of a .env variable, e.g. "
            'admin_token_env_var = "ORION_RELAY_ADMIN_TOKEN".'
        )
    admin_token_env_var = admin_token_env_var.strip()
    if admin_token_env_var:
        _validate_env_var_name(admin_token_env_var, "admin_token_env_var", where)

    return RelayConfig(
        enabled=True,
        url=url.strip(),
        token_env_var=token_env_var.strip(),
        admin_token_env_var=admin_token_env_var,
    )


def _disabled_bot() -> BotConfig:
    """Build the disabled-no-op BotConfig (absent or `enabled = false`).

    Returns:
        A BotConfig with enabled=False and empty fields.

    Why:
        Both the absent-table and the explicitly-disabled paths return the same no-op
        value; naming it once keeps those two returns identical (DRY) and makes the
        "off means truly inert" contract obvious.
    """
    return BotConfig(
        enabled=False,
        platform="",
        token_env_var="",
        app_token_env_var="",
        channel_bindings=(),
    )


def _parse_bot(raw: object, config_path: Path, project_names: set[str]) -> BotConfig:
    """Validate the optional [bot] table into a BotConfig (C2-bots).

    Args:
        raw: The raw value under the top-level `bot` key, or None when absent.
        config_path: Path to the config, used to locate error messages.
        project_names: The set of project names already parsed from this config, so a
            channel binding to an unknown/typo'd project fails at load time.

    Returns:
        A validated BotConfig. Absent or `enabled = false` resolves to a disabled
        no-op, so a config with no [bot] — or one that has turned it off — runs no bot
        and behaves exactly as before.

    Why:
        Mirrors _parse_relay: centralizing validation here means the CLI can trust the
        bot config without re-checking it, and a half-specified bot (enabled but no
        token var, or a channel bound to a project that does not exist) fails loudly at
        load time naming the exact fix — rather than failing confusingly when the
        always-on process is already running. We validate the channel→project bindings
        against project_names because a typo there would otherwise silently relay a
        supervisor's reply to the wrong (or a nonexistent) project. The relay url/token
        are NOT here: the bot writes into the [relay] table's target, so the CLI reads
        those from config.relay — keeping one relay definition, not two.
    """
    where = f"[bot] in {config_path}"

    # Absent table -> disabled no-op (the backward-compatible path).
    if raw is None:
        return _disabled_bot()

    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a table.")

    # enabled defaults to False (opt-in) and must be a real boolean — same strictness
    # as [relay].enabled and auto_send (so `enabled = 1`/`"yes"` is caught, not coerced).
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError(
            f"{where} has invalid enabled={enabled!r}. Expected true or false."
        )

    # Disabled -> a pure no-op; everything else is ignored even if present.
    if not enabled:
        return _disabled_bot()

    # platform defaults to the only supported value and must be a known one.
    platform = raw.get("platform", "slack")
    if not isinstance(platform, str) or platform not in SUPPORTED_BOT_PLATFORMS:
        raise ConfigError(
            f"{where} has an unsupported platform={platform!r}. "
            f"Supported now: {SUPPORTED_BOT_PLATFORMS}."
        )

    # Both token env-var NAMES are required when enabled: Socket Mode needs the bot
    # token (xoxb-…) AND the app-level token (xapp-…). Each must NAME a .env variable.
    token_env_var = raw.get("token_env_var")
    if not isinstance(token_env_var, str) or not token_env_var.strip():
        raise ConfigError(
            f"{where} is enabled but missing a non-empty `token_env_var` "
            f'(the .env variable holding the bot token, e.g. token_env_var = "ORION_SLACK_BOT_TOKEN").'
        )
    _validate_env_var_name(token_env_var.strip(), "token_env_var", where)

    app_token_env_var = raw.get("app_token_env_var")
    if not isinstance(app_token_env_var, str) or not app_token_env_var.strip():
        raise ConfigError(
            f"{where} is enabled but missing a non-empty `app_token_env_var` "
            f'(the .env variable holding the Socket Mode app-level token, e.g. '
            f'app_token_env_var = "ORION_SLACK_APP_TOKEN").'
        )
    _validate_env_var_name(app_token_env_var.strip(), "app_token_env_var", where)

    # At least one channel→project binding is required — a bot with no channels would
    # listen to nothing. Each binding is a {channel_id, project} table.
    channels_raw = raw.get("channels")
    if not isinstance(channels_raw, list) or not channels_raw:
        raise ConfigError(
            f"{where} is enabled but defines no channels. Add at least one "
            f'[[bot.channels]] table with channel_id = "C…" and project = "<name>".'
        )

    bindings: list[tuple[str, str]] = []
    for index, entry in enumerate(channels_raw):
        cwhere = f"[[bot.channels]] #{index + 1} in {config_path}"
        if not isinstance(entry, dict):
            raise ConfigError(f"{cwhere} must be a table.")
        channel_id = entry.get("channel_id")
        if not isinstance(channel_id, str) or not channel_id.strip():
            raise ConfigError(
                f"{cwhere} is missing a non-empty `channel_id` "
                f'(the platform channel id, e.g. channel_id = "C07ABC123").'
            )
        project = entry.get("project")
        if not isinstance(project, str) or not project.strip():
            raise ConfigError(f"{cwhere} is missing a non-empty `project`.")
        if project.strip() not in project_names:
            raise ConfigError(
                f"{cwhere} binds to unknown project {project.strip()!r}. "
                f"Define [projects.{project.strip()}] or fix the name."
            )
        bindings.append((channel_id.strip(), project.strip()))

    return BotConfig(
        enabled=True,
        platform=platform,
        token_env_var=token_env_var.strip(),
        app_token_env_var=app_token_env_var.strip(),
        channel_bindings=tuple(bindings),
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

    # checklist defaults to False (opt-in). When on, the dashboard surfaces this
    # project's LIVE checklist (open + done), read from a CHECKLIST_COLLECTORS source —
    # `tasks` (tasks_file) or `tracker` (tracker_file). It therefore requires at least
    # one of those collectors to be enabled — that pairing is what guarantees the file
    # is resolved (see _parse_collector_file) — so we reject the contradiction here, at
    # load time, with a fixable message rather than at run time.
    # isinstance(..., bool) rejects ints/strings so `checklist = 1` is caught too.
    checklist = body.get("checklist", False)
    if not isinstance(checklist, bool):
        raise ConfigError(
            f"{where} has invalid checklist={checklist!r}. Expected true or false."
        )
    if checklist and not any(c in collectors_raw for c in CHECKLIST_COLLECTORS):
        raise ConfigError(
            f"{where} enables `checklist` but no checklist source. The live checklist "
            f"is read from a 'tasks' (tasks_file) or 'tracker' (tracker_file) "
            f"collector, so enable one of those (which is what sets its file)."
        )

    # kind defaults to "project" (the common case) and must be a known value — mirroring
    # share_level's validation. A typo (e.g. kind = "tracke") is a config error caught here
    # with a fixable message rather than silently becoming an unrecognized kind downstream.
    kind = body.get("kind", DEFAULT_PROJECT_KIND)
    if kind not in PROJECT_KINDS:
        raise ConfigError(
            f"{where} has invalid kind={kind!r}. Expected one of {PROJECT_KINDS}."
        )
    # A tracker IS a checklist, and `kind` reaches the relay only on the checklist push
    # (the carrier a tracker always uses). So kind = "tracker" without `checklist` would
    # never be delivered — the project would silently never appear as a tracker. Reject
    # that contradiction here, at load, rather than leaving it a confusing no-op.
    if kind == "tracker" and not checklist:
        raise ConfigError(
            f"{where} sets kind = \"tracker\" but does not enable `checklist`. A tracker "
            f"is surfaced from its live checklist, so set `checklist = true` too."
        )

    # cadence is OPTIONAL and has no default preset — an absent key stays None, which
    # `report --all --due` (Unit 2) treats as "always due". So we validate ONLY when the
    # key is present, mirroring share_level/kind's "must be a known value" check. A typo
    # (e.g. cadence = "weekley") is caught here with a fixable message rather than silently
    # becoming an unrecognized cadence downstream.
    cadence = body.get("cadence")
    if cadence is not None and cadence not in CADENCES:
        raise ConfigError(
            f"{where} has invalid cadence={cadence!r}. "
            f"Expected one of {CADENCES}, or omit the key for always-due."
        )

    # due_soon_days is OPTIONAL (absent ⇒ None ⇒ the relay's 7-day default). When set it
    # must be an int in 1..365. `isinstance(x, bool)` is rejected FIRST because bool is a
    # subclass of int in Python, so `due_soon_days = true` would otherwise slip through as
    # 1 — the same strictness auto_send applies. Validated here so a bad value fails at
    # load with a fixable message rather than riding the wire to the relay.
    due_soon_days = body.get("due_soon_days")
    if due_soon_days is not None:
        if isinstance(due_soon_days, bool) or not isinstance(due_soon_days, int):
            raise ConfigError(
                f"{where} has invalid due_soon_days={due_soon_days!r}. "
                f"Expected a whole number of days."
            )
        if not (DUE_SOON_DAYS_MIN <= due_soon_days <= DUE_SOON_DAYS_MAX):
            raise ConfigError(
                f"{where} has out-of-range due_soon_days={due_soon_days!r}. "
                f"Expected {DUE_SOON_DAYS_MIN}..{DUE_SOON_DAYS_MAX}, or omit for the default."
            )

    # Recipients are parsed AFTER collectors are validated so each recipient's
    # `signals` filter can default to (and be validated against) the project's
    # actual collector set — see _parse_recipients.
    recipients = _parse_recipients(body.get("recipients"), where, tuple(collectors_raw))

    # Structured file-backed collectors each need their file path — but only when
    # that collector is actually enabled. _parse_collector_file enforces exactly
    # that pairing and resolves the path relative to the config file.
    tasks_file = _parse_collector_file(
        body, "tasks", collectors_raw, config_path, where
    )
    notes_file = _parse_collector_file(
        body, "notes", collectors_raw, config_path, where
    )
    incubator_file = _parse_collector_file(
        body, "incubator", collectors_raw, config_path, where
    )
    tracker_file = _parse_collector_file(
        body, "tracker", collectors_raw, config_path, where
    )

    # The "disciplines" collector reads a LIST of docs (not a single file), so it gets
    # its own resolver rather than a COLLECTOR_FILE_KEYS entry.
    discipline_docs = _parse_discipline_docs(body, collectors_raw, config_path, where)

    return ProjectConfig(
        name=name,
        repo_path=repo_path,
        share_level=share_level,
        collectors=tuple(collectors_raw),
        recipients=recipients,
        tasks_file=tasks_file,
        notes_file=notes_file,
        incubator_file=incubator_file,
        tracker_file=tracker_file,
        auto_send=auto_send,
        checklist=checklist,
        kind=kind,
        cadence=cadence,
        due_soon_days=due_soon_days,
        discipline_docs=discipline_docs,
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


def _parse_discipline_docs(
    body: dict,
    enabled: list,
    config_path: Path,
    where: str,
) -> tuple[Path, ...]:
    """Resolve the doc list for the "disciplines" collector, if it is enabled.

    Args:
        body: The raw [projects.<name>] table.
        enabled: The project's list of enabled collector names.
        config_path: Path to the config file, used to resolve relative paths and to
            locate error messages.
        where: A locating string for error messages.

    Returns:
        A tuple of absolute Paths when the "disciplines" collector is enabled, or ()
        when it is not. Each entry is expanduser-resolved and made absolute against
        the config file's directory (mirroring _parse_collector_file).

    Why:
        Disciplines are observed across SEVERAL docs, so this collector takes a list
        (`discipline_docs`) rather than a single `*_file`, which is why it gets its
        own resolver instead of a COLLECTOR_FILE_KEYS entry. We require a non-empty
        list when the collector is on (an enabled collector with nothing to read is a
        config mistake worth catching at load), but — like _parse_collector_file — we
        do NOT check that each file exists: a doc may be created later, and a missing
        doc is failed soft at run time (it simply contributes no disciplines).
    """
    if "disciplines" not in enabled:
        return ()

    raw = body.get("discipline_docs")
    if not isinstance(raw, list) or not raw:
        raise ConfigError(
            f"{where} enables the 'disciplines' collector but is missing a non-empty "
            f"`discipline_docs` list (the docs to read principles from)."
        )

    docs: list[Path] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            raise ConfigError(
                f"{where} has an invalid `discipline_docs` entry {entry!r}. "
                f"Each entry must be a non-empty path string."
            )
        path = Path(entry).expanduser()
        if not path.is_absolute():
            path = (config_path.parent / path).resolve()
        docs.append(path)
    return tuple(docs)


def _parse_recipients(
    raw: object, where: str, project_collectors: tuple[str, ...]
) -> tuple[Recipient, ...]:
    """Validate the list of recipients for one project.

    Args:
        raw: The raw value under `recipients` (expected list of tables).
        where: A locating string for error messages.
        project_collectors: The project's enabled collector names, already
            validated. Each recipient's `signals` filter defaults to this (when the
            key is omitted) and is validated as a subset of it.

    Returns:
        A tuple of validated Recipient objects.

    Why:
        Delivery is pointless with no recipient, and a half-specified recipient
        (missing channel or webhook var) would fail confusingly at send time —
        far from the typo. Validating here surfaces the problem at load time. The
        `signals` subset check needs the project's collectors, hence the extra
        argument: a recipient cannot ask for a signal the project does not collect.
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

        signals = _parse_recipient_signals(item.get("signals"), project_collectors, rwhere)

        recipients.append(
            Recipient(
                name=name,
                channel=channel,
                webhook_env_var=webhook_env_var,
                signals=signals,
            )
        )

    return tuple(recipients)


def _parse_recipient_signals(
    raw: object, project_collectors: tuple[str, ...], rwhere: str
) -> tuple[str, ...]:
    """Resolve and validate one recipient's `signals` filter (D5).

    Args:
        raw: The raw value under the recipient's `signals` key, or None when absent.
        project_collectors: The project's enabled collector names (the allowed set
            and the default).
        rwhere: A locating string for error messages (e.g. "… recipient #2").

    Returns:
        The recipient's signal subset as a tuple, in the order given. When the key
        is absent, the project's full collector set (the recipient gets everything).

    Why:
        Pulling this out keeps _parse_recipients readable and gives the subset rule
        one home. The default is "everything" so every pre-D5 config — which has no
        `signals` key — keeps delivering all signals to all recipients, unchanged.
        When the key IS present we require a non-empty list whose every entry is a
        real project collector: an empty list (a recipient that receives nothing)
        is almost certainly a mistake, and a signal the project does not collect
        could never be delivered, so both fail loudly at load time naming the fix —
        far better than a recipient silently receiving nothing at send time.
    """
    # Absent -> everything (the backward-compatible default).
    if raw is None:
        return project_collectors

    if not isinstance(raw, list) or not raw:
        raise ConfigError(
            f"{rwhere} has an invalid `signals` (must be a non-empty list of "
            f"collector names, a subset of this project's collectors "
            f"{project_collectors}). Omit it to receive all signals."
        )

    signals: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            raise ConfigError(
                f"{rwhere} has a `signals` entry that is not a non-empty string."
            )
        signal = entry.strip()
        if signal not in project_collectors:
            raise ConfigError(
                f"{rwhere} lists signal {signal!r}, which this project does not "
                f"collect. Choose from {project_collectors}, or enable that "
                f"collector for the project."
            )
        signals.append(signal)

    return tuple(signals)


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

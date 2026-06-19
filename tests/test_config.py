# =============================================================================
# tests/test_config.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying that config loading resolves defaults correctly and
#                  rejects malformed configs with clear ConfigError messages.
# Role in project: Config is the first gate of every run; a bad config should
#                  fail loudly here, not confusingly downstream. These tests pin
#                  that behavior.
# =============================================================================

from pathlib import Path

import pytest

from orion.config import ConfigError, get_project, load_config


def _write(tmp_path: Path, text: str) -> Path:
    """Write a config file into a temp dir and return its path.

    Args:
        tmp_path: pytest's per-test temporary directory.
        text: TOML content to write.

    Returns:
        Path to the written orion.toml.

    Why:
        Every test needs a throwaway config on disk; this keeps each test to the
        one TOML snippet it cares about instead of repeating file I/O (DRY).
    """
    path = tmp_path / "orion.toml"
    path.write_text(text)
    return path


def test_valid_config_resolves_defaults(tmp_path):
    """A minimal valid config loads, applies defaults, and expands paths.

    Why this matters: this is the happy path the whole pipeline depends on —
    if defaults (share_level, collectors) and path resolution are wrong, every
    later step gets bad inputs.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )

    config = load_config(path)
    project = get_project(config, "demo")

    assert project.repo_path == Path("/tmp/demo")
    # Defaults: safest share level and the git collector.
    assert project.share_level == "high_level"
    assert project.collectors == ("git",)
    # auto_send defaults to False: a project is opt-in for unattended delivery,
    # so an omitted field can never silently enable a preview-less send.
    assert project.auto_send is False
    assert project.recipients[0].name == "Alex"
    # state_db default is resolved next to the config file, as an absolute path.
    assert config.state_db == (tmp_path / "orion.sqlite3").resolve()


def test_missing_file_raises(tmp_path):
    """A nonexistent config path gives a clear ConfigError, not FileNotFoundError.

    Why this matters: a new user who hasn't copied the example should be told to
    do exactly that.
    """
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "does_not_exist.toml")


def test_no_projects_raises(tmp_path):
    """A config with no [projects.*] tables is rejected.

    Why this matters: an empty registry is a setup mistake; reporting nothing
    silently would hide it.
    """
    path = _write(tmp_path, 'state_db = "orion.sqlite3"\n')
    with pytest.raises(ConfigError, match="no projects"):
        load_config(path)


def test_missing_repo_path_raises(tmp_path):
    """A project without repo_path fails — there is nothing to report on.

    Why this matters: repo_path is the one truly required project field.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    with pytest.raises(ConfigError, match="repo_path"):
        load_config(path)


def test_invalid_share_level_raises(tmp_path):
    """An unknown share_level is rejected so the privacy dial can't be mis-set.

    Why this matters: share_level controls how much detail leaves the machine; a
    typo here is a security-relevant mistake, so it must fail, not default away.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"
        share_level = "everything"

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    with pytest.raises(ConfigError, match="share_level"):
        load_config(path)


def test_auto_send_true_parses(tmp_path):
    """`auto_send = true` is parsed as a real boolean True.

    Why this matters: this is the opt-in that (together with `--yes`) allows an
    unattended run to skip the human preview. If it didn't parse to True, the
    unattended path could never engage; if it parsed loosely, the privacy gate
    would rest on a fuzzy value. We pin the exact boolean.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"
        auto_send = true

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    project = get_project(load_config(path), "demo")
    assert project.auto_send is True


def test_auto_send_false_parses(tmp_path):
    """`auto_send = false` is parsed as a real boolean False.

    Why this matters: the explicit opt-OUT must read back as False, distinct from
    the omitted-field default (which is also False) — a user writing it out should
    get exactly what they wrote, not a coincidental default.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"
        auto_send = false

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    project = get_project(load_config(path), "demo")
    assert project.auto_send is False


def test_auto_send_invalid_type_raises(tmp_path):
    """A non-boolean auto_send is rejected rather than coerced.

    Why this matters: a privacy-relevant switch must not be truthy-by-accident. A
    string like "yes" or an int like 1 would be "truthy" in Python and could
    quietly authorize a preview-less send; isinstance(x, bool) catches that here,
    at load time, with a message that names the fix.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"
        auto_send = "yes"

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    with pytest.raises(ConfigError, match="auto_send"):
        load_config(path)


def test_no_recipients_raises(tmp_path):
    """A project with no recipients is rejected — delivery would be impossible.

    Why this matters: catching this at load time points at the config, far from
    the eventual send step.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"
        """,
    )
    with pytest.raises(ConfigError, match="recipients"):
        load_config(path)


def test_unknown_collector_still_rejected(tmp_path):
    """An unsupported collector name is rejected (regression for the new list).

    Why this matters: expanding SUPPORTED_COLLECTORS to add tasks/notes must not
    accidentally start accepting arbitrary names — a typo'd collector should
    still fail loudly rather than silently produce no signal.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"
        collectors = ["git", "todos"]

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    with pytest.raises(ConfigError, match="todos"):
        load_config(path)


def test_tasks_enabled_requires_tasks_file(tmp_path):
    """Enabling the tasks collector without a tasks_file is a clear ConfigError.

    Why this matters: the collector has nothing to read without a path; catching
    it at load time names the exact key to add, instead of failing mid-run.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"
        collectors = ["git", "tasks"]

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    with pytest.raises(ConfigError, match="tasks_file"):
        load_config(path)


def test_notes_enabled_requires_notes_file(tmp_path):
    """Enabling the notes collector without a notes_file is a clear ConfigError.

    Why this matters: same pairing guarantee as tasks — an enabled file collector
    must have its file configured.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"
        collectors = ["notes"]

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    with pytest.raises(ConfigError, match="notes_file"):
        load_config(path)


def test_collector_file_relative_resolved_to_config_dir(tmp_path):
    """A relative tasks_file resolves against the config file's directory.

    Why this matters: a user should be able to write `tasks_file = "TODO.md"` and
    have it found next to their config, regardless of the working directory the
    command runs from (mirrors how state_db resolves).
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"
        collectors = ["tasks"]
        tasks_file = "TODO.md"

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    project = get_project(load_config(path), "demo")
    assert project.tasks_file == (tmp_path / "TODO.md").resolve()
    # The disabled collector's path stays None.
    assert project.notes_file is None


def test_collector_file_ignored_when_collector_disabled(tmp_path):
    """A tasks_file present but the collector disabled is ignored, not an error.

    Why this matters: a user may keep a path in config while toggling the
    collector off; that should not fail, and the resolved field should be None so
    the orchestrator never runs a disabled collector.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"
        collectors = ["git"]
        tasks_file = "TODO.md"

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    project = get_project(load_config(path), "demo")
    assert project.tasks_file is None


def test_slack_recipient_is_accepted(tmp_path):
    """A recipient on the "slack" channel loads (Phase 3 widened the set).

    Why this matters: Slack delivery is pointless if config rejects a slack
    recipient; this pins that the channel is now valid.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"

        [[projects.demo.recipients]]
        name = "Sam"
        channel = "slack"
        webhook_env_var = "ORION_SLACK_WEBHOOK_SAM"
        """,
    )
    project = get_project(load_config(path), "demo")
    assert project.recipients[0].channel == "slack"


def test_unknown_channel_is_rejected(tmp_path):
    """A recipient on an unsupported channel fails with a clear ConfigError.

    Why this matters: widening SUPPORTED_CHANNELS to add slack must not start
    accepting arbitrary channel names — a typo ("slak") should still fail loudly
    rather than silently never deliver.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"

        [[projects.demo.recipients]]
        name = "Sam"
        channel = "slak"
        webhook_env_var = "ORION_SLACK_WEBHOOK_SAM"
        """,
    )
    with pytest.raises(ConfigError, match="channel"):
        load_config(path)


def test_summarizer_defaults_to_anthropic_haiku(tmp_path):
    """A config with no [summarizer] table resolves to Anthropic/Haiku.

    Why this matters: B4 must be backward-compatible — every existing orion.toml
    has no [summarizer] table, and those configs must keep summarizing the git
    lane with the lightest adequate model exactly as before. If the default
    drifted, an upgrade would silently change behavior.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    config = load_config(path)
    assert config.summarizer.provider == "anthropic"
    assert config.summarizer.model == "claude-haiku-4-5"
    # The local-only fields stay None for the Anthropic backend.
    assert config.summarizer.base_url is None
    assert config.summarizer.api_key_env is None


def test_summarizer_anthropic_model_override(tmp_path):
    """An explicit Anthropic model (e.g. stepping up to Sonnet) is honored.

    Why this matters: the "lightest adequate model" default can be overridden when
    Haiku misses nuance on real diffs — the whole point of B4's flexibility. We
    pin that a provided model replaces the default.
    """
    path = _write(
        tmp_path,
        """
        [summarizer]
        provider = "anthropic"
        model = "claude-sonnet-4-6"

        [projects.demo]
        repo_path = "/tmp/demo"

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    config = load_config(path)
    assert config.summarizer.provider == "anthropic"
    assert config.summarizer.model == "claude-sonnet-4-6"


def test_summarizer_local_backend_parses(tmp_path):
    """A valid local backend (base_url + model + optional key) loads.

    Why this matters: the local backend is the proof the seam is real and the
    local-first privacy payoff. We pin that base_url, model, and the optional
    api_key_env all resolve onto the SummarizerConfig the CLI will build from.
    """
    path = _write(
        tmp_path,
        """
        [summarizer]
        provider = "local"
        base_url = "http://localhost:11434/v1"
        model = "llama3.1"
        api_key_env = "LOCAL_LLM_KEY"

        [projects.demo]
        repo_path = "/tmp/demo"

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    config = load_config(path)
    assert config.summarizer.provider == "local"
    assert config.summarizer.base_url == "http://localhost:11434/v1"
    assert config.summarizer.model == "llama3.1"
    assert config.summarizer.api_key_env == "LOCAL_LLM_KEY"


def test_summarizer_local_without_api_key_is_fine(tmp_path):
    """A local backend with no api_key_env loads (most local servers need none).

    Why this matters: requiring a key for a local model would defeat the privacy
    point and block the common Ollama/llama.cpp case. The absence must be valid,
    leaving api_key_env None so the CLI knows no key is required.
    """
    path = _write(
        tmp_path,
        """
        [summarizer]
        provider = "local"
        base_url = "http://localhost:11434/v1"
        model = "llama3.1"

        [projects.demo]
        repo_path = "/tmp/demo"

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    config = load_config(path)
    assert config.summarizer.api_key_env is None


def test_summarizer_unknown_provider_rejected(tmp_path):
    """An unsupported provider name fails loudly with the allowed set.

    Why this matters: a typo'd provider ("anthropc") should be a five-second fix
    at load time, not a confusing failure when the summarizer is built mid-run.
    """
    path = _write(
        tmp_path,
        """
        [summarizer]
        provider = "openai"

        [projects.demo]
        repo_path = "/tmp/demo"

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    with pytest.raises(ConfigError, match="provider"):
        load_config(path)


def test_summarizer_local_without_base_url_rejected(tmp_path):
    """provider='local' without a base_url is rejected.

    Why this matters: the local backend has nothing to POST to without an
    endpoint URL; catching it here names the exact key to add instead of failing
    with a confusing connection error at send time.
    """
    path = _write(
        tmp_path,
        """
        [summarizer]
        provider = "local"
        model = "llama3.1"

        [projects.demo]
        repo_path = "/tmp/demo"

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    with pytest.raises(ConfigError, match="base_url"):
        load_config(path)


def test_summarizer_local_without_model_rejected(tmp_path):
    """provider='local' without a model is rejected (no universal default).

    Why this matters: unlike Anthropic (which defaults to Haiku), there is no
    sensible default local model name, so we require an explicit one rather than
    guess — a wrong guess would fail confusingly at the endpoint.
    """
    path = _write(
        tmp_path,
        """
        [summarizer]
        provider = "local"
        base_url = "http://localhost:11434/v1"

        [projects.demo]
        repo_path = "/tmp/demo"

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    with pytest.raises(ConfigError, match="model"):
        load_config(path)


def test_unknown_project_lists_known(tmp_path):
    """Requesting a missing project lists the ones that exist.

    Why this matters: turns a typo'd project name into a one-glance fix.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    config = load_config(path)
    with pytest.raises(ConfigError, match="demo"):
        get_project(config, "typo")


# A minimal valid [projects.demo] body reused by the relay tests below, so each
# relay test contributes only the [relay] snippet it is actually checking (DRY).
_DEMO_PROJECT = """
[projects.demo]
repo_path = "/tmp/demo"

[[projects.demo.recipients]]
name = "Alex"
channel = "discord"
webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
"""


def test_relay_absent_defaults_to_disabled(tmp_path):
    """A config with no [relay] table resolves to a disabled, empty relay.

    Why this matters: the relay is opt-in and C1 must not change any existing
    config's behavior. An absent table must mean "push nowhere" — disabled with no
    url/token — so every pre-C1 config keeps delivering exactly as before.
    """
    config = load_config(_write(tmp_path, _DEMO_PROJECT))

    assert config.relay.enabled is False
    assert config.relay.url == ""
    assert config.relay.token_env_var == ""


def test_relay_disabled_ignores_other_fields(tmp_path):
    """`enabled = false` is a no-op even if url/token are present.

    Why this matters: a user toggling the relay off (rather than deleting the
    table) should fully disable it without having to also remove url/token. We pin
    that a disabled table is a pure no-op and does not require — or surface — its
    other fields.
    """
    config = load_config(
        _write(
            tmp_path,
            """
            [relay]
            enabled = false
            url = "http://127.0.0.1:8787/ingest"
            token_env_var = "ORION_RELAY_TOKEN"
            """
            + _DEMO_PROJECT,
        )
    )

    assert config.relay.enabled is False
    # Ignored when disabled: the no-op config carries no destination/token.
    assert config.relay.url == ""
    assert config.relay.token_env_var == ""


def test_relay_enabled_parses(tmp_path):
    """A fully-specified enabled relay parses into the expected RelayConfig.

    Why this matters: this is the happy path CP3/CP4 build on — the CLI reads
    url + token_env_var straight off this object to POST the blob, so they must
    survive parsing verbatim (whitespace-trimmed).
    """
    config = load_config(
        _write(
            tmp_path,
            """
            [relay]
            enabled = true
            url = "http://127.0.0.1:8787/ingest"
            token_env_var = "ORION_RELAY_TOKEN"
            """
            + _DEMO_PROJECT,
        )
    )

    assert config.relay.enabled is True
    assert config.relay.url == "http://127.0.0.1:8787/ingest"
    assert config.relay.token_env_var == "ORION_RELAY_TOKEN"


def test_relay_enabled_without_url_rejected(tmp_path):
    """An enabled relay with no url fails loudly naming the missing key.

    Why this matters: a relay with nothing to POST to would fail confusingly at
    push time; catching it at load time turns it into a five-second config fix.
    """
    path = _write(
        tmp_path,
        """
        [relay]
        enabled = true
        token_env_var = "ORION_RELAY_TOKEN"
        """
        + _DEMO_PROJECT,
    )
    with pytest.raises(ConfigError, match="url"):
        load_config(path)


def test_relay_enabled_without_token_rejected(tmp_path):
    """An enabled relay with no token_env_var fails loudly naming the missing key.

    Why this matters: the ingest endpoint is Bearer-authenticated, so an enabled
    relay with no token would always be rejected (401) at push time. Requiring the
    token's env-var name up front surfaces the gap at the config, not the wire.
    """
    path = _write(
        tmp_path,
        """
        [relay]
        enabled = true
        url = "http://127.0.0.1:8787/ingest"
        """
        + _DEMO_PROJECT,
    )
    with pytest.raises(ConfigError, match="token_env_var"):
        load_config(path)


def test_relay_enabled_non_bool_rejected(tmp_path):
    """`enabled` must be a real boolean, not a truthy int/string.

    Why this matters: TOML `enabled = 1` or `enabled = "yes"` silently treated as
    truthy would turn the relay on unexpectedly. The strict isinstance(bool) check
    (same as auto_send) rejects it so enabling the relay is always an explicit
    `true`.
    """
    path = _write(
        tmp_path,
        """
        [relay]
        enabled = 1
        url = "http://127.0.0.1:8787/ingest"
        token_env_var = "ORION_RELAY_TOKEN"
        """
        + _DEMO_PROJECT,
    )
    with pytest.raises(ConfigError, match="enabled"):
        load_config(path)


def test_relay_token_env_var_rejects_a_pasted_value(tmp_path):
    """A token VALUE in `token_env_var` (not the variable name) is rejected — and the
    error never echoes the value.

    Why this matters: pasting the secret where the NAME belongs is a real footgun (it
    happened on the first Fly deploy). It must fail at config load with a clear message,
    and must NOT print the secret back — the old "secret '<value>' is not set" path leaked
    it. The sample has a leading digit and a hyphen, which a real env-var name cannot.
    """
    leaked = "0CoZb-jeUavOrr_uy0KCnK9pYtDCqzJhZJKGSGqDiTI"
    path = _write(
        tmp_path,
        f"""
        [relay]
        enabled = true
        url = "https://relay.example.com/ingest"
        token_env_var = "{leaked}"
        """
        + _DEMO_PROJECT,
    )
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    msg = str(exc.value)
    assert "token_env_var" in msg
    assert "environment variable name" in msg
    assert leaked not in msg  # the secret must NOT be echoed back


def test_recipient_webhook_env_var_rejects_a_pasted_url(tmp_path):
    """A webhook URL pasted into `webhook_env_var` (not the variable name) is rejected,
    without echoing it.

    Why this matters: the same footgun as the relay token — a user pastes the secret
    (here a webhook URL, full of ':' and '/') where the env-var NAME belongs.
    """
    url = "https://hooks.slack.com/services/T00/B00/XXXX"
    path = _write(
        tmp_path,
        f"""
        [projects.demo]
        repo_path = "/tmp/demo"

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "slack"
        webhook_env_var = "{url}"
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    msg = str(exc.value)
    assert "webhook_env_var" in msg
    assert url not in msg


def test_summarizer_api_key_env_rejects_a_pasted_value(tmp_path):
    """A pasted key VALUE in `api_key_env` (local summarizer) is rejected.

    Why this matters: api_key_env is the third *_env_var field; it gets the same
    name-shape guard so the value-vs-name slip can't leak a key here either.
    """
    path = _write(
        tmp_path,
        """
        [summarizer]
        provider = "local"
        model = "llama3.1"
        base_url = "http://localhost:11434/v1"
        api_key_env = "sk-abc-123456"
        """
        + _DEMO_PROJECT,
    )
    with pytest.raises(ConfigError, match="api_key_env"):
        load_config(path)

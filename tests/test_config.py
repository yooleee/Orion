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
    # display_timezone defaults to Pacific (KI-20) so messages match the dashboard.
    assert config.display_timezone == "America/Los_Angeles"


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


def test_checklist_true_with_tasks_collector_parses(tmp_path):
    """`checklist = true` parses to True when the `tasks` collector is enabled.

    Why this matters: the live-checklist toggle is read from tasks_file, so the
    happy path is "tasks enabled + checklist on." We pin that this valid pairing
    resolves to a real boolean True (config does not check the file exists yet).
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"
        collectors = ["git", "tasks"]
        tasks_file = "TODO.md"
        checklist = true

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    project = get_project(load_config(path), "demo")
    assert project.checklist is True


def test_checklist_true_without_tasks_collector_raises(tmp_path):
    """`checklist = true` without the `tasks` collector is a clear ConfigError.

    Why this matters: the live checklist has no source unless the `tasks` collector
    (which resolves tasks_file) is enabled. We reject the contradiction at load time
    with a fixable message rather than discovering it as an empty/crashing read later.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"
        collectors = ["git"]
        checklist = true

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    with pytest.raises(ConfigError, match="checklist"):
        load_config(path)


def test_checklist_true_with_tracker_collector_parses(tmp_path):
    """`checklist = true` is valid when the `tracker` collector (no `tasks`) is enabled.

    Why this matters: E2 Inc 2.6 made the tracker a second checklist source, so a
    tracker-only project (the applications use case: no git, no tasks) must satisfy the
    checklist requirement and resolve its tracker_file absolute.
    """
    path = _write(
        tmp_path,
        """
        [projects.apps]
        repo_path = "/tmp/apps"
        collectors = ["tracker"]
        tracker_file = "to_do.md"
        checklist = true

        [[projects.apps.recipients]]
        name = "Placeholder"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    project = get_project(load_config(path), "apps")
    assert project.checklist is True
    # A relative tracker_file resolves against the config directory, like tasks_file.
    assert project.tracker_file == (tmp_path / "to_do.md").resolve()


def test_tracker_enabled_requires_tracker_file(tmp_path):
    """Enabling the tracker collector without a tracker_file is a clear ConfigError.

    Why this matters: the collector/file pairing is enforced at load time naming the
    exact key to add (`tracker_file`), mirroring tasks/notes/incubator.
    """
    path = _write(
        tmp_path,
        """
        [projects.apps]
        repo_path = "/tmp/apps"
        collectors = ["tracker"]

        [[projects.apps.recipients]]
        name = "Placeholder"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    with pytest.raises(ConfigError, match="tracker_file"):
        load_config(path)


def test_checklist_invalid_type_raises(tmp_path):
    """A non-boolean checklist is rejected rather than coerced.

    Why this matters: like auto_send, this is a switch that exposes user content
    (open/planned items) to the dashboard, so it must not be truthy-by-accident. A
    string "yes" or int 1 is caught by isinstance(x, bool) at load time.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"
        collectors = ["git", "tasks"]
        tasks_file = "TODO.md"
        checklist = "yes"

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    with pytest.raises(ConfigError, match="checklist"):
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


def test_incubator_enabled_requires_incubator_file(tmp_path):
    """Enabling the incubator collector without an incubator_file is a ConfigError.

    Why this matters: the same enabled-collector/file pairing guarantee as tasks and
    notes — D4's fifth collector slots into the generic check, so a typo is caught at
    load time naming `incubator_file`, not mid-run.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"
        collectors = ["incubator"]

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    with pytest.raises(ConfigError, match="incubator_file"):
        load_config(path)


def test_incubator_collector_loads_and_resolves_file(tmp_path):
    """An enabled incubator collector parses and resolves its file path.

    Why this matters: pins that D4's collector is a real SUPPORTED_COLLECTORS entry
    and that its file resolves like the other file-backed collectors (relative to
    the config dir), so a `[projects.incubator]` stanza loads end to end.
    """
    path = _write(
        tmp_path,
        """
        [projects.incubator]
        repo_path = "/tmp/incubator"
        collectors = ["incubator"]
        incubator_file = "index.md"

        [[projects.incubator.recipients]]
        name = "Mentor"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_MENTOR"
        """,
    )
    project = get_project(load_config(path), "incubator")
    assert project.collectors == ("incubator",)
    assert project.incubator_file == (tmp_path / "index.md").resolve()


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


# --- D5: per-recipient signals filter ----------------------------------------


def test_signals_omitted_defaults_to_all_collectors(tmp_path):
    """A recipient with no `signals` key receives every project collector.

    Why this matters: this is the backward-compatible default — every pre-D5 config
    (which has no `signals` line) must keep delivering all signals to all
    recipients. We use a two-collector project so "all" is a real set, not just git.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"
        collectors = ["git", "notes"]
        notes_file = "NOTES.md"

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    project = get_project(load_config(path), "demo")
    # Resolved to the project's full collector set, in collector order.
    assert project.recipients[0].signals == ("git", "notes")


def test_signals_subset_is_kept(tmp_path):
    """An explicit `signals` subset is parsed as given, in the order written.

    Why this matters: D5's whole point is that a recipient can receive a SLICE of
    the report — here, only git — so the subset must survive load intact.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"
        collectors = ["git", "notes"]
        notes_file = "NOTES.md"

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        signals = ["git"]
        """,
    )
    project = get_project(load_config(path), "demo")
    assert project.recipients[0].signals == ("git",)


def test_signals_unknown_value_rejected(tmp_path):
    """A `signals` entry the project does not collect fails with a clear error.

    Why this matters: a recipient asking for a signal the project never collects
    could only ever receive nothing — almost certainly a typo. Failing loudly at
    load time (naming the bad value) beats a silently-empty delivery at send time.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"
        collectors = ["git"]

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        signals = ["tasks"]
        """,
    )
    # The message should name the offending signal so the fix is obvious.
    with pytest.raises(ConfigError, match="tasks"):
        load_config(path)


def test_signals_empty_list_rejected(tmp_path):
    """An empty `signals = []` is rejected rather than meaning "receive nothing".

    Why this matters: a recipient that receives nothing is pointless and almost
    certainly a mistake; the user should OMIT the key to mean "everything", not
    write an empty list. We reject it loudly to catch that confusion at load time.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"
        collectors = ["git"]

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        signals = []
        """,
    )
    with pytest.raises(ConfigError, match="signals"):
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


def test_relay_admin_token_env_var_parses(tmp_path):
    """An enabled relay with admin_token_env_var parses it onto the RelayConfig (C3).

    Why this matters: the `relay-user` provisioning commands read the admin token's
    env-var NAME from here, so it must survive parsing verbatim (whitespace-trimmed),
    exactly like token_env_var.
    """
    config = load_config(
        _write(
            tmp_path,
            """
            [relay]
            enabled = true
            url = "http://127.0.0.1:8787/ingest"
            token_env_var = "ORION_RELAY_TOKEN"
            admin_token_env_var = "ORION_RELAY_ADMIN_TOKEN"
            """
            + _DEMO_PROJECT,
        )
    )

    assert config.relay.admin_token_env_var == "ORION_RELAY_ADMIN_TOKEN"


def test_relay_admin_token_env_var_optional_defaults_empty(tmp_path):
    """An enabled relay WITHOUT admin_token_env_var defaults it to "" (provisioning off).

    Why this matters: provisioning is an opt-in capability separate from pushing — a
    push-only relay needs no admin token. So omitting it must NOT fail config load; it
    leaves the field empty, and a `relay-user` command then errors clearly only if invoked.
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
    assert config.relay.admin_token_env_var == ""  # absent -> empty, not an error


def test_relay_admin_token_env_var_rejects_a_pasted_value(tmp_path):
    """A token VALUE in admin_token_env_var (not the NAME) is rejected, without echoing it.

    Why this matters: the same footgun as token_env_var — pasting the admin secret where
    the env-var NAME belongs. It must fail at config load with a clear message naming the
    key, and must never print the secret back. The sample has a leading digit + hyphen,
    which a legal env-var name cannot contain.
    """
    leaked = "9XyZb-adminTokenValue_not_a_name_AAAA"
    path = _write(
        tmp_path,
        f"""
        [relay]
        enabled = true
        url = "https://relay.example.com/ingest"
        token_env_var = "ORION_RELAY_TOKEN"
        admin_token_env_var = "{leaked}"
        """
        + _DEMO_PROJECT,
    )
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    msg = str(exc.value)
    assert "admin_token_env_var" in msg
    assert leaked not in msg  # the secret must NOT be echoed back


def test_load_relay_config_parses_relay_without_any_projects(tmp_path):
    """load_relay_config returns the [relay] table from a config that has NO projects.

    Why this matters: the `relay-user` commands talk only to the relay, so they must load
    the relay config from a config that legitimately has no `[projects.<name>]` (an
    admin-only operator). Full load_config would raise "defines no projects"; this focused
    loader must not, while still validating the [relay] table identically.
    """
    from orion.config import load_relay_config

    path = _write(
        tmp_path,
        """
        [relay]
        enabled = true
        url = "http://127.0.0.1:8787/ingest"
        token_env_var = "ORION_RELAY_TOKEN"
        admin_token_env_var = "ORION_RELAY_ADMIN_TOKEN"
        """,
    )  # NOTE: no [projects.*] table at all

    # Full load_config rejects the no-projects config...
    with pytest.raises(ConfigError, match="no projects"):
        load_config(path)

    # ...but the focused relay loader parses the [relay] table fine.
    relay = load_relay_config(path)
    assert relay.enabled is True
    assert relay.url == "http://127.0.0.1:8787/ingest"
    assert relay.admin_token_env_var == "ORION_RELAY_ADMIN_TOKEN"


def test_load_relay_config_still_validates_the_relay_table(tmp_path):
    """load_relay_config applies the SAME [relay] validation as full load_config.

    Why this matters: skipping the project requirement must not skip relay validation — a
    pasted secret in admin_token_env_var (the NAME field) must still fail loudly, without
    echoing the value.
    """
    from orion.config import load_relay_config

    leaked = "9XyZ-pasted-admin-secret-not-a-name"
    path = _write(
        tmp_path,
        f"""
        [relay]
        enabled = true
        url = "http://127.0.0.1:8787/ingest"
        token_env_var = "ORION_RELAY_TOKEN"
        admin_token_env_var = "{leaked}"
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_relay_config(path)
    assert "admin_token_env_var" in str(exc.value)
    assert leaked not in str(exc.value)


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


def test_display_timezone_absent_defaults_to_pacific(tmp_path):
    """A config with no display_timezone resolves to the Pacific default (KI-20).

    Why this matters: the field is opt-in and must not change existing configs'
    behavior beyond the intended fix — an absent key means messages render in the
    same Pacific zone the dashboard already uses, so the two surfaces agree by
    default and every pre-KI-20 config keeps working.
    """
    config = load_config(_write(tmp_path, _DEMO_PROJECT))
    assert config.display_timezone == "America/Los_Angeles"


def test_display_timezone_override_parses(tmp_path):
    """An explicit, valid IANA zone is accepted and carried onto the Config.

    Why this matters: a user with non-Pacific recipients can choose their zone (or
    UTC). A valid name must round-trip onto the Config so compose renders in it.
    """
    path = _write(tmp_path, 'display_timezone = "UTC"\n' + _DEMO_PROJECT)
    assert load_config(path).display_timezone == "UTC"


def test_display_timezone_invalid_zone_raises(tmp_path):
    """An unknown/garbage zone name fails loudly at load time, naming the value.

    Why this matters: the zone drives timestamp rendering on the pre-send path, so a
    typo must be a clear five-second fix at load — not a confusing time, or a crash
    deep in delivery. We validate by actually constructing the ZoneInfo, so "valid
    here" means "usable there."
    """
    path = _write(tmp_path, 'display_timezone = "Mars/Phobos"\n' + _DEMO_PROJECT)
    with pytest.raises(ConfigError, match="display_timezone"):
        load_config(path)


# --- C2-bots: the [bot] table (native two-way chat bot) -------------------------
#
# The bot is global, opt-in, and writes INTO the relay, so these mirror the [relay]
# tests: absent/disabled is a no-op; enabled requires both token env-var NAMES and at
# least one channel→project binding; a binding to an unknown project, a pasted secret,
# or a non-bool `enabled` all fail loudly at load time.

# A full, valid [bot] block reused below so each test contributes only the deviation
# it is checking (DRY). It binds one channel to the demo project that _DEMO_PROJECT
# defines, so the project-existence validation passes.
_VALID_BOT = """
[bot]
enabled = true
platform = "slack"
token_env_var = "ORION_SLACK_BOT_TOKEN"
app_token_env_var = "ORION_SLACK_APP_TOKEN"

[[bot.channels]]
channel_id = "C07ABC123"
project = "demo"
"""


def test_bot_absent_defaults_to_disabled(tmp_path):
    """A config with no [bot] table resolves to a disabled, empty BotConfig.

    Why this matters: the bot is opt-in and must not change any existing config's
    behavior. An absent table means "run no bot" — disabled with empty fields — so
    every pre-bots config keeps working exactly as before.
    """
    config = load_config(_write(tmp_path, _DEMO_PROJECT))

    assert config.bot.enabled is False
    assert config.bot.platform == ""
    assert config.bot.token_env_var == ""
    assert config.bot.app_token_env_var == ""
    assert config.bot.channel_bindings == ()


def test_bot_disabled_ignores_other_fields(tmp_path):
    """`enabled = false` is a no-op even if tokens/channels are present.

    Why this matters: toggling the bot off (rather than deleting the table) should
    fully disable it without having to also strip its other fields — a disabled table
    is a pure no-op that surfaces none of them.
    """
    config = load_config(
        _write(
            tmp_path,
            _VALID_BOT.replace("enabled = true", "enabled = false") + _DEMO_PROJECT,
        )
    )

    assert config.bot.enabled is False
    assert config.bot.channel_bindings == ()


def test_bot_enabled_parses(tmp_path):
    """A fully-specified enabled bot parses into the expected BotConfig.

    Why this matters: this is the happy path cmd_bot builds on — it reads the platform,
    the two token env-var names, and the channel→project map straight off this object,
    so they must survive parsing verbatim (whitespace-trimmed) and the bindings must
    become an ordered (channel_id, project) tuple.
    """
    config = load_config(_write(tmp_path, _VALID_BOT + _DEMO_PROJECT))

    assert config.bot.enabled is True
    assert config.bot.platform == "slack"
    assert config.bot.token_env_var == "ORION_SLACK_BOT_TOKEN"
    assert config.bot.app_token_env_var == "ORION_SLACK_APP_TOKEN"
    assert config.bot.channel_bindings == (("C07ABC123", "demo"),)


def test_bot_enabled_missing_bot_token_rejected(tmp_path):
    """An enabled bot with no token_env_var fails loudly naming the missing key.

    Why this matters: without the bot token the listener cannot authenticate to Slack;
    catching it at load time is a five-second fix instead of a confusing runtime auth
    failure.
    """
    path = _write(
        tmp_path, _VALID_BOT.replace('token_env_var = "ORION_SLACK_BOT_TOKEN"\n', "") + _DEMO_PROJECT
    )
    with pytest.raises(ConfigError, match="token_env_var"):
        load_config(path)


def test_bot_enabled_missing_app_token_rejected(tmp_path):
    """An enabled bot with no app_token_env_var fails loudly naming the missing key.

    Why this matters: Socket Mode needs the app-level token to open its WebSocket — it
    is distinct from the bot token. A bot configured with only the bot token would fail
    to connect, so requiring both at load time prevents a confusing startup failure.
    """
    path = _write(
        tmp_path,
        _VALID_BOT.replace('app_token_env_var = "ORION_SLACK_APP_TOKEN"\n', "") + _DEMO_PROJECT,
    )
    with pytest.raises(ConfigError, match="app_token_env_var"):
        load_config(path)


def test_bot_enabled_without_channels_rejected(tmp_path):
    """An enabled bot with no [[bot.channels]] fails loudly.

    Why this matters: a bot bound to no channels would listen to nothing — almost
    certainly a mistake. Requiring at least one binding makes "enabled" mean "actually
    doing something."
    """
    # Drop the [[bot.channels]] block, keeping the table header + tokens.
    bot_no_channels = _VALID_BOT.split("[[bot.channels]]")[0]
    path = _write(tmp_path, bot_no_channels + _DEMO_PROJECT)
    with pytest.raises(ConfigError, match="no channels"):
        load_config(path)


def test_bot_channel_bound_to_unknown_project_rejected(tmp_path):
    """A channel bound to a project that does not exist fails loudly naming it.

    Why this matters: a typo'd project name would otherwise silently relay a
    supervisor's reply to the wrong (or nonexistent) project. Validating the binding
    against the real project names catches it at load, before the bot is running.
    """
    path = _write(
        tmp_path, _VALID_BOT.replace('project = "demo"', 'project = "typo"') + _DEMO_PROJECT
    )
    with pytest.raises(ConfigError, match="unknown project"):
        load_config(path)


def test_bot_enabled_non_bool_rejected(tmp_path):
    """`enabled = 1` (int) is rejected — enabling the bot must be an explicit bool.

    Why this matters: a truthy non-bool would turn the always-on bot on unexpectedly.
    The strict isinstance(bool) check (same as [relay] and auto_send) makes enabling it
    deliberate.
    """
    path = _write(tmp_path, _VALID_BOT.replace("enabled = true", "enabled = 1") + _DEMO_PROJECT)
    with pytest.raises(ConfigError, match="enabled"):
        load_config(path)


def test_bot_unsupported_platform_rejected(tmp_path):
    """An unknown platform fails loudly naming the supported set.

    Why this matters: only Slack is supported in this slice; a config naming another
    platform must fail at load (naming what IS supported) rather than start a bot that
    has no listener for that platform.
    """
    path = _write(
        tmp_path, _VALID_BOT.replace('platform = "slack"', 'platform = "irc"') + _DEMO_PROJECT
    )
    with pytest.raises(ConfigError, match="platform"):
        load_config(path)


def test_bot_token_env_var_rejects_a_pasted_value(tmp_path):
    """A token VALUE in `token_env_var` (not the variable name) is rejected, unechoed.

    Why this matters: the same footgun as the relay token — pasting the secret where
    the NAME belongs. The bot token field gets the same name-shape guard, and the error
    must not echo the pasted secret (the sample has a hyphen a real env-var name can't).
    """
    leaked = "xoxb-1234567890-abcdEFGHijklMNOP"
    path = _write(
        tmp_path, _VALID_BOT.replace('"ORION_SLACK_BOT_TOKEN"', f'"{leaked}"') + _DEMO_PROJECT
    )
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    msg = str(exc.value)
    assert "token_env_var" in msg
    assert leaked not in msg  # the secret must NOT be echoed back


# --- E2 Inc 4: the project/tracker `kind` flag --------------------------------------


def test_kind_defaults_to_project(tmp_path):
    """An omitted `kind` defaults to "project" — the common case.

    Why this matters: most entries are real projects, so the safe default keeps existing
    configs working unchanged and only trackers need to opt in.
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
    assert get_project(load_config(path), "demo").kind == "project"


def test_kind_tracker_loads_with_checklist(tmp_path):
    """A checklist-backed entry may declare kind = "tracker".

    Why this matters: this is how the applications tracker is marked a To-do, not a project.
    It pairs with `checklist` + a checklist collector (the carrier the kind rides on).
    """
    (tmp_path / "TODO.md").write_text("- [ ] Apply\n", encoding="utf-8")
    path = _write(
        tmp_path,
        """
        [projects.apps]
        repo_path = "/tmp/apps"
        collectors = ["tasks"]
        checklist = true
        kind = "tracker"
        tasks_file = "TODO.md"

        [[projects.apps.recipients]]
        name = "Family"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    assert get_project(load_config(path), "apps").kind == "tracker"


def test_invalid_kind_raises(tmp_path):
    """An unknown kind value is a clear ConfigError, not a silent unrecognized kind.

    Why this matters: a typo (kind = "tracke") would otherwise sail through and confuse the
    home's split; catching it at load points the user at the exact key.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"
        kind = "tracke"

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    with pytest.raises(ConfigError, match="invalid kind"):
        load_config(path)


def test_tracker_without_checklist_raises(tmp_path):
    """kind = "tracker" without `checklist` is rejected (it would never reach the relay).

    Why this matters: kind rides the checklist push, so a tracker with no checklist would
    silently never appear as one. Failing at load turns that no-op into a fixable error.
    """
    path = _write(
        tmp_path,
        """
        [projects.apps]
        repo_path = "/tmp/apps"
        kind = "tracker"

        [[projects.apps.recipients]]
        name = "Family"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    with pytest.raises(ConfigError, match="checklist"):
        load_config(path)


# --- disciplines collector: discipline_docs parsing (E2 Inc 4 slice 4b) ----------


def test_discipline_docs_resolve_absolute(tmp_path):
    """With the 'disciplines' collector on, discipline_docs resolve next to the config.

    Why this matters: the collector reads these paths, so a relative entry must be made
    absolute against the config dir (mirroring tasks_file), and an absolute entry kept.
    """
    path = _write(
        tmp_path,
        """
        [summarizer]
        provider = "anthropic"

        [projects.demo]
        repo_path = "/tmp/demo"
        collectors = ["git", "disciplines"]
        discipline_docs = ["CLAUDE.md", "/abs/design/README.md"]

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    project = get_project(load_config(path), "demo")
    assert project.discipline_docs == (
        (tmp_path / "CLAUDE.md").resolve(),
        Path("/abs/design/README.md"),
    )


def test_disciplines_enabled_without_docs_raises(tmp_path):
    """Enabling 'disciplines' with no discipline_docs is a config error, caught at load.

    Why this matters: an enabled collector with nothing to read is a setup mistake; we
    fail loudly here with a fixable message rather than producing an empty section.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"
        collectors = ["disciplines"]

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    with pytest.raises(ConfigError, match="discipline_docs"):
        load_config(path)


def test_discipline_docs_ignored_when_collector_off(tmp_path):
    """discipline_docs is () when the 'disciplines' collector is not enabled.

    Why this matters: a stray discipline_docs key without the collector must not turn
    the feature on — only the collector list does. The field defaults to empty.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"
        collectors = ["git"]
        discipline_docs = ["CLAUDE.md"]

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    project = get_project(load_config(path), "demo")
    assert project.discipline_docs == ()


def test_discipline_docs_invalid_entry_raises(tmp_path):
    """A non-string discipline_docs entry is rejected with a clear error.

    Why this matters: a malformed entry would crash the collector later; catching it at
    load keeps the failure locatable.
    """
    path = _write(
        tmp_path,
        """
        [projects.demo]
        repo_path = "/tmp/demo"
        collectors = ["disciplines"]
        discipline_docs = ["ok.md", 42]

        [[projects.demo.recipients]]
        name = "Alex"
        channel = "discord"
        webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """,
    )
    with pytest.raises(ConfigError, match="discipline_docs"):
        load_config(path)

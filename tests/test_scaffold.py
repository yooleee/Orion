# =============================================================================
# tests/test_scaffold.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the write-side stanza builder (scaffold.py) produces
#                  TOML the read side (config.load_config) accepts, and that
#                  recipient-spec parsing validates inputs.
# Role in project: scaffold.render_project_stanza is what `orion add-project`
#                  writes into orion.toml. The strongest guarantee is a ROUND TRIP:
#                  render -> write -> load_config -> compare. Using the real loader
#                  as the oracle means the writer can never drift from the reader.
# =============================================================================

from pathlib import Path

import pytest

from orion.config import ConfigError, Recipient, get_project, load_config
from orion.scaffold import (
    PROJECT_NAME_RE,
    parse_recipient_spec,
    render_project_stanza,
    slugify_project_name,
)


def _recipient() -> Recipient:
    """A throwaway valid recipient used across the rendering tests."""
    return Recipient(
        name="Alex (supervisor)", channel="discord", webhook_env_var="ORION_DISCORD_ALEX"
    )


# --- slugify_project_name -----------------------------------------------------


def test_slugify_typical_idea_title():
    """A normal idea title becomes a lowercase, hyphenated, valid project name.

    Why: `graduate-idea` derives a default project name from an incubator idea title
    that carries spaces/capitals a project name (a TOML bare key) cannot.
    """
    slug = slugify_project_name("VLM Photo Overlay")
    assert slug == "vlm-photo-overlay"
    assert PROJECT_NAME_RE.match(slug)  # the derived slug is always a valid name


def test_slugify_collapses_punctuation_and_trims():
    """Punctuation runs collapse to single hyphens and edges are trimmed.

    Why: titles like "C++ Tool!" must not produce doubled or edge hyphens, which
    would read poorly and (for edges) be ugly though still valid.
    """
    assert slugify_project_name("  C++  Tool! ") == "c-tool"


def test_slugify_returns_none_when_nothing_valid():
    """A title with no usable characters yields None (caller must ask for a name).

    Why: returning None makes "couldn't derive a name" explicit, so the command can
    require --name instead of silently producing an empty/invalid key.
    """
    assert slugify_project_name("!!!") is None
    assert slugify_project_name("   ") is None


# --- parse_recipient_spec -----------------------------------------------------


def test_parse_recipient_spec_happy_path():
    """A well-formed "Name:channel:ENV_VAR" parses into the right fields.

    Why: this is the explicit --recipient path; the three fields must map in order.
    """
    r = parse_recipient_spec("Alex:slack:ORION_SLACK_ALEX")
    assert r == Recipient(name="Alex", channel="slack", webhook_env_var="ORION_SLACK_ALEX")


def test_parse_recipient_spec_strips_whitespace():
    """Surrounding spaces around each field are trimmed.

    Why: users will naturally type "Name : channel : VAR"; that should still work.
    """
    r = parse_recipient_spec(" Alex : discord : ORION_DISCORD_ALEX ")
    assert r == Recipient(name="Alex", channel="discord", webhook_env_var="ORION_DISCORD_ALEX")


def test_parse_recipient_spec_wrong_field_count():
    """A spec without exactly three colon-separated fields is rejected.

    Why: a name containing a colon would silently mis-split, so we require the
    exact shape and say so rather than guess.
    """
    with pytest.raises(ConfigError, match="three colon-separated"):
        parse_recipient_spec("Alex:discord")  # missing the env var field


def test_parse_recipient_spec_bad_channel():
    """An unknown channel is rejected with the supported set named.

    Why: a typo like "slak" must fail at parse, not confusingly at send time.
    """
    with pytest.raises(ConfigError, match="invalid channel"):
        parse_recipient_spec("Alex:slak:ORION_SLACK_ALEX")


def test_parse_recipient_spec_secret_pasted_as_var_name():
    """A webhook URL pasted where the env-var NAME goes is rejected.

    Why: the third field must NAME a .env variable, not be the secret itself; the
    env-var-name check (reused from config) catches the common paste mistake.
    """
    with pytest.raises(ConfigError):
        parse_recipient_spec("Alex:slack:https://hooks.slack.com/xxx")


# --- render_project_stanza: round trips --------------------------------------


def test_render_create_mode_round_trips(tmp_path):
    """A create-mode stanza is a complete, loadable config on its own.

    Why: `add-project` create mode writes a brand-new orion.toml; with_state_db
    must make the file valid standalone. Load it back and check every field.
    """
    repo = tmp_path / "myrepo"
    stanza = render_project_stanza(
        name="myproj",
        repo_path=repo,
        share_level="high_level",
        collectors=("git",),
        recipients=(_recipient(),),
        with_state_db=True,
    )
    cfg_path = tmp_path / "orion.toml"
    cfg_path.write_text(stanza)

    config = load_config(cfg_path)
    project = get_project(config, "myproj")
    assert project.repo_path == repo
    assert project.share_level == "high_level"
    assert project.collectors == ("git",)
    assert project.auto_send is False  # always false on registration
    # The stanza writes no `signals` line, so on load the recipient's signals
    # resolve to the project's collectors (D5: omitting signals = receive all).
    assert project.recipients == (
        Recipient(
            name="Alex (supervisor)",
            channel="discord",
            webhook_env_var="ORION_DISCORD_ALEX",
            signals=("git",),
        ),
    )


def test_render_append_mode_round_trips(tmp_path):
    """An append-mode stanza added after an existing config loads as a 2nd project.

    Why: append mode (with_state_db=False) must produce a fragment that, appended
    to a file that already has state_db + a project, yields a valid 2-project config.
    """
    existing = (
        'state_db = "orion.sqlite3"\n\n'
        "[projects.first]\n"
        f'repo_path = "{(tmp_path / "first").as_posix()}"\n'
        "collectors = [\"git\"]\n\n"
        "[[projects.first.recipients]]\n"
        'name = "Sam"\n'
        'channel = "slack"\n'
        'webhook_env_var = "ORION_SLACK_SAM"\n'
    )
    stanza = render_project_stanza(
        name="second",
        repo_path=tmp_path / "second",
        share_level="detailed",
        collectors=("git",),
        recipients=(_recipient(),),
        with_state_db=False,
    )
    cfg_path = tmp_path / "orion.toml"
    # Mirror the CLI's append: ensure a separating blank line between the two.
    cfg_path.write_text(existing + "\n" + stanza)

    config = load_config(cfg_path)
    assert set(config.projects) == {"first", "second"}
    assert get_project(config, "second").share_level == "detailed"


def test_render_with_tasks_collector_round_trips(tmp_path):
    """Enabling the "tasks" collector writes tasks_file and round-trips.

    Why: a file-backed collector must emit its required path key; the loader pairs
    "collector enabled" with "file given", so the rendered output must satisfy it.
    """
    stanza = render_project_stanza(
        name="withtasks",
        repo_path=tmp_path / "r",
        share_level="high_level",
        collectors=("git", "tasks"),
        recipients=(_recipient(),),
        tasks_file=tmp_path / "TODO.md",
        with_state_db=True,
    )
    cfg_path = tmp_path / "orion.toml"
    cfg_path.write_text(stanza)
    config = load_config(cfg_path)
    project = get_project(config, "withtasks")
    assert project.collectors == ("git", "tasks")
    assert project.tasks_file == (tmp_path / "TODO.md")


def test_render_with_tracker_collector_round_trips(tmp_path):
    """Enabling the "tracker" collector writes tracker_file and round-trips.

    Why this matters: before Unit 2, render_project_stanza had no tracker entry in its
    collector_files map, so the COLLECTOR_FILE_KEYS loop KeyError'd the moment "tracker"
    was enabled (the reason `applications` had to be hand-edited into orion.toml). This
    pins that the path is now emitted and the real loader accepts it.
    """
    stanza = render_project_stanza(
        name="withtracker",
        repo_path=tmp_path / "r",
        share_level="high_level",
        collectors=("git", "tracker"),
        recipients=(_recipient(),),
        tracker_file=tmp_path / "ROADMAP.md",
        with_state_db=True,
    )
    cfg_path = tmp_path / "orion.toml"
    cfg_path.write_text(stanza)
    project = get_project(load_config(cfg_path), "withtracker")
    assert project.collectors == ("git", "tracker")
    assert project.tracker_file == (tmp_path / "ROADMAP.md")


def test_render_with_incubator_collector_round_trips(tmp_path):
    """Enabling the "incubator" collector writes incubator_file and round-trips.

    Why this matters: same KeyError gap as tracker — the incubator collector had no
    collector_files entry. This pins the path is emitted and loads back.
    """
    stanza = render_project_stanza(
        name="withincubator",
        repo_path=tmp_path / "r",
        share_level="high_level",
        collectors=("git", "incubator"),
        recipients=(_recipient(),),
        incubator_file=tmp_path / "index.md",
        with_state_db=True,
    )
    cfg_path = tmp_path / "orion.toml"
    cfg_path.write_text(stanza)
    project = get_project(load_config(cfg_path), "withincubator")
    assert project.collectors == ("git", "incubator")
    assert project.incubator_file == (tmp_path / "index.md")


def test_render_rejects_tracker_collector_without_file(tmp_path):
    """Enabling "tracker" but giving no tracker_file is refused before writing.

    Why this matters: the enabled⇒path-required contract must hold for the newly-wired
    collectors too — a clear render-time error, not a KeyError or a config the loader
    would later reject.
    """
    with pytest.raises(ConfigError, match="tracker.*collector but no tracker_file"):
        render_project_stanza(
            name="bad",
            repo_path=tmp_path / "r",
            share_level="high_level",
            collectors=("git", "tracker"),
            recipients=(_recipient(),),
        )


# --- render_project_stanza: validation --------------------------------------


def test_render_rejects_tasks_collector_without_file(tmp_path):
    """Enabling "tasks" but giving no tasks_file is refused before writing.

    Why: we never write a config the loader would reject; this catches the gap at
    render time with a message naming the missing key.
    """
    with pytest.raises(ConfigError, match="tasks.*collector but no tasks_file"):
        render_project_stanza(
            name="bad",
            repo_path=tmp_path / "r",
            share_level="high_level",
            collectors=("git", "tasks"),
            recipients=(_recipient(),),
        )


def test_render_rejects_invalid_project_name(tmp_path):
    """A name with spaces/dots is rejected (must be a safe TOML bare key).

    Why: an unsafe name would need key-quoting we deliberately don't do; fail early
    and tell the user to pick a simple name.
    """
    with pytest.raises(ConfigError, match="valid project name"):
        render_project_stanza(
            name="my project",
            repo_path=tmp_path / "r",
            share_level="high_level",
            collectors=("git",),
            recipients=(_recipient(),),
        )


def test_render_rejects_unescapable_string(tmp_path):
    """A recipient name containing a double quote is refused, not silently escaped.

    Why: stdlib has no TOML writer; rather than hand-roll an escaper we reject the
    rare value that would need escaping, keeping the emitted TOML provably valid.
    """
    bad = Recipient(name='Alex "the boss"', channel="discord", webhook_env_var="ORION_X")
    with pytest.raises(ConfigError, match="quote, backslash, or control"):
        render_project_stanza(
            name="proj",
            repo_path=tmp_path / "r",
            share_level="high_level",
            collectors=("git",),
            recipients=(bad,),
        )

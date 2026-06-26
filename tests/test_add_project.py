# =============================================================================
# tests/test_add_project.py
# -----------------------------------------------------------------------------
# Responsible for: End-to-end verification of `orion add-project` — the only
#                  command that WRITES orion.toml. Covers create vs append, the
#                  recipient sources (--like / --recipient), cwd inference, the
#                  three write modes (--print / preview / --yes), and the error
#                  paths that must refuse to write.
# Role in project: add-project closes the dogfood's onboarding friction. These
#                  tests pin that it produces a config the real loader accepts,
#                  never writes on an error or a declined preview, and never
#                  rewrites existing content (append-only).
# =============================================================================

from pathlib import Path

from conftest import _answer, _make_repo, _write_config

from orion import cli
from orion.collectors.tasks import snapshot as snapshot_tasks
from orion.config import get_project, load_config


def _run_add(args, config_path):
    """Invoke `orion add-project` with a config path appended (DRY).

    Args:
        args: The add-project arguments before --config.
        config_path: Path to the orion.toml to act on.

    Returns:
        The CLI exit code.
    """
    return cli.main(["add-project", *args, "--config", str(config_path)])


# --- create mode --------------------------------------------------------------


def test_create_mode_writes_minimal_loadable_config(tmp_path):
    """With no config yet, add-project creates a valid one with the new project.

    Why: this is the brand-new-user path; the written file must stand alone
    (state_db + the project) and load cleanly.
    """
    repo = _make_repo(tmp_path)
    cfg = tmp_path / "orion.toml"
    code = _run_add(
        ["demo", "--repo-path", str(repo), "--recipient", "Mom:slack:ORION_SLACK_MOM", "--yes"],
        cfg,
    )
    assert code == 0
    assert cfg.exists()

    config = load_config(cfg)
    project = get_project(config, "demo")
    assert project.repo_path == repo
    assert [r.webhook_env_var for r in project.recipients] == ["ORION_SLACK_MOM"]


def test_explicit_recipient_is_parsed(tmp_path):
    """A --recipient spec maps its three fields onto the written recipient.

    Why: the explicit path must round-trip name/channel/env-var correctly.
    """
    repo = _make_repo(tmp_path)
    cfg = tmp_path / "orion.toml"
    code = _run_add(
        ["demo", "--repo-path", str(repo), "--recipient", "Alex:discord:ORION_DISCORD_ALEX", "--yes"],
        cfg,
    )
    assert code == 0
    r = get_project(load_config(cfg), "demo").recipients[0]
    assert (r.name, r.channel, r.webhook_env_var) == ("Alex", "discord", "ORION_DISCORD_ALEX")


# --- append mode + --like -----------------------------------------------------


def test_append_mode_with_like_copies_recipients(tmp_path):
    """Adding a second project with --like copies the first's recipients.

    Why: the dogfood case — the user already had supervisors configured and wanted
    the new project to reuse them. Append must not disturb the existing project.
    """
    repo = _make_repo(tmp_path)
    cfg = _write_config(tmp_path, repo)  # seeds project "demo" (recipient: Alex/discord)
    code = _run_add(["second", "--repo-path", str(repo), "--like", "demo", "--yes"], cfg)
    assert code == 0

    config = load_config(cfg)
    assert set(config.projects) == {"demo", "second"}
    # second's recipients are a copy of demo's.
    assert get_project(config, "second").recipients == get_project(config, "demo").recipients


def test_append_preserves_existing_content(tmp_path):
    """Append-only: the original config text is left intact, with the stanza added.

    Why: the whole point of append (vs rewrite) is that hand-written comments and
    ordering survive. We check the original text is still a prefix of the result.
    """
    repo = _make_repo(tmp_path)
    cfg = _write_config(tmp_path, repo)
    original = cfg.read_text()
    code = _run_add(["second", "--repo-path", str(repo), "--like", "demo", "--yes"], cfg)
    assert code == 0
    assert cfg.read_text().startswith(original.rstrip("\n"))


# --- inference ----------------------------------------------------------------


def test_infers_name_and_repo_from_cwd(tmp_path, monkeypatch):
    """Run from inside a repo with no name/--repo-path: both are inferred.

    Why: this is the ergonomic win — `cd myproj && orion add-project` registers
    the current project without spelling out its name or path.
    """
    repo = _make_repo(tmp_path, name="myproj")
    cfg = tmp_path / "orion.toml"
    monkeypatch.chdir(repo)
    code = _run_add(["--recipient", "Mom:slack:ORION_SLACK_MOM", "--yes"], cfg)
    assert code == 0

    project = get_project(load_config(cfg), "myproj")  # name inferred from the dir
    assert project.repo_path.name == "myproj"
    assert project.repo_path.is_dir()


# --- write modes --------------------------------------------------------------


def test_print_only_writes_nothing(tmp_path, capsys):
    """--print shows the stanza and creates no file.

    Why: a review/inspect path (and what graduate-idea can use to preview) must be
    side-effect-free.
    """
    repo = _make_repo(tmp_path)
    cfg = tmp_path / "orion.toml"
    code = _run_add(
        ["demo", "--repo-path", str(repo), "--recipient", "Mom:slack:ORION_SLACK_MOM", "--print"],
        cfg,
    )
    assert code == 0
    assert "[projects.demo]" in capsys.readouterr().out
    assert not cfg.exists()  # nothing written


def test_declined_preview_writes_nothing(tmp_path, monkeypatch):
    """Answering 'n' at the preview aborts without touching the config.

    Why: preview-before-write is the safety gate; a decline must change nothing.
    """
    repo = _make_repo(tmp_path)
    cfg = _write_config(tmp_path, repo)
    before = cfg.read_text()
    _answer(monkeypatch, "n")
    code = _run_add(["second", "--repo-path", str(repo), "--like", "demo"], cfg)
    assert code == 0
    assert cfg.read_text() == before  # untouched


def test_confirmed_preview_writes(tmp_path, monkeypatch):
    """Answering 'y' at the preview writes the new project.

    Why: the interactive happy path must actually persist after confirmation.
    """
    repo = _make_repo(tmp_path)
    cfg = _write_config(tmp_path, repo)
    _answer(monkeypatch, "y")
    code = _run_add(["second", "--repo-path", str(repo), "--like", "demo"], cfg)
    assert code == 0
    assert "second" in load_config(cfg).projects


# --- error paths (must refuse to write) --------------------------------------


def test_duplicate_name_errors_and_leaves_config(tmp_path):
    """Re-adding an existing name fails and leaves the config unchanged.

    Why: silently overwriting or duplicating a project would be destructive; the
    user should rename or hand-edit instead.
    """
    repo = _make_repo(tmp_path)
    cfg = _write_config(tmp_path, repo)
    before = cfg.read_text()
    code = _run_add(["demo", "--repo-path", str(repo), "--like", "demo", "--yes"], cfg)
    assert code == 1
    assert cfg.read_text() == before


def test_like_unknown_project_errors(tmp_path):
    """--like pointing at a non-existent project fails with a clear error.

    Why: copying from a project that isn't there is a typo; fail loudly (the loader
    already lists the known names).
    """
    repo = _make_repo(tmp_path)
    cfg = _write_config(tmp_path, repo)
    code = _run_add(["second", "--repo-path", str(repo), "--like", "nope", "--yes"], cfg)
    assert code == 1


def test_no_recipients_errors(tmp_path):
    """Neither --like nor --recipient → refuse (a project needs a recipient).

    Why: we never write a config the loader would reject; guide the user instead.
    """
    repo = _make_repo(tmp_path)
    cfg = tmp_path / "orion.toml"
    code = _run_add(["demo", "--repo-path", str(repo), "--yes"], cfg)
    assert code == 1
    assert not cfg.exists()


def test_like_in_create_mode_errors(tmp_path):
    """--like with no existing config is rejected (nothing to copy from).

    Why: the first project has no sibling to copy; the message points at --recipient.
    """
    repo = _make_repo(tmp_path)
    cfg = tmp_path / "orion.toml"
    code = _run_add(["demo", "--repo-path", str(repo), "--like", "demo", "--yes"], cfg)
    assert code == 1
    assert not cfg.exists()


def test_invalid_project_name_errors(tmp_path):
    """A name that isn't a safe TOML bare key is rejected before writing.

    Why: an unsafe key would need quoting we deliberately avoid; fail with guidance.
    """
    repo = _make_repo(tmp_path)
    cfg = tmp_path / "orion.toml"
    code = _run_add(
        ["bad name", "--repo-path", str(repo), "--recipient", "Mom:slack:ORION_SLACK_MOM", "--yes"],
        cfg,
    )
    assert code == 1
    assert not cfg.exists()


# --- tasks_file bootstrapping (E2 Inc 2.6, Unit B) ----------------------------
# When the tasks collector is enabled without --tasks-file, add-project defaults the
# path to <repo>/TODO.md AND creates a starter checklist there (preview-gated, never
# overwriting). An explicit --tasks-file opts out of creation (config-only, as before).


def test_tasks_enabled_without_file_defaults_and_creates_checklist(tmp_path):
    """tasks + no --tasks-file → tasks_file=<repo>/TODO.md, and the file is created.

    Why this matters: this is the structural fix for "no project has a tasks_file" —
    enabling tasks alone now yields a ready checklist surface, no second flag, and the
    written config loads with that path.
    """
    repo = _make_repo(tmp_path)
    cfg = tmp_path / "orion.toml"
    code = _run_add(
        [
            "demo",
            "--repo-path",
            str(repo),
            "--collectors",
            "git,tasks",
            "--recipient",
            "Mom:slack:ORION_SLACK_MOM",
            "--yes",
        ],
        cfg,
    )
    assert code == 0

    project = get_project(load_config(cfg), "demo")
    assert project.tasks_file == repo / "TODO.md"
    # The starter checklist exists with the format-teaching header, and the snapshot
    # parser finds NO items in it (the usage comment is not a bullet line), so the
    # dashboard checklist starts empty rather than showing a placeholder row.
    todo = repo / "TODO.md"
    assert todo.exists()
    assert todo.read_text(encoding="utf-8").startswith("# TODO")
    assert snapshot_tasks(todo) == ()


def test_explicit_tasks_file_is_not_created(tmp_path):
    """An explicit --tasks-file keeps the prior config-only behavior (no file created).

    Why this matters: passing your own path is the opt-out — add-project records it in
    config but must not create it, so a user who manages the file themselves is
    unaffected by the new defaulting.
    """
    repo = _make_repo(tmp_path)
    cfg = tmp_path / "orion.toml"
    explicit = repo / "MY_TASKS.md"
    code = _run_add(
        [
            "demo",
            "--repo-path",
            str(repo),
            "--collectors",
            "git,tasks",
            "--tasks-file",
            str(explicit),
            "--recipient",
            "Mom:slack:ORION_SLACK_MOM",
            "--yes",
        ],
        cfg,
    )
    assert code == 0
    assert get_project(load_config(cfg), "demo").tasks_file == explicit
    assert not explicit.exists()  # explicit path is config-only, not created


def test_default_tasks_file_never_overwrites_existing(tmp_path):
    """A pre-existing <repo>/TODO.md is referenced, never clobbered.

    Why this matters: file creation is strictly additive (like the config append). A
    user's existing checklist content must survive add-project untouched.
    """
    repo = _make_repo(tmp_path)
    existing = repo / "TODO.md"
    existing.write_text("# TODO\n\n- [x] Pre-existing work\n", encoding="utf-8")
    cfg = tmp_path / "orion.toml"
    code = _run_add(
        [
            "demo",
            "--repo-path",
            str(repo),
            "--collectors",
            "git,tasks",
            "--recipient",
            "Mom:slack:ORION_SLACK_MOM",
            "--yes",
        ],
        cfg,
    )
    assert code == 0
    # Content is exactly what we wrote — the starter seed did not overwrite it.
    assert existing.read_text(encoding="utf-8") == "# TODO\n\n- [x] Pre-existing work\n"


def test_print_only_creates_no_tasks_file(tmp_path, capsys):
    """--print with tasks enabled creates neither the config nor the TODO.md.

    Why this matters: --print is a side-effect-free inspect path; the new file-creation
    write surface must respect it too.
    """
    repo = _make_repo(tmp_path)
    cfg = tmp_path / "orion.toml"
    code = _run_add(
        [
            "demo",
            "--repo-path",
            str(repo),
            "--collectors",
            "git,tasks",
            "--recipient",
            "Mom:slack:ORION_SLACK_MOM",
            "--print",
        ],
        cfg,
    )
    assert code == 0
    assert not cfg.exists()
    assert not (repo / "TODO.md").exists()


def test_declined_preview_creates_no_tasks_file(tmp_path, monkeypatch):
    """Declining the preview creates neither the config nor the TODO.md.

    Why this matters: the file creation rides on the SAME preview gate as the config
    write, so a single 'n' must decline both.
    """
    repo = _make_repo(tmp_path)
    cfg = tmp_path / "orion.toml"
    _answer(monkeypatch, "n")
    code = _run_add(
        [
            "demo",
            "--repo-path",
            str(repo),
            "--collectors",
            "git,tasks",
            "--recipient",
            "Mom:slack:ORION_SLACK_MOM",
        ],
        cfg,
    )
    assert code == 0
    assert not cfg.exists()
    assert not (repo / "TODO.md").exists()

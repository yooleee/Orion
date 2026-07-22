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


def test_next_step_hint_is_a_command_that_actually_parses(tmp_path, capsys):
    """The onboarding hint names a runnable command, not one the parser rejects.

    Why this matters: add-project's closing line is the FIRST thing a new user is told
    to run. It suggested `orion check <project>`, but `check` validates the whole config
    and takes no project argument — so that command exits with
    `unrecognized arguments: <project>`. Found by the DF1 dogfood sweep by simply doing
    what the output said. We parse the suggested command rather than string-matching it,
    so the assertion tracks the real parser instead of today's wording.
    """
    repo = _make_repo(tmp_path)
    cfg = tmp_path / "orion.toml"
    _run_add(
        ["demo", "--repo-path", str(repo), "--recipient", "Mom:slack:ORION_SLACK_MOM", "--yes"],
        cfg,
    )
    hint = [
        line for line in capsys.readouterr().out.splitlines() if line.strip().startswith("Then:")
    ]
    assert len(hint) == 1, "the closing hint should still be there"

    # Turn "  Then: orion check" into the argv a user would actually type.
    argv = hint[0].split("Then:", 1)[1].split()
    assert argv[0] in ("orion", "python"), f"unexpected hint shape: {hint[0]}"
    argv = argv[argv.index("orion") + 1 :]  # drop the program (and any `-m`)

    parser_error = {}
    try:
        cli.main([*argv, "--config", str(cfg)])
    except SystemExit as exc:  # argparse exits 2 on an unparseable command line
        parser_error["code"] = exc.code
    assert parser_error.get("code") != 2, (
        f"add-project suggested a command the parser rejects: {' '.join(argv)}"
    )


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


# --- Unit 2: tracker / incubator file args ------------------------------------
# add-project can now wire the tracker and incubator collectors' file paths, which
# previously KeyError'd in render_project_stanza. Unlike tasks, no file is created.


def test_add_project_wires_tracker_file(tmp_path):
    """--collectors tracker --tracker-file produces a loadable stanza with tracker_file.

    Why this matters: this is the exact command that used to fail (the reason the
    `applications` tracker had to be hand-edited into orion.toml). It must now register
    cleanly, record the path, and NOT create any file (a tracker points at a rich user
    doc the user maintains).
    """
    repo = _make_repo(tmp_path)
    cfg = tmp_path / "orion.toml"
    tracker = repo / "ROADMAP.md"
    code = _run_add(
        [
            "demo",
            "--repo-path",
            str(repo),
            "--collectors",
            "git,tracker",
            "--tracker-file",
            str(tracker),
            "--recipient",
            "Mom:slack:ORION_SLACK_MOM",
            "--yes",
        ],
        cfg,
    )
    assert code == 0
    project = get_project(load_config(cfg), "demo")
    assert project.collectors == ("git", "tracker")
    assert project.tracker_file == tracker
    assert not tracker.exists()  # config-only, never created


def test_add_project_wires_incubator_file(tmp_path):
    """--collectors incubator --incubator-file produces a loadable stanza, file uncreated.

    Why this matters: same newly-wired path as tracker; pins the incubator collector's
    file arg threads through and is config-only.
    """
    repo = _make_repo(tmp_path)
    cfg = tmp_path / "orion.toml"
    index = repo / "index.md"
    code = _run_add(
        [
            "demo",
            "--repo-path",
            str(repo),
            "--collectors",
            "git,incubator",
            "--incubator-file",
            str(index),
            "--recipient",
            "Mom:slack:ORION_SLACK_MOM",
            "--yes",
        ],
        cfg,
    )
    assert code == 0
    project = get_project(load_config(cfg), "demo")
    assert project.collectors == ("git", "incubator")
    assert project.incubator_file == index
    assert not index.exists()


# --- Unit 3: --seed-tasks-from ------------------------------------------------
# When add-project CREATES a defaulted tasks_file, --seed-tasks-from <doc> fills it from
# the doc's Markdown tables (parse, no LLM) instead of the empty starter. A doc with no
# usable table falls back to the starter; the flag is ignored (warned) when no tasks_file
# is being created. Never fails the add.

# A roadmap-shaped doc: a status column with one done (✅) and one open row, plus a
# preamble line that is NOT a table (must be ignored by parse_tables).
_ROADMAP_DOC = (
    "# Roadmap\n"
    "\n"
    "Some intro prose that is not a table.\n"
    "\n"
    "| Scope          | Status        |\n"
    "| -------------- | ------------- |\n"
    "| Ship the thing | ✅ Signed off |\n"
    "| Plan the next  | In progress   |\n"
)


def test_seed_tasks_from_seeds_checklist_with_done_mapping(tmp_path):
    """A roadmap table seeds checkbox lines, mapping its status column to done/open.

    Why this matters: this is the Unit 3 payoff — a doc the user already maintains
    becomes the new project's starting checklist, with the ✅ row checked and the
    in-progress row open, and the written file reads back through the real tasks
    snapshot (so the dashboard would show exactly those items).
    """
    repo = _make_repo(tmp_path)
    doc = tmp_path / "roadmap.md"
    doc.write_text(_ROADMAP_DOC, encoding="utf-8")
    cfg = tmp_path / "orion.toml"
    code = _run_add(
        [
            "demo",
            "--repo-path",
            str(repo),
            "--collectors",
            "git,tasks",
            "--seed-tasks-from",
            str(doc),
            "--recipient",
            "Mom:slack:ORION_SLACK_MOM",
            "--yes",
        ],
        cfg,
    )
    assert code == 0
    todo = repo / "TODO.md"
    assert todo.exists()
    # The real snapshot parser reads the seeded file back as the dashboard would.
    items = snapshot_tasks(todo)
    by_text = {item.text: item.done for item in items}
    assert by_text == {"Ship the thing": True, "Plan the next": False}


def test_seed_tasks_from_no_table_falls_back_to_starter(tmp_path):
    """A doc with no usable table warns and falls back to the empty starter (add succeeds).

    Why this matters: seeding must NEVER fail the add. A doc Orion can't parse into a
    checklist yields the same empty starter as no --seed-tasks-from at all.
    """
    repo = _make_repo(tmp_path)
    doc = tmp_path / "prose.md"
    doc.write_text("# Notes\n\nJust prose, no tables here.\n", encoding="utf-8")
    cfg = tmp_path / "orion.toml"
    code = _run_add(
        [
            "demo",
            "--repo-path",
            str(repo),
            "--collectors",
            "git,tasks",
            "--seed-tasks-from",
            str(doc),
            "--recipient",
            "Mom:slack:ORION_SLACK_MOM",
            "--yes",
        ],
        cfg,
    )
    assert code == 0
    todo = repo / "TODO.md"
    assert todo.exists()
    assert todo.read_text(encoding="utf-8").startswith("# TODO")
    assert snapshot_tasks(todo) == ()  # the empty starter (no seeded rows)


def test_seed_tasks_from_ignored_when_no_tasks_file_created(tmp_path, capsys):
    """--seed-tasks-from with an explicit --tasks-file is ignored (warned), not applied.

    Why this matters: seeding only acts on the defaulted-tasks creation flow. An explicit
    --tasks-file is config-only (never created), so there is nothing to seed; the command
    must warn rather than silently swallow the flag, and still succeed.
    """
    repo = _make_repo(tmp_path)
    doc = tmp_path / "roadmap.md"
    doc.write_text(_ROADMAP_DOC, encoding="utf-8")
    explicit = repo / "MY_TASKS.md"
    cfg = tmp_path / "orion.toml"
    code = _run_add(
        [
            "demo",
            "--repo-path",
            str(repo),
            "--collectors",
            "git,tasks",
            "--tasks-file",
            str(explicit),
            "--seed-tasks-from",
            str(doc),
            "--recipient",
            "Mom:slack:ORION_SLACK_MOM",
            "--yes",
        ],
        cfg,
    )
    assert code == 0
    assert not explicit.exists()  # explicit path stays config-only
    assert "ignored" in capsys.readouterr().err  # the warning was surfaced


# --- Unit 3: focused helper behavior ------------------------------------------


def test_status_is_done_guards_against_substring_false_positives():
    """_status_is_done treats ✅/[x] as done but guards "incomplete" and "not done".

    Why this matters: the seed source is an arbitrary user doc, so a naive substring test
    would wrongly check "incomplete" (contains "complete") or "not done" (contains "done").
    Word boundaries plus a "not" veto keep those open while still honoring real markers.
    """
    assert cli._status_is_done("✅ Signed off (2026-06-15)") is True
    assert cli._status_is_done("Shipped") is True
    assert cli._status_is_done("[x]") is True
    assert cli._status_is_done("incomplete") is False
    assert cli._status_is_done("not done") is False
    assert cli._status_is_done("In progress") is False
    assert cli._status_is_done("") is False


def test_seed_checklist_from_doc_returns_none_for_unusable_doc(tmp_path):
    """The seeding helper returns None for a missing file or a table with no text column.

    Why this matters: None is the "fall back to starter" signal the caller relies on, and
    it must cover both an unreadable doc and a table whose headers Orion doesn't recognize
    as item text (so seeding never raises into the add path).
    """
    missing = tmp_path / "nope.md"
    assert cli._seed_checklist_from_doc(missing, "demo") is None

    # A real table, but no recognized text column (no task/scope/item/... header).
    no_text_col = tmp_path / "weird.md"
    no_text_col.write_text(
        "| Owner | Status |\n| ----- | ------ |\n| Mom | done |\n", encoding="utf-8"
    )
    assert cli._seed_checklist_from_doc(no_text_col, "demo") is None

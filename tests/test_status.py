# =============================================================================
# tests/test_status.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying `orion status` — the read-only, all-projects digest
#                  of what has unreported activity.
# Role in project: status reuses the report flow's activity detector and the
#                  report_history last-report time. These tests pin that it agrees
#                  with real report state (new before a report, up-to-date after,
#                  new again after a fresh commit), fails soft on a bad repo, and
#                  tallies across projects.
# =============================================================================

from orion import cli
from conftest import _answer, _make_repo, _run, _write_config


def _status(toml):
    """Run `orion status` against a config and return the exit code (DRY)."""
    return cli.main(["status", "--config", str(toml)])


def _write_two_project_config(tmp_path, repo_a, repo_b):
    """Write an orion.toml with two git projects (a, b) sharing one recipient.

    Why: the cross-project tally needs more than one project; the recipient only
    needs a valid env-var NAME (status/baseline never resolve the secret).
    """
    toml = tmp_path / "orion.toml"
    toml.write_text(
        f"""
        state_db = "state.sqlite3"

        [projects.a]
        repo_path = "{repo_a.as_posix()}"
        collectors = ["git"]

          [[projects.a.recipients]]
          name = "Alex"
          channel = "discord"
          webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"

        [projects.b]
        repo_path = "{repo_b.as_posix()}"
        collectors = ["git"]

          [[projects.b.recipients]]
          name = "Alex"
          channel = "discord"
          webhook_env_var = "ORION_DISCORD_WEBHOOK_ALEX"
        """
    )
    return toml


def test_never_reported_shows_new_activity(tmp_path, capsys):
    """A never-reported project reads as new activity, not 'up to date'.

    Why: never-reported is the core backlog case — with no marker, the git
    collector treats the whole history as new, which is what should surface.
    """
    repo = _make_repo(tmp_path)
    toml = _write_config(tmp_path, repo)
    code = _status(toml)
    out = capsys.readouterr().out
    assert code == 0
    assert "demo" in out
    assert "never reported" in out
    assert "new: git" in out
    assert "1 of 1 project(s) have unreported activity." in out


def test_up_to_date_after_report(tmp_path, capsys, env_and_mocks):
    """After a confirmed report, status flips the project to up-to-date with a time.

    Why: a successful report advances the git marker and writes report_history, so
    status must read "up to date" and a relative last-report time (not "never").
    """
    mp = env_and_mocks["monkeypatch"]
    repo = _make_repo(tmp_path)
    toml = _write_config(tmp_path, repo)
    _answer(mp, "y")
    assert cli.main(["report", "demo", "--config", str(toml)]) == 0
    capsys.readouterr()  # discard the report's output

    code = _status(toml)
    out = capsys.readouterr().out
    assert code == 0
    assert "up to date" in out
    assert "last report" in out
    assert "never reported" not in out
    assert "0 of 1 project(s) have unreported activity." in out


def test_new_commit_after_report_shows_new(tmp_path, capsys, env_and_mocks):
    """A commit made after a report makes status show new git activity again.

    Why: this is the digest's whole point — surfacing work done since the last
    delivered report.
    """
    mp = env_and_mocks["monkeypatch"]
    repo = _make_repo(tmp_path)
    toml = _write_config(tmp_path, repo)
    _answer(mp, "y")
    assert cli.main(["report", "demo", "--config", str(toml)]) == 0
    capsys.readouterr()

    (repo / "more.py").write_text("x = 2\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "More work")

    code = _status(toml)
    out = capsys.readouterr().out
    assert code == 0
    assert "new: git" in out
    assert "1 of 1 project(s) have unreported activity." in out


def test_missing_repo_path_is_failsoft(tmp_path, capsys):
    """A project whose repo_path doesn't exist shows 'unreadable', never crashes.

    Why: status must survive a misconfigured project and still exit cleanly so it
    can report on the rest of the projects.
    """
    ghost = tmp_path / "ghost"  # deliberately never created
    toml = _write_config(tmp_path, ghost)
    code = _status(toml)
    out = capsys.readouterr().out
    assert code == 0
    assert "unreadable: git" in out


def test_multi_project_tally(tmp_path, capsys):
    """With two projects, the footer counts only those with unreported activity.

    Why: the cross-project tally is the headline number. We baseline 'a' (advancing
    its marker without sending) so it's up to date, while 'b' stays new — a clean
    mixed state with no delivery mocks needed.
    """
    repo_a = _make_repo(tmp_path, name="a")
    repo_b = _make_repo(tmp_path, name="b")
    toml = _write_two_project_config(tmp_path, repo_a, repo_b)

    assert cli.main(["baseline", "a", "--config", str(toml)]) == 0
    capsys.readouterr()

    code = _status(toml)
    out = capsys.readouterr().out
    assert code == 0
    assert "1 of 2 project(s) have unreported activity." in out

# =============================================================================
# tests/test_git_collector.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the git collector against REAL temporary repos —
#                  delta detection, first run, empty/non-repo handling, and the
#                  security filters (sensitive content excluded; share level
#                  controls whether any diff is emitted).
# Role in project: The git collector is where secrets first enter the pipeline as
#                  text. These tests pin that sensitive file CONTENT never appears
#                  in the collected output, which is the front line of the plan's
#                  security release gate.
# =============================================================================

import subprocess

import pytest

from orion.collectors.git import GitError, collect


def _run(repo, *args):
    """Run a git command in a test repo, raising on failure.

    Args:
        repo: Path to the repository.
        *args: git arguments.

    Why:
        Tests need to build repo history; this keeps each test focused on the
        scenario rather than subprocess boilerplate (DRY).
    """
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _init_repo(tmp_path):
    """Create an initialized repo with a committer identity, no commits yet.

    Args:
        tmp_path: pytest temp dir.

    Returns:
        Path to the new repo.

    Why:
        Setting user.name/email locally avoids depending on the machine's global
        git config, so the tests are hermetic.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init", "-q")
    _run(repo, "config", "user.email", "test@example.com")
    _run(repo, "config", "user.name", "Test User")
    return repo


def _commit(repo, filename, content, message):
    """Write a file and commit it.

    Args:
        repo: Path to the repo.
        filename: File to write (relative to repo).
        content: File contents.
        message: Commit message.

    Returns:
        The new HEAD sha.

    Why:
        Most tests need "make a commit and get its sha"; centralizing it keeps
        them readable.
    """
    (repo / filename).write_text(content)
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", message)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_first_run_reports_full_history(tmp_path):
    """With no prior marker, the collector reports all activity.

    Why this matters: the first report must cover existing history, and the
    marker it returns must equal HEAD so the next run is a delta.
    """
    repo = _init_repo(tmp_path)
    head = _commit(repo, "feature.py", "def f(): return 1\n", "Add feature")

    result = collect(repo, since_sha=None, share_level="high_level")

    assert result.has_activity is True
    assert result.new_marker == head
    assert "Add feature" in result.raw_text  # commit message present


def test_no_new_commits_reports_no_activity(tmp_path):
    """When since_sha equals HEAD, there is nothing to report.

    Why this matters: this is what makes an immediate re-run say "no new
    activity" and skip the LLM and delivery entirely.
    """
    repo = _init_repo(tmp_path)
    head = _commit(repo, "a.py", "x = 1\n", "First")

    result = collect(repo, since_sha=head, share_level="high_level")

    assert result.has_activity is False
    assert result.new_marker == head


def test_incremental_reports_only_new_commit(tmp_path):
    """After a marker, only commits since that marker are reported.

    Why this matters: delta reporting — the supervisor should see what's new, not
    the whole history again.
    """
    repo = _init_repo(tmp_path)
    first = _commit(repo, "a.py", "x = 1\n", "First commit")
    _commit(repo, "b.py", "y = 2\n", "Second commit")

    result = collect(repo, since_sha=first, share_level="high_level")

    assert result.has_activity is True
    assert "Second commit" in result.raw_text
    assert "First commit" not in result.raw_text  # already reported


def test_empty_repo_reports_no_activity(tmp_path):
    """A repo with no commits yields no activity rather than an error.

    Why this matters: a freshly-initialized project shouldn't crash the tool.
    """
    repo = _init_repo(tmp_path)
    result = collect(repo, since_sha=None, share_level="high_level")
    assert result.has_activity is False


def test_missing_path_raises_git_error(tmp_path):
    """A nonexistent repo_path raises a clear GitError.

    Why this matters: a typo'd path should fail loudly at collection, not send a
    confusing empty report.
    """
    with pytest.raises(GitError):
        collect(tmp_path / "nope", since_sha=None, share_level="high_level")


def test_non_repo_directory_raises_git_error(tmp_path):
    """A real directory that is not a git repo raises GitError.

    Why this matters: pointing repo_path at the wrong folder is a common mistake.
    """
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(GitError):
        collect(plain, since_sha=None, share_level="high_level")


def test_high_level_omits_code_diff(tmp_path):
    """At high_level, file CONTENT is never included — only messages + diffstat.

    Why this matters: high_level is the safe default; it must not leak code. We
    check that a distinctive source line does not appear in the output.
    """
    repo = _init_repo(tmp_path)
    _commit(repo, "feature.py", "MAGIC_CONSTANT = 'distinctive_value_123'\n", "Add")

    result = collect(repo, since_sha=None, share_level="high_level")

    assert "distinctive_value_123" not in result.raw_text  # no code content
    assert "feature.py" in result.raw_text  # but the filename (diffstat) is fine


def test_detailed_includes_normal_code_diff(tmp_path):
    """At detailed, ordinary source content DOES appear in the diff excerpt.

    Why this matters: detailed mode exists to give the summarizer real specifics;
    this confirms the diff is actually included for non-sensitive files.
    """
    repo = _init_repo(tmp_path)
    _commit(repo, "feature.py", "MAGIC_CONSTANT = 'distinctive_value_123'\n", "Add")

    result = collect(repo, since_sha=None, share_level="detailed")

    assert "distinctive_value_123" in result.raw_text


def test_sensitive_file_content_excluded_even_at_detailed(tmp_path):
    """A .env file's CONTENT is excluded from the diff even in detailed mode.

    Why this matters: THE core security guarantee of the collector — sensitive
    files are filtered before the diff is built, so a secret in .env never enters
    the collected text. Its filename may appear (diffstat / omission note), but
    its value must not.
    """
    repo = _init_repo(tmp_path)
    # Commit a sensitive file alongside a normal one.
    (repo / ".env").write_text("AWS_SECRET_ACCESS_KEY=supersecretvalue_should_never_leak\n")
    (repo / "app.py").write_text("print('hello')\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "Add app and (oops) .env")

    result = collect(repo, since_sha=None, share_level="detailed")

    # Secret value never appears anywhere in the collected text.
    assert "supersecretvalue_should_never_leak" not in result.raw_text
    # The normal file's content is still included (detailed mode).
    assert "print('hello')" in result.raw_text
    # The omission is made visible, not silent.
    assert ".env" in result.raw_text

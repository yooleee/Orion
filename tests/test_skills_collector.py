# =============================================================================
# tests/test_skills_collector.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the skills collector's snapshot() — the
#                  gather-evidence -> redact-before-LLM -> hash-cache -> extract
#                  pipeline (E2 Inc 4 slice 4c, the "skills comb").
# Role in project: This collector DERIVES a project's skills from observed activity
#                  (languages from tracked files, recent commit subjects, doc focus),
#                  the honest alternative to an authored resume. These tests pin the
#                  behaviors that make it cheap, honest, and safe: the LLM runs only on
#                  CHANGED evidence; secrets are redacted BEFORE the model; an extraction
#                  failure PROPAGATES (so the CLI never clobbers stored skills with an
#                  empty push); and an empty repo extracts nothing without a model call.
#                  A FAKE extractor is injected, against a REAL temporary git repo.
# =============================================================================

import subprocess

import pytest

from orion.collectors.skills import snapshot
from orion.extract import Skill, ExtractError


def _run(repo, *args):
    """Run a git command in a test repo, raising on failure (DRY for history setup)."""
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _init_repo(tmp_path):
    """Create an initialized repo with a local committer identity (hermetic)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init", "-q")
    _run(repo, "config", "user.email", "test@example.com")
    _run(repo, "config", "user.name", "Test User")
    return repo


def _commit(repo, filename, content, message):
    """Write a file and commit it, returning nothing (history is the point)."""
    (repo / filename).write_text(content, encoding="utf-8")
    _run(repo, "add", filename)
    _run(repo, "commit", "-q", "-m", message)


class _FakeExtractor:
    """An injected SkillExtractor stand-in: records calls, returns canned skills.

    Why:
        Lets us assert HOW OFTEN the (expensive) extractor runs — the whole point of the
        cache — and WHAT text it received (to prove evidence was gathered and redacted
        first), without a network call.
    """

    def __init__(self, specs=(("Python backends", "Backend", "evidence", 2, ("git",)),), error=None):
        self.calls = []
        self._specs = specs
        self.error = error

    def extract(self, evidence_text):
        self.calls.append(evidence_text)
        if self.error is not None:
            raise self.error
        return tuple(
            Skill(name=n, category=c, evidence=e, weight=w, signals=s)
            for (n, c, e, w, s) in self._specs
        )


def _cache():
    """A dict-backed (store, get, set) cache, standing in for the state store."""
    store: dict = {}
    return (
        store,
        lambda key: store.get(key),
        lambda key, content_hash, value: store.__setitem__(key, (content_hash, value)),
    )


def test_first_run_gathers_languages_and_commits_then_extracts(tmp_path):
    """A first snapshot renders languages + commit subjects and calls the extractor once.

    Why this matters: the baseline path — the bundle the model sees must contain the
    project's real observed evidence (the language from its tracked files and what its
    commits say), which is what makes the resulting skills honest rather than authored.
    """
    repo = _init_repo(tmp_path)
    _commit(repo, "app.py", "print('hi')\n", "Add the Python entrypoint")
    ext = _FakeExtractor()
    _store, get, set_ = _cache()

    out = snapshot(repo, (), ext, cache_get=get, cache_set=set_)

    assert out == (
        Skill(name="Python backends", category="Backend", evidence="evidence", weight=2, signals=("git",)),
    )
    assert len(ext.calls) == 1
    bundle = ext.calls[0]
    assert "Python" in bundle  # language derived from the .py tracked file
    assert "Add the Python entrypoint" in bundle  # commit subject is evidence


def test_unchanged_evidence_reuses_cache_without_calling_the_model(tmp_path):
    """A second run on UNCHANGED evidence returns cached skills and does NOT re-extract.

    Why this matters: the cost/idempotency guarantee — the LLM must run only when the
    evidence actually changes, so a repeated push is free and stable.
    """
    repo = _init_repo(tmp_path)
    _commit(repo, "app.py", "x = 1\n", "Initial commit")
    ext = _FakeExtractor()
    _store, get, set_ = _cache()

    first = snapshot(repo, (), ext, cache_get=get, cache_set=set_)
    second = snapshot(repo, (), ext, cache_get=get, cache_set=set_)

    assert first == second
    assert len(ext.calls) == 1  # the second run was a pure cache hit


def test_new_commit_changes_evidence_and_re_extracts(tmp_path):
    """A new commit changes the bundle hash, so the next run re-extracts.

    Why this matters: new work must invalidate the cache — otherwise the comb would
    freeze on stale skills as the project keeps moving.
    """
    repo = _init_repo(tmp_path)
    _commit(repo, "app.py", "x = 1\n", "First")
    ext = _FakeExtractor()
    _store, get, set_ = _cache()

    snapshot(repo, (), ext, cache_get=get, cache_set=set_)
    _commit(repo, "web.tsx", "export const A = () => null\n", "Add a React component")
    snapshot(repo, (), ext, cache_get=get, cache_set=set_)

    assert len(ext.calls) == 2  # the new commit + new language forced a fresh extraction


def test_evidence_is_redacted_before_reaching_the_model(tmp_path):
    """A secret in a commit subject is scrubbed BEFORE the extractor (privacy invariant).

    Why this matters: the model is the weakest layer; no raw secret may reach the LLM.
    Commit subjects are author free-text, so they go through the same redaction net.
    """
    repo = _init_repo(tmp_path)
    _commit(repo, "app.py", "x = 1\n", "Set key sk-ant-aaaaaaaaaaaaaaaaaaaaaaaaaaaa in config")
    ext = _FakeExtractor()
    _store, get, set_ = _cache()

    snapshot(repo, (), ext, cache_get=get, cache_set=set_)

    sent = ext.calls[0]
    assert "sk-ant-" not in sent
    assert "[REDACTED" in sent


def test_extraction_failure_propagates_and_is_not_cached(tmp_path):
    """An extraction error PROPAGATES (not swallowed) and nothing is cached.

    Why this matters: skills is a single bundle, so a transient API failure must NOT
    become an empty result that the CLI would push as a full-state replace (clobbering
    the project's stored skills). Propagating lets the CLI abort without pushing, and
    not caching means the next run retries.
    """
    repo = _init_repo(tmp_path)
    _commit(repo, "app.py", "x = 1\n", "First")
    ext = _FakeExtractor(error=ExtractError("api down"))
    store, get, set_ = _cache()

    with pytest.raises(ExtractError):
        snapshot(repo, (), ext, cache_get=get, cache_set=set_)

    assert store == {}  # nothing cached, so a later run will retry


def test_empty_repo_yields_no_skills_without_calling_the_model(tmp_path):
    """A repo with no commits and no docs has no evidence, so the model is never called.

    Why this matters: no evidence means nothing to observe — we must return empty
    cheaply (no API spend) rather than prompting the model with an empty bundle.
    """
    repo = _init_repo(tmp_path)  # initialized, but no commits
    ext = _FakeExtractor()
    _store, get, set_ = _cache()

    out = snapshot(repo, (), ext, cache_get=get, cache_set=set_)

    assert out == ()
    assert ext.calls == []  # no bundle, no model call


def test_missing_doc_is_skipped_but_git_evidence_still_extracts(tmp_path):
    """An unreadable focus doc contributes nothing; git evidence still drives extraction.

    Why this matters: docs are additive focus context — a missing/renamed doc must never
    abort the snapshot, which still has the project's languages and commits to observe.
    """
    repo = _init_repo(tmp_path)
    _commit(repo, "app.py", "x = 1\n", "First")
    missing = repo / "gone.md"  # never created
    ext = _FakeExtractor()
    _store, get, set_ = _cache()

    out = snapshot(repo, (missing,), ext, cache_get=get, cache_set=set_)

    assert len(out) == 1
    assert len(ext.calls) == 1  # extraction still happened from git evidence alone

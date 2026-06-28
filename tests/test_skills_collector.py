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

from orion.collectors.skills import SkillProject, snapshot, sync
from orion.extract import ExtractError, Skill, VocabSkill


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


# =============================================================================
# sync() — the GLOBAL two-pass orchestrator (the skills comb rework). Pass 1 builds one
# deduplicated vocabulary across ALL projects; pass 2 attributes it per project, BLIND to
# the others. These pin the behaviors the rework depends on: consistent cross-project
# naming (so the relay merge collapses duplicates), structural containment (pass 2 never
# sees another project, so it cannot leak one), the two-level cache, and abort-on-failure.
# =============================================================================


class _FakeSyncExtractor:
    """A two-pass SkillExtractor stand-in: records pass-1 and pass-2 calls.

    Why:
        Lets us assert the orchestration — how often each (expensive) pass runs (the cache
        contract), and crucially WHAT bundle each pass-2 call received (to prove a project
        is observed in isolation, the containment guarantee) — with no network call.

    Args:
        vocab_specs: (name, category) pairs the global pass returns.
        attributions: {marker: [(name, evidence, weight, signals), ...]} — a project whose
            bundle CONTAINS `marker` is attributed those skills (names must be in the vocab).
        error: "vocab" or "attribute" to make that pass raise, else None.
    """

    def __init__(self, vocab_specs, attributions, error=None):
        self.vocab_calls = []
        self.attribute_calls = []
        self._vocab_specs = vocab_specs
        self._attributions = attributions
        self.error = error

    def extract_vocab(self, portfolio_text):
        self.vocab_calls.append(portfolio_text)
        if self.error == "vocab":
            raise ExtractError("vocab pass down")
        return tuple(VocabSkill(name=n, category=c) for (n, c) in self._vocab_specs)

    def attribute(self, evidence_text, vocab):
        self.attribute_calls.append((evidence_text, vocab))
        if self.error == "attribute":
            raise ExtractError("attribute pass down")
        category = {v.name: v.category for v in vocab}
        cards = []
        for marker, specs in self._attributions.items():
            if marker in evidence_text:
                cards.extend(
                    Skill(name=n, category=category.get(n, "Backend"), evidence=e, weight=w, signals=s)
                    for (n, e, w, s) in specs
                )
        return tuple(cards)


def _sync_cache():
    """A dict-backed (store, get, set) cache for sync, keyed on (cache_id, key)."""
    store: dict = {}
    return (
        store,
        lambda cache_id, key: store.get((cache_id, key)),
        lambda cache_id, key, content_hash, value: store.__setitem__(
            (cache_id, key), (content_hash, value)
        ),
    )


def _sync_project(tmp_path, name, filename, content, message):
    """Create a one-commit repo and return the SkillProject pointing at it."""
    repo = tmp_path / name
    repo.mkdir()
    _run(repo, "init", "-q")
    _run(repo, "config", "user.email", "test@example.com")
    _run(repo, "config", "user.name", "Test User")
    _commit(repo, filename, content, message)
    return SkillProject(name=name, repo_path=repo, doc_paths=())


def test_sync_gives_every_project_the_same_canonical_name(tmp_path):
    """A skill both projects evidence is attributed under ONE canonical vocab name.

    Why this matters: the core fix — per-project independent extraction produced
    near-duplicate names that the relay merge could not collapse. With one shared
    vocabulary, both projects carry the identical canonical name, so the merge folds them
    into a single comb tooth. Pass 1 runs once; pass 2 once per project.
    """
    a = _sync_project(tmp_path, "alpha", "app.py", "x=1\n", "alpha work")
    b = _sync_project(tmp_path, "beta", "svc.py", "y=2\n", "beta work")
    ext = _FakeSyncExtractor(
        vocab_specs=[("Python backends", "Backend")],
        attributions={
            "alpha work": [("Python backends", "alpha evidence", 2, ("git",))],
            "beta work": [("Python backends", "beta evidence", 3, ("git",))],
        },
    )
    store, get, set_ = _sync_cache()

    out = sync([a, b], ext, cache_get=get, cache_set=set_)

    assert out["alpha"][0].name == out["beta"][0].name == "Python backends"
    assert out["alpha"][0].category == "Backend"  # canonical category from the vocab
    assert len(ext.vocab_calls) == 1  # one global pass over the whole portfolio
    assert len(ext.attribute_calls) == 2  # one attribution per project


def test_sync_pass2_is_blind_to_other_projects(tmp_path):
    """Each pass-2 call receives ONLY its own project's bundle — never another's.

    Why this matters: this is the existence-hiding guarantee restored STRUCTURALLY. Pass 2
    cannot reference (and so cannot leak) a project it never sees, so a scoped viewer can
    never have one project's evidence text name another. We prove the isolation directly:
    every attribution bundle contains exactly one project's marker.
    """
    a = _sync_project(tmp_path, "alpha", "app.py", "x=1\n", "ALPHA_MARKER work")
    b = _sync_project(tmp_path, "beta", "svc.py", "y=2\n", "BETA_MARKER work")
    ext = _FakeSyncExtractor(
        vocab_specs=[("Python backends", "Backend")],
        attributions={"work": [("Python backends", "ev", 2, ("git",))]},
    )
    _store, get, set_ = _sync_cache()

    sync([a, b], ext, cache_get=get, cache_set=set_)

    for bundle, _vocab in ext.attribute_calls:
        has_alpha = "ALPHA_MARKER" in bundle
        has_beta = "BETA_MARKER" in bundle
        assert has_alpha != has_beta  # exactly one project's marker, never both


def test_sync_caches_both_passes_on_unchanged_evidence(tmp_path):
    """A second sync on UNCHANGED evidence re-runs NEITHER pass (two-level cache).

    Why this matters: the cost guarantee. The vocabulary is cached on the whole portfolio's
    content and each attribution on its own bundle plus the vocabulary, so a repeated sync
    with no changes spends nothing on the model.
    """
    a = _sync_project(tmp_path, "alpha", "app.py", "x=1\n", "alpha work")
    b = _sync_project(tmp_path, "beta", "svc.py", "y=2\n", "beta work")
    ext = _FakeSyncExtractor(
        vocab_specs=[("Python backends", "Backend")],
        attributions={"work": [("Python backends", "ev", 2, ("git",))]},
    )
    _store, get, set_ = _sync_cache()

    sync([a, b], ext, cache_get=get, cache_set=set_)
    sync([a, b], ext, cache_get=get, cache_set=set_)

    assert len(ext.vocab_calls) == 1  # vocabulary reused
    assert len(ext.attribute_calls) == 2  # each attribution reused


def test_sync_changed_project_re_extracts_vocab_and_only_that_attribution(tmp_path):
    """A new commit in ONE project re-runs pass 1 and only that project's pass 2.

    Why this matters: a global vocabulary necessarily re-runs when any project changes (the
    price of seeing everything at once), but the UNCHANGED project's attribution still hits
    its cache — so the cost of a change stays proportional, not a full re-extraction.
    """
    a = _sync_project(tmp_path, "alpha", "app.py", "x=1\n", "alpha work")
    b = _sync_project(tmp_path, "beta", "svc.py", "y=2\n", "beta work")
    ext = _FakeSyncExtractor(
        vocab_specs=[("Python backends", "Backend")],
        attributions={"work": [("Python backends", "ev", 2, ("git",))]},
    )
    _store, get, set_ = _sync_cache()

    sync([a, b], ext, cache_get=get, cache_set=set_)
    _commit(a.repo_path, "more.py", "z=3\n", "alpha more work")  # only alpha changes
    sync([a, b], ext, cache_get=get, cache_set=set_)

    assert len(ext.vocab_calls) == 2  # portfolio content changed -> vocab re-run
    assert len(ext.attribute_calls) == 3  # alpha re-attributed; beta was a cache hit


def test_sync_empty_project_gets_empty_slice_and_no_attribute_call(tmp_path):
    """A project with no evidence maps to an empty slice without a pass-2 call.

    Why this matters: no evidence means nothing to observe — the project still appears in
    the result (so the caller can prune/clear it) but never costs a model call, and does
    not enter the portfolio vocabulary.
    """
    a = _sync_project(tmp_path, "alpha", "app.py", "x=1\n", "alpha work")
    empty = tmp_path / "empty"
    empty.mkdir()
    _run(empty, "init", "-q")  # initialized, no commits, no docs
    b = SkillProject(name="empty", repo_path=empty, doc_paths=())
    ext = _FakeSyncExtractor(
        vocab_specs=[("Python backends", "Backend")],
        attributions={"work": [("Python backends", "ev", 2, ("git",))]},
    )
    _store, get, set_ = _sync_cache()

    out = sync([a, b], ext, cache_get=get, cache_set=set_)

    assert out["empty"] == ()  # present in the result, but empty
    assert len(ext.attribute_calls) == 1  # only the project with evidence was attributed


def test_sync_vocab_failure_propagates_and_pushes_nothing(tmp_path):
    """A pass-1 failure raises (so the CLI aborts the whole run without pushing).

    Why this matters: a transient failure must never become an empty/partial push that
    clobbers the relay's stored skills. Propagating lets the CLI leave them intact.
    """
    a = _sync_project(tmp_path, "alpha", "app.py", "x=1\n", "alpha work")
    ext = _FakeSyncExtractor(vocab_specs=[("X", "Backend")], attributions={}, error="vocab")
    _store, get, set_ = _sync_cache()

    with pytest.raises(ExtractError):
        sync([a], ext, cache_get=get, cache_set=set_)


def test_sync_no_evidence_anywhere_returns_all_empty_without_model(tmp_path):
    """When no project has evidence, every slice is empty and no pass runs.

    Why this matters: the whole-portfolio version of the empty-repo guard — no spend when
    there is nothing to observe.
    """
    empty = tmp_path / "empty"
    empty.mkdir()
    _run(empty, "init", "-q")
    p = SkillProject(name="empty", repo_path=empty, doc_paths=())
    ext = _FakeSyncExtractor(vocab_specs=[("X", "Backend")], attributions={})
    _store, get, set_ = _sync_cache()

    out = sync([p], ext, cache_get=get, cache_set=set_)

    assert out == {"empty": ()}
    assert ext.vocab_calls == []  # no evidence -> no model call at all

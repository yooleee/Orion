# =============================================================================
# tests/test_disciplines_collector.py
# -----------------------------------------------------------------------------
# Responsible for: Verifying the disciplines collector's snapshot() — the
#                  read -> redact-before-LLM -> hash-cache -> extract -> stamp-source
#                  pipeline (E2 Inc 4 slice 4b).
# Role in project: This collector reflects the principles a user stated in their own
#                  docs. These tests pin the behaviors that make it cheap, honest, and
#                  safe: the LLM runs only on a CHANGED doc; the source is repo-relative
#                  (never an absolute home path); secrets are redacted BEFORE the model;
#                  and one bad/missing doc never aborts the rest. A FAKE extractor is
#                  injected, so no real API is called.
# =============================================================================

from orion.collectors.disciplines import snapshot
from orion.extract import Discipline, ExtractError


class _FakeExtractor:
    """An injected DisciplineExtractor stand-in: records calls, returns canned cards.

    Why:
        Lets us assert HOW OFTEN the (expensive) extractor runs — the whole point of
        the cache — and WHAT text it received (to prove redaction ran first), without a
        network call. It echoes the caller's `source` into each card, mirroring the
        real backend, so source-stamping is observable on the returned Disciplines.
    """

    def __init__(self, specs=(("Title", "Why", "project"),), error=None):
        self.calls = []
        self._specs = specs
        self.error = error

    def extract(self, doc_text, *, source):
        self.calls.append({"text": doc_text, "source": source})
        if self.error is not None:
            raise self.error
        return tuple(
            Discipline(title=t, why=w, scope=s, source=source) for (t, w, s) in self._specs
        )


def _cache():
    """A dict-backed (store, get, set) cache, standing in for the state store.

    Why:
        The collector takes cache_get/cache_set callables precisely so it stays
        decoupled from sqlite and is trivially testable. The dict lets a test inspect
        and persist cache state across snapshot() calls within one test.
    """
    store: dict = {}
    return (
        store,
        lambda key: store.get(key),
        lambda key, content_hash, value: store.__setitem__(key, (content_hash, value)),
    )


def test_first_run_extracts_and_caches(tmp_path):
    """A first snapshot reads the doc, calls the extractor once, and caches the result.

    Why this matters: the baseline path — an unseen doc is extracted and its result is
    stored under the doc path so later runs can reuse it.
    """
    repo = tmp_path
    doc = repo / "CLAUDE.md"
    doc.write_text("Principles live here.", encoding="utf-8")
    ext = _FakeExtractor(specs=(("Local-first", "Runs locally.", "global"),))
    store, get, set_ = _cache()

    out = snapshot([doc], repo, ext, cache_get=get, cache_set=set_)

    assert out == (
        Discipline(title="Local-first", why="Runs locally.", scope="global", source="CLAUDE.md"),
    )
    assert len(ext.calls) == 1
    assert str(doc) in store  # the extraction was cached under the doc path


def test_unchanged_doc_reuses_cache_without_calling_the_model(tmp_path):
    """A second run on an UNCHANGED doc returns the cached cards and does NOT re-extract.

    Why this matters: this is the cost/idempotency guarantee — the LLM must run only
    when a doc's content actually changes, so a repeated push is free and stable.
    """
    repo = tmp_path
    doc = repo / "CLAUDE.md"
    doc.write_text("Same content.", encoding="utf-8")
    ext = _FakeExtractor()
    store, get, set_ = _cache()

    first = snapshot([doc], repo, ext, cache_get=get, cache_set=set_)
    second = snapshot([doc], repo, ext, cache_get=get, cache_set=set_)

    assert first == second
    assert len(ext.calls) == 1  # the second run was a pure cache hit


def test_changed_doc_re_extracts(tmp_path):
    """Editing the doc changes its hash, so the next run re-extracts.

    Why this matters: a content edit must invalidate the cache — otherwise the
    dashboard would freeze on stale principles after the user revised a doc.
    """
    repo = tmp_path
    doc = repo / "CLAUDE.md"
    doc.write_text("Version one.", encoding="utf-8")
    ext = _FakeExtractor()
    store, get, set_ = _cache()

    snapshot([doc], repo, ext, cache_get=get, cache_set=set_)
    doc.write_text("Version two — revised.", encoding="utf-8")
    snapshot([doc], repo, ext, cache_get=get, cache_set=set_)

    assert len(ext.calls) == 2  # the edit forced a fresh extraction


def test_source_is_repo_relative_for_in_repo_docs(tmp_path):
    """A doc under the repo gets a repo-relative, forward-slash source.

    Why this matters: the dashboard footer shows 'observed · design/README.md' — a
    clean repo-relative path, which is also the privacy-safe form.
    """
    repo = tmp_path / "repo"
    (repo / "design").mkdir(parents=True)
    doc = repo / "design" / "README.md"
    doc.write_text("x", encoding="utf-8")
    ext = _FakeExtractor()
    _store, get, set_ = _cache()

    out = snapshot([doc], repo, ext, cache_get=get, cache_set=set_)

    assert out[0].source == "design/README.md"
    assert ext.calls[0]["source"] == "design/README.md"


def test_source_falls_back_to_basename_for_out_of_repo_docs(tmp_path):
    """A doc OUTSIDE the repo uses its bare name, never an absolute home path.

    Why this matters: a global file (e.g. ~/.claude/CLAUDE.md) sits outside the repo;
    emitting its absolute path would leak the home directory and username. The
    basename fallback is the privacy fix and still reads sensibly in the footer.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "global.md"  # a sibling of the repo, not under it
    outside.write_text("y", encoding="utf-8")
    ext = _FakeExtractor()
    _store, get, set_ = _cache()

    out = snapshot([outside], repo, ext, cache_get=get, cache_set=set_)

    assert out[0].source == "global.md"


def test_doc_is_redacted_before_reaching_the_model(tmp_path):
    """A secret in the doc is scrubbed BEFORE the extractor (the privacy invariant).

    Why this matters: the model is the weakest layer; the non-negotiable rule is that
    no raw secret reaches the LLM. The extractor must receive redacted text.
    """
    repo = tmp_path
    doc = repo / "CLAUDE.md"
    doc.write_text("Our key is sk-ant-aaaaaaaaaaaaaaaaaaaaaaaaaaaa for the API.", encoding="utf-8")
    ext = _FakeExtractor()
    _store, get, set_ = _cache()

    snapshot([doc], repo, ext, cache_get=get, cache_set=set_)

    sent = ext.calls[0]["text"]
    assert "sk-ant-" not in sent
    assert "[REDACTED" in sent


def test_missing_doc_is_skipped_others_proceed(tmp_path):
    """An unreadable doc contributes nothing; the readable docs still extract.

    Why this matters: disciplines are additive context — a missing/renamed doc must
    never abort the snapshot of the docs that ARE present.
    """
    repo = tmp_path
    missing = repo / "gone.md"  # never created
    present = repo / "CLAUDE.md"
    present.write_text("here", encoding="utf-8")
    ext = _FakeExtractor(specs=(("T", "w", "project"),))
    _store, get, set_ = _cache()

    out = snapshot([missing, present], repo, ext, cache_get=get, cache_set=set_)

    assert [d.title for d in out] == ["T"]
    # Only the present doc reached the extractor.
    assert [c["source"] for c in ext.calls] == ["CLAUDE.md"]


def test_extract_failure_is_skipped_and_not_cached(tmp_path):
    """A doc whose extraction errors yields nothing and is NOT cached (retries next run).

    Why this matters: a transient API failure must fail soft (don't crash) AND not
    poison the cache with an empty result, so the next run re-attempts it.
    """
    repo = tmp_path
    doc = repo / "CLAUDE.md"
    doc.write_text("content", encoding="utf-8")
    ext = _FakeExtractor(error=ExtractError("api down"))
    store, get, set_ = _cache()

    out = snapshot([doc], repo, ext, cache_get=get, cache_set=set_)

    assert out == ()
    assert store == {}  # nothing cached, so a later run will retry

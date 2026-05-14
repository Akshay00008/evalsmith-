# tests/test_corpus.py
# Verifies the corpus + retrieval pipeline works without any PDFs —
# we hand-build a corpus.jsonl and check BM25 returns the expected
# chunk, then verify the registry.retrieve() entry point uses it.

from __future__ import annotations
import json
import tempfile
from pathlib import Path

from lib import corpus as corpus_mod
from lib import registry as model_registry
from lib.schemas.variant import RetrievalConfig


def _scratch_project_with_corpus(rows: list[dict]) -> Path:
    """Build a temporary project workspace with a corpus.jsonl populated
    from the given rows. Returns the project root path."""
    project_dir = Path(tempfile.mkdtemp(prefix="agt_corpus_"))
    (project_dir / "data").mkdir()
    with (project_dir / "data" / "corpus.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return project_dir


def test_has_corpus_detects_populated_file():
    """has_corpus returns False on empty/missing files; True once populated."""
    pd = Path(tempfile.mkdtemp())
    (pd / "data").mkdir()
    assert corpus_mod.has_corpus(pd) is False
    (pd / "data" / "corpus.jsonl").write_text("", encoding="utf-8")
    assert corpus_mod.has_corpus(pd) is False  # empty file
    (pd / "data" / "corpus.jsonl").write_text('{"doc_id":"x","text":"hi"}\n', encoding="utf-8")
    # Clear the lru_cache so we don't reuse an earlier empty load.
    corpus_mod._load_corpus.cache_clear()
    assert corpus_mod.has_corpus(pd) is True


def test_bm25_retrieves_topically_relevant_chunk():
    """The retriever should rank a chunk that shares query terms above an
    unrelated chunk. This is the bedrock of the entire RAG path."""
    pd = _scratch_project_with_corpus([
        {"doc_id": "d1", "text": "Refunds are available within 30 days of purchase. Digital products are non-refundable."},
        {"doc_id": "d2", "text": "Our office hours are 9am to 5pm Eastern Time. Holidays follow the federal schedule."},
        {"doc_id": "d3", "text": "Subscriptions can be cancelled anytime via the account settings page."},
    ])
    corpus_mod._load_corpus.cache_clear()  # ensure we read this fresh tmp file
    hits = corpus_mod.bm25_retrieve(pd, query="refund policy", top_k=2)
    assert hits, "Expected at least one BM25 hit"
    assert hits[0]["doc_id"] == "d1", f"Expected d1 (refund-related) first, got {hits[0]['doc_id']}"
    # Score should be positive for a real match.
    assert hits[0]["score"] > 0


def test_bm25_returns_empty_on_no_overlap():
    """Query with zero shared terms => no hits (BM25 score 0 is skipped)."""
    pd = _scratch_project_with_corpus([
        {"doc_id": "d1", "text": "Refunds are available within 30 days."},
    ])
    corpus_mod._load_corpus.cache_clear()
    hits = corpus_mod.bm25_retrieve(pd, query="unrelated quantum mechanics", top_k=3)
    assert hits == []


def test_registry_retrieve_routes_to_real_corpus_when_present():
    """registry.retrieve() must dispatch to lib.corpus when corpus_dir is
    passed AND the corpus file exists. The non-stub doc_ids prove it."""
    pd = _scratch_project_with_corpus([
        {"doc_id": "real_doc_alpha", "text": "Refunds are processed within 30 days."},
        {"doc_id": "real_doc_beta",  "text": "Subscriptions auto-renew unless cancelled."},
    ])
    corpus_mod._load_corpus.cache_clear()
    cfg = RetrievalConfig(enabled=True, retriever_kind="bm25", top_k=2)
    hits = model_registry.retrieve(query="refund", config=cfg, corpus_dir=pd)
    assert hits, "Expected at least one hit from real corpus"
    # The stub would have produced "doc_<num>" ids; real corpus uses our names.
    assert all(h["doc_id"].startswith("real_doc_") for h in hits), \
        f"Got non-real doc_ids — registry didn't route to corpus: {[h['doc_id'] for h in hits]}"


def test_registry_retrieve_falls_back_to_stub_when_no_corpus():
    """Without a corpus file, registry.retrieve() returns deterministic
    stub docs — the default behavior tests rely on."""
    pd = Path(tempfile.mkdtemp())
    (pd / "data").mkdir()
    corpus_mod._load_corpus.cache_clear()
    cfg = RetrievalConfig(enabled=True, retriever_kind="bm25", top_k=2)
    hits = model_registry.retrieve(query="anything", config=cfg, corpus_dir=pd)
    assert len(hits) == 2
    # Stub doc_ids follow the "doc_<hash>" pattern.
    assert all(h["doc_id"].startswith("doc_") for h in hits)


def test_chunk_text_handles_short_input_as_single_chunk():
    """Strings shorter than chunk_size_chars produce exactly one chunk."""
    chunks = list(corpus_mod.chunk_text("Short text.", chunk_size_chars=1500))
    assert len(chunks) == 1
    assert chunks[0] == (0, 11, "Short text.")


def test_chunk_text_paragraph_aware_split():
    """When paragraphs fit, the splitter groups them into chunks at
    paragraph boundaries rather than mid-sentence."""
    p1 = "First paragraph about refunds and policies." * 5    # ~225 chars
    p2 = "Second paragraph about subscriptions." * 5         # ~190 chars
    p3 = "Third paragraph about authentication." * 5         # ~190 chars
    text = "\n\n".join([p1, p2, p3])
    # Force grouping: chunk size big enough for two paragraphs.
    chunks = list(corpus_mod.chunk_text(text, chunk_size_chars=500))
    assert len(chunks) >= 2
    # No chunk should start mid-paragraph (every chunk starts with "First",
    # "Second", or "Third").
    for _, _, ctext in chunks:
        first_word = ctext.lstrip().split()[0]
        assert first_word in ("First", "Second", "Third"), \
            f"Chunk starts mid-paragraph: {ctext[:80]!r}"

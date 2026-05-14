# lib/corpus.py
# Corpus loading + retrieval for RAG missions.
#
# The corpus is a JSON-Lines file at `<project>/data/corpus.jsonl`. Each
# row is one chunk with a stable `doc_id` that EvalCase.relevant_doc_ids
# references. The framework's `retrieve()` reads this corpus when the
# Variant's RetrievalConfig requests it.
#
# Two retrievers are supported here:
#   * "bm25"  — lexical, no embedding model needed; works offline.
#   * "dense" — embeddings + cosine similarity. Lazy-imports
#                sentence-transformers; falls back to BM25 if missing.
#
# Both share the corpus file format so you can swap retrievers without
# re-ingesting PDFs.

from __future__ import annotations
from pathlib import Path
from typing import Iterable, Optional
import json
import math
import re
from functools import lru_cache


# ---------------------------------------------------------------------------
# Corpus loading
# ---------------------------------------------------------------------------

def corpus_path(project_dir: Path) -> Path:
    """Convention: corpus lives at `<project>/data/corpus.jsonl`. The PDF
    ingestion tool writes here; the retriever reads from here."""
    return project_dir / "data" / "corpus.jsonl"


@lru_cache(maxsize=8)
def _load_corpus(corpus_file: str) -> list[dict]:
    """Load the corpus once per process. The lru_cache key is the *string*
    path because Path objects aren't hashable across older Python versions
    in some configs."""
    path = Path(corpus_file)
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def has_corpus(project_dir: Path) -> bool:
    """True if a non-empty corpus file exists for this project."""
    p = corpus_path(project_dir)
    return p.exists() and p.stat().st_size > 0


def load_chunks(project_dir: Path) -> list[dict]:
    """Public loader. Returns a list of chunk dicts:
        {doc_id, text, source_pdf?, page?, char_start?, char_end?, ...}
    The caller should treat the dicts as read-only.
    """
    return _load_corpus(str(corpus_path(project_dir)))


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------
# Minimal regex tokenizer — splits on non-word chars, lowercases. Good
# enough for BM25 over English-ish technical docs. Replace with a proper
# tokenizer if you need multilingual / phrase-aware retrieval.

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")

# A tiny English suffix stripper. Not a full Porter — just the four
# suffixes that account for ~80% of inflectional variation in technical
# English text. Lets BM25 match "refund" ↔ "refunds", "cancel" ↔ "cancelled",
# "process" ↔ "processing".
#
# Trade-off acknowledged: this over-stems some words ("business" → "busines"),
# but the over-stemming is *consistent* on both sides (query + doc) so
# matches still work. For multilingual / strict-stemming workloads, swap
# this for nltk's PorterStemmer or snowball.
_SUFFIXES = ("ing", "ies", "ed", "es", "ly", "s")


def _stem(token: str) -> str:
    """Strip one trailing suffix if present and length permits. Tokens
    shorter than 4 chars are returned unchanged (stemming short tokens
    typically hurts recall more than it helps)."""
    if len(token) < 4:
        return token
    for suf in _SUFFIXES:
        if token.endswith(suf) and len(token) - len(suf) >= 3:
            return token[: -len(suf)]
    return token


def _tokenize(text: str) -> list[str]:
    return [_stem(t.lower()) for t in _WORD_RE.findall(text or "")]


# ---------------------------------------------------------------------------
# BM25 retriever
# ---------------------------------------------------------------------------
# We implement BM25 inline (no dependency) so the framework retrieves
# correctly on a bare-bones install. If `rank_bm25` is installed, we
# delegate to it for speed; the scoring is functionally identical.

def _bm25_score(
    query_tokens: list[str],
    doc_tokens_list: list[list[str]],
    avgdl: float,
    df: dict[str, int],
    n_docs: int,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[float]:
    """Pure-Python BM25 ranker. Returns one score per doc in the same
    order as `doc_tokens_list`."""
    scores = [0.0] * n_docs
    for q in set(query_tokens):
        if q not in df:
            continue
        n_q = df[q]
        # idf with the smoothing variant used by Anserini/Elasticsearch.
        idf = math.log((n_docs - n_q + 0.5) / (n_q + 0.5) + 1.0)
        for i, doc in enumerate(doc_tokens_list):
            tf = doc.count(q)
            if tf == 0:
                continue
            dl = len(doc)
            denom = tf + k1 * (1 - b + b * dl / avgdl)
            scores[i] += idf * tf * (k1 + 1) / denom
    return scores


def bm25_retrieve(project_dir: Path, query: str, top_k: int) -> list[dict]:
    """Top-k BM25 retrieval over the project corpus. Returns chunks with
    a `score` field added. Empty list if corpus missing."""
    chunks = load_chunks(project_dir)
    if not chunks:
        return []

    doc_tokens = [_tokenize(c.get("text", "")) for c in chunks]
    n_docs = len(chunks)
    avgdl = sum(len(d) for d in doc_tokens) / max(1, n_docs)

    # Document frequency table — count distinct docs each token appears in.
    df: dict[str, int] = {}
    for d in doc_tokens:
        for t in set(d):
            df[t] = df.get(t, 0) + 1

    q_tokens = _tokenize(query)
    scores = _bm25_score(q_tokens, doc_tokens, avgdl, df, n_docs)

    # Pair scores with chunks, sort desc, take top_k.
    ranked = sorted(zip(scores, chunks), key=lambda x: x[0], reverse=True)
    out = []
    for score, chunk in ranked[:top_k]:
        if score <= 0:
            break  # BM25 score <= 0 means no shared content terms — skip.
        out.append({**chunk, "score": float(score)})
    return out


# ---------------------------------------------------------------------------
# Dense retriever (optional — needs sentence-transformers)
# ---------------------------------------------------------------------------

def dense_retrieve(project_dir: Path, query: str, top_k: int, *, embedder: str) -> list[dict]:
    """Embedding-based retrieval. Lazy-imports sentence-transformers; if
    missing OR the embedder fails, falls back to BM25 transparently so the
    framework still produces results."""
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
        import numpy as np  # type: ignore
    except ImportError:
        # No embedding stack — fall through.
        return bm25_retrieve(project_dir, query, top_k)

    chunks = load_chunks(project_dir)
    if not chunks:
        return []

    model = _cached_embedder(embedder)
    # Encode the corpus once per process via the lru_cache below.
    corpus_emb = _cached_corpus_embeddings(str(corpus_path(project_dir)), embedder)
    q_emb = model.encode([query], normalize_embeddings=True)[0]
    # Cosine similarity — both vectors are unit-norm so dot product suffices.
    sims = corpus_emb @ q_emb
    order = sims.argsort()[::-1][:top_k]
    return [{**chunks[i], "score": float(sims[i])} for i in order]


@lru_cache(maxsize=2)
def _cached_embedder(name: str):
    """SentenceTransformer constructor is slow (loads weights); cache it."""
    from sentence_transformers import SentenceTransformer  # type: ignore
    return SentenceTransformer(name)


@lru_cache(maxsize=2)
def _cached_corpus_embeddings(corpus_file: str, embedder: str):
    """Pre-compute corpus embeddings once per (corpus, embedder). For
    larger corpora consider a real vector DB (chroma/faiss); this is fine
    up to ~10k chunks."""
    import numpy as np  # type: ignore
    chunks = _load_corpus(corpus_file)
    model = _cached_embedder(embedder)
    texts = [c.get("text", "") for c in chunks]
    return model.encode(texts, normalize_embeddings=True, show_progress_bar=False)


# ---------------------------------------------------------------------------
# Hybrid (BM25 + dense, reciprocal rank fusion)
# ---------------------------------------------------------------------------

def hybrid_retrieve(project_dir: Path, query: str, top_k: int, *, embedder: str) -> list[dict]:
    """Reciprocal Rank Fusion of BM25 + dense. Robust default when you're
    not sure which retriever fits your corpus."""
    bm = bm25_retrieve(project_dir, query, top_k * 2)
    de = dense_retrieve(project_dir, query, top_k * 2, embedder=embedder)

    # RRF: score = sum(1 / (k + rank)) across rankers. k=60 is the
    # conventional constant from the original RRF paper.
    K = 60
    rrf: dict[str, dict] = {}
    for rank, item in enumerate(bm):
        rrf[item["doc_id"]] = {**item, "rrf": 1.0 / (K + rank + 1)}
    for rank, item in enumerate(de):
        prev = rrf.get(item["doc_id"], {**item, "rrf": 0.0})
        prev["rrf"] = prev.get("rrf", 0.0) + 1.0 / (K + rank + 1)
        rrf[item["doc_id"]] = prev

    ranked = sorted(rrf.values(), key=lambda x: x["rrf"], reverse=True)
    return [{**r, "score": r.pop("rrf")} for r in ranked[:top_k]]


# ---------------------------------------------------------------------------
# Chunking helper — used by tools/ingest_pdfs.py
# ---------------------------------------------------------------------------

def chunk_text(text: str, *, chunk_size_chars: int = 1500, overlap_chars: int = 200) -> Iterable[tuple[int, int, str]]:
    """Yield (char_start, char_end, chunk_text) tuples. Splits on paragraph
    boundaries where possible; falls back to character windows.

    chunk_size_chars: roughly 400 tokens at chars/4 heuristic — a sane
    default for English technical docs. Override per-project via the
    ingest CLI's --chunk-size flag.
    """
    if not text:
        return
    if len(text) <= chunk_size_chars:
        yield 0, len(text), text
        return

    # Prefer paragraph-aware splits when possible (\n\n boundaries).
    # We greedily pack paragraphs into chunks up to chunk_size_chars.
    paragraphs = re.split(r"\n\s*\n", text)
    if all(len(p) <= chunk_size_chars for p in paragraphs):
        buf = ""
        buf_start = 0
        pos = 0
        for p in paragraphs:
            # +2 for the paragraph separator we stripped.
            if buf and len(buf) + len(p) + 2 > chunk_size_chars:
                yield buf_start, buf_start + len(buf), buf
                buf = p
                buf_start = pos
            else:
                buf = (buf + "\n\n" + p) if buf else p
            pos += len(p) + 2
        if buf:
            yield buf_start, buf_start + len(buf), buf
        return

    # Paragraphs are too long — fall back to fixed-window with overlap.
    step = max(1, chunk_size_chars - overlap_chars)
    for start in range(0, len(text), step):
        end = min(start + chunk_size_chars, len(text))
        yield start, end, text[start:end]
        if end >= len(text):
            break

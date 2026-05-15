# lib/registry.py
# The single chokepoint for every model / embedder / retriever / reranker
# call the framework makes. Two reasons it's centralized:
#
#   1. Cost & latency accounting. Every call's tokens, USD, and ms are
#      attributed back to a TrialResult — no rogue paths that bypass the
#      budget ledger.
#   2. Vendor swap. The framework targets Anthropic primarily but should
#      not be welded to it; OpenAI / local models can plug in as alternate
#      backends without touching capabilities.
#
# All call entry points return tightly-typed result objects, never raw SDK
# responses, so a vendor swap doesn't leak through.

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional
import os
import time

from .schemas.variant import PromptBundle, RetrievalConfig, GenerationConfig


# ---------------------------------------------------------------------------
# Result objects
# ---------------------------------------------------------------------------

@dataclass
class ModelCallResult:
    """Output of a single (non-tool-using) LLM call."""
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    # `refused` is heuristic: True if the response matches a refusal
    # pattern. Used by the chatbot capability to feed refusal_calibration.
    refused: bool = False


@dataclass
class AgenticCallResult:
    """Aggregated output of a tool-use loop. The Operator does NOT see the
    intermediate tool calls — those go into the trial trace consumed by
    the sketch updater. Only the final answer and totals come back here."""
    final_text: str
    tool_calls: list[dict] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    total_latency_ms: float = 0.0


@dataclass
class StructuredCallResult:
    """Output of a structured-output / tool-schema-constrained call."""
    parsed: Any
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    schema_valid: bool = True


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------
#
# The framework picks a backend based on env vars at call time. We don't
# require *any* SDK to be installed — if none is available, calls fall
# through to a deterministic stub mode so tests and `genai replay` work
# in airgapped environments.

def _backend() -> str:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return "stub"


def log_backend_status() -> str:
    """Call once at startup to print which backend is active. Helps diagnose
    stub mode when the API key is present but not reaching the container."""
    import logging
    backend = _backend()
    log = logging.getLogger("evalsmith.registry")
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if backend == "anthropic":
        log.info("Backend: ANTHROPIC  (key present, prefix=%s...)", key[:18])
    elif backend == "openai":
        log.info("Backend: OPENAI  (key present)")
    else:
        log.warning(
            "Backend: STUB — no API keys found in os.environ. "
            "Set ANTHROPIC_API_KEY in .env and restart. "
            "All LLM outputs will be deterministic fake strings."
        )
    return backend


# ---------------------------------------------------------------------------
# Pricing table — used to estimate cost when the SDK doesn't report it.
# USD per 1M tokens. Update when pricing changes; kept here (not in env) so
# replays compute identical costs.
# ---------------------------------------------------------------------------

_PRICING_USD_PER_1M = {
    # Approximate ballpark numbers. The replay verifier compares within
    # ±10% so small pricing drifts don't invalidate prior runs.
    "claude-haiku-4-5":   {"in": 1.0,  "out": 5.0},
    "claude-sonnet-4-5":  {"in": 3.0,  "out": 15.0},
    "claude-sonnet-4-6":  {"in": 3.0,  "out": 15.0},   # newer Sonnet drop-in
    "claude-opus-4-1":    {"in": 15.0, "out": 75.0},
    "claude-opus-4-5":    {"in": 15.0, "out": 75.0},   # alias for newer Opus
    "gpt-4o-mini":        {"in": 0.15, "out": 0.6},
    "gpt-4o":             {"in": 2.5,  "out": 10.0},
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Token-count -> USD. Used both for live calls (when SDK doesn't
    return cost) and for the Strategist's pre-trial budget projection."""
    p = _PRICING_USD_PER_1M.get(model, {"in": 1.0, "out": 5.0})
    return (input_tokens * p["in"] + output_tokens * p["out"]) / 1_000_000


# ---------------------------------------------------------------------------
# Primary call surface
# ---------------------------------------------------------------------------

def model_call(
    *,
    system: str,
    user: str,
    generation: GenerationConfig,
    few_shots: Optional[list[tuple[str, str]]] = None,
) -> ModelCallResult:
    """Single-turn LLM call. The dominant call shape — used by rag_qa, nlq,
    insight_agent. For chat, see chat_call; for tool-use, see agentic_call.
    """
    backend = _backend()
    start = time.time()

    if backend == "stub":
        # Deterministic stub: echoes a fingerprint of the inputs so trials
        # are reproducible without API access. Tests rely on this.
        text = _stub_text(system, user, generation)
        in_tok = max(1, (len(system) + len(user)) // 4)
        out_tok = max(1, len(text) // 4)
        return ModelCallResult(
            text=text,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=estimate_cost(generation.model, in_tok, out_tok),
            latency_ms=(time.time() - start) * 1000,
            refused=_looks_like_refusal(text),
        )

    if backend == "anthropic":
        return _anthropic_call(system, user, generation, few_shots)
    if backend == "openai":
        return _openai_call(system, user, generation, few_shots)

    raise RuntimeError(f"Unknown backend: {backend}")


def chat_call(*, system: str, turns: list[dict], generation: GenerationConfig, few_shots=None) -> ModelCallResult:
    """Multi-turn chat call. The chatbot capability uses this."""
    # Concatenate prior turns into a single user payload for the stub path;
    # real backends pass them as a message list.
    if _backend() == "stub":
        user_repr = "\n".join(f"{t.get('role','user')}: {t.get('content','')}" for t in turns)
        return model_call(system=system, user=user_repr, generation=generation, few_shots=few_shots)
    if _backend() == "anthropic":
        return _anthropic_chat(system, turns, generation, few_shots)
    return _openai_chat(system, turns, generation, few_shots)


def structured_call(*, system: str, user: str, generation: GenerationConfig, chunking: RetrievalConfig) -> StructuredCallResult:
    """Structured-output call. Used by insight_agent / nlq when the model
    must emit JSON that conforms to a schema declared in
    generation.tools[0]['input_schema']."""
    raw = model_call(system=system, user=user, generation=generation)
    # In stub mode we just echo {}; in real backends we'd parse + validate.
    import json
    try:
        parsed = json.loads(raw.text) if raw.text.strip().startswith("{") else {"value": raw.text}
        valid = True
    except json.JSONDecodeError:
        parsed = {"value": raw.text}
        valid = False
    return StructuredCallResult(
        parsed=parsed,
        input_tokens=raw.input_tokens,
        output_tokens=raw.output_tokens,
        cost_usd=raw.cost_usd,
        latency_ms=raw.latency_ms,
        schema_valid=valid,
    )


def agentic_call(*, system: str, user: str, generation: GenerationConfig) -> AgenticCallResult:
    """Tool-use loop. Iterates up to generation.max_tool_iterations, calling
    declared tools and feeding their output back to the model."""
    # Stub implementation: pretend we ran 2 tool calls then produced an
    # answer. Keeps tests deterministic. Real backends would implement the
    # full tool-use loop via the Anthropic / OpenAI tool-use API.
    raw = model_call(system=system, user=user, generation=generation)
    return AgenticCallResult(
        final_text=raw.text,
        tool_calls=[
            {"name": "search", "input": {"q": user[:50]}, "output": "[stub-search-result]"},
            {"name": "summarize", "input": {"text": "..."}, "output": "[stub-summary]"},
        ] if _backend() == "stub" else [],
        total_input_tokens=raw.input_tokens,
        total_output_tokens=raw.output_tokens,
        total_cost_usd=raw.cost_usd,
        total_latency_ms=raw.latency_ms,
    )


# ---------------------------------------------------------------------------
# Retrieval / reranking surface
# ---------------------------------------------------------------------------

def retrieve(*, query: str, config: RetrievalConfig, corpus_dir=None) -> list[dict]:
    """Retrieve top-k docs for a query. Returns dicts with at least
    {doc_id, text} and optional {input_tokens, cost_usd, latency_ms} for
    cost attribution.

    Resolution order:
      1. If the caller passed a `corpus_dir` (a Path to a project workspace)
         AND that project has a non-empty `data/corpus.jsonl`, dispatch to
         `lib.corpus` for real BM25/dense/hybrid retrieval.
      2. Otherwise fall through to a deterministic stub keyed on the query
         hash — useful for tests and projects that don't actually have a
         corpus on disk yet.

    The stub is *not* a feature — it's a fallback so capabilities can be
    exercised end-to-end without ingesting documents first. Real RAG
    projects should always have `corpus.jsonl` populated.
    """
    if not config.enabled or config.retriever_kind == "none":
        return []

    # Real-corpus path. Lazy-imported because the corpus module pulls in
    # math/regex; we don't want to pay that cost in chatbot/NLQ runs that
    # never retrieve.
    from pathlib import Path as _Path
    from . import corpus as corpus_mod
    if corpus_dir is not None and corpus_mod.has_corpus(_Path(corpus_dir)):
        cdir = _Path(corpus_dir)
        if config.retriever_kind == "bm25":
            return corpus_mod.bm25_retrieve(cdir, query, config.top_k)
        if config.retriever_kind == "dense":
            return corpus_mod.dense_retrieve(cdir, query, config.top_k, embedder=config.embedder)
        if config.retriever_kind == "hybrid":
            return corpus_mod.hybrid_retrieve(cdir, query, config.top_k, embedder=config.embedder)

    # Fallback: deterministic stub. The hash-derived doc ids let trial
    # reproducibility hold even without a real corpus.
    import hashlib
    seed = int(hashlib.sha256(query.encode()).hexdigest()[:8], 16)
    return [
        {"doc_id": f"doc_{(seed + i) % 1000}", "text": f"[stub doc {(seed + i) % 1000} for query: {query[:30]}]"}
        for i in range(config.top_k)
    ]


def rerank(*, query: str, candidates: list[dict], reranker_name: str, generation: GenerationConfig, prompt: PromptBundle) -> list[dict]:
    """Rerank candidates. Stub mode reverses the list (so the test can
    verify reranking actually changed order); real backends call the
    reranker model."""
    if not candidates:
        return candidates
    if _backend() == "stub":
        return list(reversed(candidates))
    # Real backend: call the reranker. Left as a TODO for the user's
    # specific deployment.
    return candidates


# ---------------------------------------------------------------------------
# Private backend implementations
# ---------------------------------------------------------------------------

def _stub_text(system: str, user: str, gen: GenerationConfig) -> str:
    """Deterministic stub response. Length scales with max_tokens so
    different decoding configs produce different-sized outputs."""
    import hashlib
    h = hashlib.sha256((system + "||" + user + "||" + gen.model).encode()).hexdigest()[:12]
    return f"[stub:{gen.model}:{h}] Answer for input len {len(user)}."


def _looks_like_refusal(text: str) -> bool:
    """Heuristic refusal detector. The chatbot capability uses this to
    compute refusal_calibration. Conservative — false negatives are
    cheaper than false positives here."""
    refusal_markers = ("i can't help", "i cannot", "i'm not able", "i won't", "<<escalate>>")
    return any(m in text.lower() for m in refusal_markers)


def _anthropic_call(system, user, gen, few_shots) -> ModelCallResult:
    """Anthropic backend. Lazy-imports so the framework runs without the SDK."""
    import anthropic  # type: ignore
    client = anthropic.Anthropic()
    start = time.time()
    msgs = []
    if few_shots:
        for q, a in few_shots:
            msgs.append({"role": "user", "content": q})
            msgs.append({"role": "assistant", "content": a})
    msgs.append({"role": "user", "content": user})
    resp = client.messages.create(
        model=gen.model,
        system=system,
        max_tokens=gen.max_tokens,
        temperature=gen.temperature,
        messages=msgs,
    )
    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    in_tok = getattr(resp.usage, "input_tokens", 0)
    out_tok = getattr(resp.usage, "output_tokens", 0)
    return ModelCallResult(
        text=text, input_tokens=in_tok, output_tokens=out_tok,
        cost_usd=estimate_cost(gen.model, in_tok, out_tok),
        latency_ms=(time.time() - start) * 1000,
        refused=_looks_like_refusal(text),
    )


def _anthropic_chat(system, turns, gen, few_shots) -> ModelCallResult:
    """Multi-turn Anthropic chat."""
    import anthropic  # type: ignore
    client = anthropic.Anthropic()
    start = time.time()
    msgs = []
    if few_shots:
        for q, a in few_shots:
            msgs.extend([{"role": "user", "content": q}, {"role": "assistant", "content": a}])
    for t in turns:
        msgs.append({"role": t.get("role", "user"), "content": t.get("content", "")})
    resp = client.messages.create(
        model=gen.model, system=system, max_tokens=gen.max_tokens,
        temperature=gen.temperature, messages=msgs,
    )
    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    in_tok = getattr(resp.usage, "input_tokens", 0)
    out_tok = getattr(resp.usage, "output_tokens", 0)
    return ModelCallResult(
        text=text, input_tokens=in_tok, output_tokens=out_tok,
        cost_usd=estimate_cost(gen.model, in_tok, out_tok),
        latency_ms=(time.time() - start) * 1000,
        refused=_looks_like_refusal(text),
    )


def _openai_call(system, user, gen, few_shots) -> ModelCallResult:
    import openai  # type: ignore
    client = openai.OpenAI()
    start = time.time()
    msgs = [{"role": "system", "content": system}]
    if few_shots:
        for q, a in few_shots:
            msgs.extend([{"role": "user", "content": q}, {"role": "assistant", "content": a}])
    msgs.append({"role": "user", "content": user})
    resp = client.chat.completions.create(
        model=gen.model, messages=msgs,
        temperature=gen.temperature, max_tokens=gen.max_tokens,
    )
    text = resp.choices[0].message.content or ""
    in_tok = resp.usage.prompt_tokens
    out_tok = resp.usage.completion_tokens
    return ModelCallResult(
        text=text, input_tokens=in_tok, output_tokens=out_tok,
        cost_usd=estimate_cost(gen.model, in_tok, out_tok),
        latency_ms=(time.time() - start) * 1000,
        refused=_looks_like_refusal(text),
    )


def _openai_chat(system, turns, gen, few_shots) -> ModelCallResult:
    return _openai_call(system, "\n".join(t.get("content","") for t in turns), gen, few_shots)

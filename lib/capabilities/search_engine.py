# lib/capabilities/search_engine.py
# Search / recommendation engine capability. The system under test is a
# *ranking* pipeline: candidate generation (BM25/dense/hybrid) followed by
# an LLM-based reranker that produces a ranked list of doc_ids.
#
# Eval is over ranking metrics (NDCG, MAP@k) not generation quality.

from __future__ import annotations
from .base import CapabilityBase, CapabilityContext, RunOutput
from .registry import register_capability
from ..schemas.variant import Variant
from ..schemas.eval_case import EvalCase
from .. import registry as model_registry


@register_capability("search_engine")
class SearchEngineCapability(CapabilityBase):
    """LLM-augmented search/recommendation. EvalCase.input is the query;
    EvalCase.expected is a ranked list of doc_ids; the case is scored by
    ranking metrics from lib/eval.py.

    Levers:
      * retriever_change       — candidate generator swap
      * rerank_add_or_change   — main reranker tuning
      * prompt_rewrite         — reranker prompt + scoring criteria
      * model_swap             — reranker model (Haiku reranker vs Sonnet)
      * router_policy          — when to bypass rerank for cost reasons
    """

    primary_metrics = ["ndcg_at_10", "map_at_10"]
    secondary_metrics = ["cost_usd", "p95_latency_ms", "recall_at_50"]
    allowed_arms = [
        "retriever_change",
        "rerank_add_or_change",
        "prompt_rewrite",
        "model_swap",
        "router_policy",
    ]

    def run_single_case(self, variant: Variant, case: EvalCase, ctx: CapabilityContext) -> RunOutput:
        # 1. Generate candidates from the retriever.
        candidates = model_registry.retrieve(
            query=str(case.input),
            config=variant.retrieval,
            corpus_dir=None,
        )
        # 2. If a reranker is configured, call it; otherwise pass-through.
        if variant.retrieval.reranker:
            reranked = model_registry.rerank(
                query=str(case.input),
                candidates=candidates,
                reranker_name=variant.retrieval.reranker,
                generation=variant.generation,
                prompt=variant.prompt,
            )
        else:
            reranked = candidates

        return RunOutput(
            case_id=case.case_id,
            raw_output=[d["doc_id"] for d in reranked],
            input_tokens=sum(d.get("input_tokens", 0) for d in reranked),
            output_tokens=0,  # rerankers usually score, not generate
            cost_usd=sum(d.get("cost_usd", 0.0) for d in reranked),
            latency_ms=sum(d.get("latency_ms", 0.0) for d in reranked),
            retrieved_doc_ids=[d["doc_id"] for d in reranked],
        )

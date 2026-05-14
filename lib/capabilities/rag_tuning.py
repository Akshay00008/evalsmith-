# lib/capabilities/rag_tuning.py
# RAG QA tuning capability.
#
# The "system under test" is a retrieve-then-generate pipeline parameterized
# by Variant.retrieval and Variant.prompt + Variant.generation. The
# Strategist's job is to find the (chunking, retriever, top_k, reranker,
# prompt) tuple that maximizes judge_score subject to cost & latency.

from __future__ import annotations
from typing import Any

from .base import CapabilityBase, CapabilityContext, RunOutput
from .registry import register_capability
from ..schemas.variant import Variant
from ..schemas.eval_case import EvalCase
from .. import registry as model_registry


@register_capability("rag_qa")
class RagQaCapability(CapabilityBase):
    """RAG over a fixed corpus. EvalCase.input is a question (str);
    EvalCase.expected is the gold answer; EvalCase.relevant_doc_ids
    drives recall@k.

    The corpus is loaded from <project>/data/corpus/. We don't index it
    here — that's lib/registry.py's vectorstore() — we just consult the
    Variant.retrieval config and forward to it.
    """

    primary_metrics = ["judge_score", "recall_at_5"]
    secondary_metrics = ["cost_usd", "p95_latency_ms", "faithfulness"]
    allowed_arms = [
        # The full RAG toolbox. Each maps to a Strategist move pattern
        # documented in .claude/agents/strategist.md.
        "prompt_rewrite",
        "few_shot_selection",
        "model_swap",
        "retriever_change",
        "chunking_change",
        "rerank_add_or_change",
        "guardrail_add",
    ]

    def run_single_case(self, variant: Variant, case: EvalCase, ctx: CapabilityContext) -> RunOutput:
        # 1. Retrieve. The registry resolves to lib/corpus.py when the
        #    project has a real corpus.jsonl; otherwise falls back to a
        #    deterministic stub keyed on the query hash.
        retrieved = model_registry.retrieve(
            query=str(case.input),
            config=variant.retrieval,
            corpus_dir=ctx.project_dir,
        )

        # 2. Construct the prompt. The retrieved docs are concatenated
        #    into a {context} slot the user_template can reference.
        ctx_block = "\n\n".join(d["text"] for d in retrieved)
        rendered = variant.prompt.user_template.format(
            input=case.input,
            context=ctx_block,
        )

        # 3. Generate. model_call is the single chokepoint for cost +
        #    latency tracking; it returns a structured result.
        call = model_registry.model_call(
            system=variant.prompt.system,
            user=rendered,
            generation=variant.generation,
            few_shots=variant.prompt.few_shots,
        )

        return RunOutput(
            case_id=case.case_id,
            raw_output=call.text,
            input_tokens=call.input_tokens,
            output_tokens=call.output_tokens,
            cost_usd=call.cost_usd,
            latency_ms=call.latency_ms,
            retrieved_doc_ids=[d["doc_id"] for d in retrieved],
        )

# lib/capabilities/research_agent.py
# Research agent capability — open-ended multi-step research producing a
# cited answer. EvalCase.expected is typically *not* a single string but a
# list of "must-cite" facts; the judge scores citation quality + faithfulness
# + coverage.

from __future__ import annotations
from .base import CapabilityBase, CapabilityContext, RunOutput
from .registry import register_capability
from ..schemas.variant import Variant
from ..schemas.eval_case import EvalCase
from .. import registry as model_registry


@register_capability("research_agent")
class ResearchAgentCapability(CapabilityBase):
    """Multi-step research agent.

    Levers:
      * tool_schema_edit   — what does the search/browse tool look like?
      * prompt_rewrite     — research planning prompt
      * model_swap         — Sonnet for planning, Haiku for summarization
      * decoding_params    — sometimes higher temp helps exploration
      * guardrail_add      — citation enforcement, hallucination checks
    """

    primary_metrics = ["citation_quality", "faithfulness", "coverage"]
    secondary_metrics = ["cost_usd", "p95_latency_ms", "n_tool_calls_avg"]
    allowed_arms = [
        "prompt_rewrite",
        "model_swap",
        "tool_schema_edit",
        "decoding_params",
        "guardrail_add",
    ]

    def run_single_case(self, variant: Variant, case: EvalCase, ctx: CapabilityContext) -> RunOutput:
        # The tool-use loop lives behind registry.agentic_call — it iterates
        # tool calls up to generation.max_tool_iterations and aggregates the
        # cost/latency across all calls in the loop.
        loop = model_registry.agentic_call(
            system=variant.prompt.system,
            user=variant.prompt.user_template.format(input=case.input),
            generation=variant.generation,
        )
        return RunOutput(
            case_id=case.case_id,
            raw_output=loop.final_text,
            input_tokens=loop.total_input_tokens,
            output_tokens=loop.total_output_tokens,
            cost_usd=loop.total_cost_usd,
            latency_ms=loop.total_latency_ms,
            tool_calls=loop.tool_calls,
        )

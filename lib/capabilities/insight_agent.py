# lib/capabilities/insight_agent.py
# Insight agent capability — extract structured insights from unstructured
# documents. EvalCase.input is a document or list of documents;
# EvalCase.expected is a structured object (list of insights, each with
# evidence span). The judge or a structured-output check scores precision/
# recall over the insight set.

from __future__ import annotations
from .base import CapabilityBase, CapabilityContext, RunOutput
from .registry import register_capability
from ..schemas.variant import Variant
from ..schemas.eval_case import EvalCase
from .. import registry as model_registry


@register_capability("insight_agent")
class InsightAgentCapability(CapabilityBase):
    """Structured insight extraction.

    Levers:
      * prompt_rewrite        — the instruction + schema
      * tool_schema_edit      — the JSON schema the model emits against
      * model_swap            — recall heavily depends on model strength
      * chunking_change       — long docs need sliding-window chunking
      * guardrail_add         — output validation, retry on schema failure
    """

    primary_metrics = ["insight_precision", "insight_recall", "schema_validity"]
    secondary_metrics = ["cost_usd", "p95_latency_ms", "evidence_grounding"]
    allowed_arms = [
        "prompt_rewrite",
        "tool_schema_edit",
        "model_swap",
        "chunking_change",
        "guardrail_add",
        "decoding_params",
    ]

    def run_single_case(self, variant: Variant, case: EvalCase, ctx: CapabilityContext) -> RunOutput:
        # Insight agents typically use structured output (tool schema or
        # JSON mode). registry.structured_call enforces the schema with a
        # retry on parse failure — that retry's cost is rolled into the
        # returned cost_usd.
        result = model_registry.structured_call(
            system=variant.prompt.system,
            user=variant.prompt.user_template.format(input=case.input),
            generation=variant.generation,
            chunking=variant.retrieval,  # chunking config is reused even though we don't retrieve
        )
        return RunOutput(
            case_id=case.case_id,
            raw_output=result.parsed,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
        )

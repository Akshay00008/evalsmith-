# lib/capabilities/nlq.py
# Natural Language Query capability — NL question -> structured query
# (SQL, GraphQL, an API call, or a DSL fragment).
#
# Critical here is *tool_call_match* eval strategy: we compare the produced
# query against an expected canonical query rather than running it (which
# would require live DB access during eval). For projects that want
# execution-equivalence eval, the recipe can opt into running the query in
# a sandboxed read-only DB — see recipes/nlq_sql.json.

from __future__ import annotations
from .base import CapabilityBase, CapabilityContext, RunOutput
from .registry import register_capability
from ..schemas.variant import Variant
from ..schemas.eval_case import EvalCase
from .. import registry as model_registry


@register_capability("nlq_to_query")
class NlqCapability(CapabilityBase):
    """NL -> query. Variant.prompt.system holds the schema description /
    DSL guide; Variant.prompt.few_shots holds example (NL, query) pairs.

    The Strategist's main levers here are:
      * prompt_rewrite       — refine the schema description
      * few_shot_selection   — which examples best teach the patterns?
      * model_swap           — Haiku for simple SELECTs, Sonnet for joins
      * tool_schema_edit     — if we wrap the call in a structured tool
    """

    primary_metrics = ["exact_match_normalized", "execution_equivalence"]
    secondary_metrics = ["cost_usd", "p95_latency_ms", "syntactic_validity"]
    allowed_arms = [
        "prompt_rewrite",
        "few_shot_selection",
        "model_swap",
        "tool_schema_edit",
        "decoding_params",
    ]

    def run_single_case(self, variant: Variant, case: EvalCase, ctx: CapabilityContext) -> RunOutput:
        # NLQ does not retrieve over a corpus, so we ignore variant.retrieval.
        rendered = variant.prompt.user_template.format(input=case.input)
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
        )

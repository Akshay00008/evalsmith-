# lib/capabilities/chatbot.py
# Conversational chatbot capability. EvalCase.input is a *turn list*
# (list of {role, content} dicts); EvalCase.expected is the gold final
# assistant message OR a rubric the judge applies.
#
# Special considerations:
#   * Multi-turn means context length grows; chunking is irrelevant but
#     conversation summarization (registry.summarize_context) becomes
#     a lever.
#   * Refusal calibration is its own metric. Over-refusal is as bad as
#     under-refusal — the Provoker tracks both.

from __future__ import annotations
from .base import CapabilityBase, CapabilityContext, RunOutput
from .registry import register_capability
from ..schemas.variant import Variant
from ..schemas.eval_case import EvalCase
from .. import registry as model_registry


@register_capability("chatbot")
class ChatbotCapability(CapabilityBase):
    """Multi-turn chatbot.

    Levers:
      * prompt_rewrite       — system prompt, persona, safety preamble
      * few_shot_selection   — exemplar conversations
      * model_swap           — model size driven by cost/latency floor
      * router_policy        — route easy turns to Haiku, complex to Sonnet
      * guardrail_add        — refusal calibration, PII redaction
      * decoding_params      — temperature affects conversational variety
    """

    primary_metrics = ["judge_score", "task_success_rate"]
    secondary_metrics = ["cost_usd", "p95_latency_ms", "refusal_calibration", "turn_coherence"]
    allowed_arms = [
        "prompt_rewrite",
        "few_shot_selection",
        "model_swap",
        "router_policy",
        "guardrail_add",
        "decoding_params",
    ]

    def run_single_case(self, variant: Variant, case: EvalCase, ctx: CapabilityContext) -> RunOutput:
        # case.input is a list of {role, content} dicts. We hand the whole
        # transcript to model_call; the registry handles the platform-
        # specific message-formatting (Anthropic Messages API vs OpenAI).
        turns = case.input if isinstance(case.input, list) else [{"role": "user", "content": str(case.input)}]
        call = model_registry.chat_call(
            system=variant.prompt.system,
            turns=turns,
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
            refused=call.refused,
        )

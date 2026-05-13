# lib/capabilities/base.py
# Capability base class + execution context. The base class defines the
# contract every capability must implement; the context bundles the
# read-only artifacts (Mission, EvalSet, knowledge) a capability needs.

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Optional
from abc import ABC, abstractmethod

from ..schemas.mission import Mission
from ..schemas.variant import Variant, TechniqueFamily
from ..schemas.eval_case import EvalCase, EvalSet


@dataclass(frozen=True)
class CapabilityContext:
    """Read-only bundle of artifacts a capability consults during run."""
    mission: Mission
    eval_set: EvalSet
    # Knowledge library snippets relevant to this capability (loaded by
    # lib/retrieval.py from knowledge/ at /run start). May be empty for
    # the very first project on a fresh framework install.
    knowledge_snippets: list[dict]


class CapabilityBase(ABC):
    """Abstract interface every capability implements."""

    # Identifier matching Mission.composition.task_modality.
    name: str = ""

    # The list of metric names this capability cares about. lib/eval.py
    # restricts metric computation to this whitelist so we don't waste
    # cycles computing recall@k for a chatbot mission.
    primary_metrics: list[str] = []
    secondary_metrics: list[str] = ["cost_usd", "p95_latency_ms"]

    # The technique families that make sense to try for this capability.
    # The bandit is initialized only over these arms — keeps the action
    # space lean.
    allowed_arms: list[TechniqueFamily] = []

    @abstractmethod
    def run_single_case(self, variant: Variant, case: EvalCase, ctx: CapabilityContext) -> "RunOutput":
        """Execute one EvalCase against a Variant. Returns a RunOutput with
        the produced answer plus any per-case telemetry needed by the
        sketch updater. Implementations may be no-LLM stubs for testing —
        the real LLM call lives behind lib/registry.py's model_call()."""
        raise NotImplementedError


@dataclass
class RunOutput:
    """Per-case execution output. Aggregated into a TrialResult by run.py.

    Kept as a dataclass (not pydantic) because it's hot-path and we don't
    cross any agent boundary with it — it's local to one Operator call."""
    case_id: str
    raw_output: Any
    # Tokens / cost / latency for *this* case. Aggregated up to TrialResult.
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    # Optional: doc ids retrieved (RAG), tool calls made (agent), refusal
    # flag (chatbot), etc. Used by the sketch updater for E4/E5/E7 layers.
    retrieved_doc_ids: Optional[list[str]] = None
    tool_calls: Optional[list[dict]] = None
    refused: bool = False
    # Provoker uses this when running red-team variants; carries the
    # pattern_id that the input case came from.
    safety_pattern_id: Optional[str] = None

# lib/schemas/eval_case.py
# EvalCase / EvalSet — the *frozen* test bed for a Mission. The hash of an
# EvalSet is pinned into the Mission so the framework can detect tampering.
#
# Importantly: subagents never see raw EvalCases. They query the eval-sketch
# (lib/sketch/) for *summaries* (slice performance, failure clusters). The
# Operator subagent is the only one that actually runs cases through the
# system under test.

from __future__ import annotations
from typing import Optional, Any
from pydantic import BaseModel, Field
import hashlib
import json


class EvalCase(BaseModel):
    """A single test case in the eval set."""
    case_id: str = Field(..., description="Stable id; survives shuffling, used for trace correlation.")

    # The input the system under test receives. Free-form because the
    # modality varies: for chatbots it's a turn list, for RAG it's a query
    # string, for NLQ it's a natural language question.
    input: Any

    # Optional reference output. May be:
    #   - a string (for exact_match or judge_llm strategies)
    #   - a structured object (for tool_call_match)
    #   - None (for open-ended research agents where the judge scores on
    #     other criteria like citation quality)
    expected: Optional[Any] = None

    # Free-form tags used by the sketch to slice metrics (e.g. ["math",
    # "multi-hop", "long-context"]). The sketch's E3 layer aggregates
    # per-tag performance.
    tags: list[str] = Field(default_factory=list)

    # Difficulty hint — usually populated during /init by sampling a baseline
    # model and bucketing by its success rate. Used by the bandit to weight
    # hard cases more heavily when scoring techniques.
    difficulty: Optional[float] = None

    # Required reference documents for RAG missions — used to compute
    # retrieval recall@k. None for non-RAG missions.
    relevant_doc_ids: Optional[list[str]] = None


class EvalSet(BaseModel):
    """Frozen collection of EvalCases plus metadata."""
    eval_set_id: str
    cases: list[EvalCase]
    # Optional held-out split used by judges.py for LLM-judge calibration.
    # The Auditor warns if `gold_calibration_case_ids` is empty for a
    # judge_llm mission, because we can't bound judge drift without it.
    gold_calibration_case_ids: list[str] = Field(default_factory=list)

    def content_hash(self) -> str:
        """Stable hash over the *content* of every case (in id order).
        Pinned into Mission.eval_set_hash so we can detect silent edits."""
        payload = json.dumps(
            [c.model_dump(mode="json") for c in sorted(self.cases, key=lambda c: c.case_id)],
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def __len__(self) -> int:
        return len(self.cases)

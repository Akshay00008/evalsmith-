# lib/schemas/judge.py
# Judge schemas. The "judge" is the LLM-as-judge component used when
# Mission.composition.eval_strategy == "judge_llm". Because the judge is
# itself an LLM, its drift directly affects every metric — so we treat it
# as a first-class versioned artifact with calibration data.

from __future__ import annotations
from typing import Optional, Literal
from pydantic import BaseModel, Field
import hashlib
import time


class JudgeSpec(BaseModel):
    """The judge configuration. Pinned per Mission so a re-run uses the
    *same* judge — otherwise we can't compare trials across iterations."""
    judge_id: str
    model: str = "claude-sonnet-4-5"
    system_prompt: str
    # The output schema the judge must produce. We constrain to a small
    # enum to keep aggregation deterministic; free-form rationale is
    # captured but not used in scoring.
    score_kind: Literal["binary", "likert_5", "rubric_pass_fail"] = "likert_5"
    # Optional rubric items, used by `rubric_pass_fail`. The judge must
    # report pass/fail on each; overall score is fraction passed.
    rubric_items: list[str] = Field(default_factory=list)

    @classmethod
    def compute_id(cls, model: str, system_prompt: str, score_kind: str, rubric_items: list[str]) -> str:
        # Same recipe as other ids — content hash, prefix to 16 chars.
        payload = "|".join([model, system_prompt, score_kind] + list(rubric_items))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class JudgeCalibration(BaseModel):
    """Output of running the judge against the gold-labeled calibration
    subset. The Auditor reads this to bound metric trust.

    Concretely: if `human_agreement < 0.7`, the Auditor downgrades every
    judge_score-based win claim to ACCEPT_WITH_WARN and forces the
    Inspector to do a qualitative spot check."""
    judge_id: str
    measured_at_unix: float = Field(default_factory=time.time)
    # Agreement vs human gold labels (Cohen's kappa or simple accuracy
    # depending on score_kind). 1.0 = perfect, 0.0 = chance.
    human_agreement: float
    # How many gold cases were used. <10 => the auditor refuses to trust
    # this calibration at all.
    n_gold_cases: int
    # Per-tag agreement, so we can detect that the judge is reliable on
    # short answers but flaky on long-context cases, for example.
    per_tag_agreement: dict[str, float] = Field(default_factory=dict)


class JudgeReport(BaseModel):
    """One judge call's output, on one (variant_output, eval_case) pair.
    Aggregated up into MetricSnapshot by lib/eval.py."""
    case_id: str
    trial_id: str
    score: float          # normalized into [0, 1] for aggregation
    raw_score: float      # raw judge output (e.g. 4 on a likert_5)
    rationale: str = ""
    rubric_results: Optional[dict[str, bool]] = None

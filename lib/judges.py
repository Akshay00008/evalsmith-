# lib/judges.py
# LLM-as-judge implementation + calibration. The judge is a first-class
# versioned artifact because its drift directly affects every metric.
#
# Flow:
#   1. /plan picks a JudgeSpec (or uses a recipe default).
#   2. /init runs the judge over gold_calibration_case_ids using
#      pre-labeled human scores; result -> JudgeCalibration.
#   3. /run uses the judge to score new trial outputs; periodically
#      re-runs calibration to detect drift.
#
# The Auditor consults the latest JudgeCalibration before accepting any
# judge_score-based win claim.

from __future__ import annotations
from pathlib import Path
from typing import Optional
import json

from .schemas.judge import JudgeSpec, JudgeReport, JudgeCalibration
from .schemas.eval_case import EvalSet, EvalCase
from .schemas.variant import GenerationConfig
from .capabilities.base import RunOutput
from . import registry as model_registry


# ---------------------------------------------------------------------------
# Score normalization
# ---------------------------------------------------------------------------

def _normalize_score(raw: float, score_kind: str) -> float:
    """Map raw judge output to [0,1] for aggregation. Likert_5 -> divide by
    5; binary -> already 0/1; rubric -> caller already passed the fraction."""
    if score_kind == "binary":
        return float(min(1.0, max(0.0, raw)))
    if score_kind == "likert_5":
        return min(1.0, max(0.0, raw / 5.0))
    if score_kind == "rubric_pass_fail":
        return float(min(1.0, max(0.0, raw)))
    return float(raw)


# ---------------------------------------------------------------------------
# Judge invocation
# ---------------------------------------------------------------------------

def judge_outputs(
    judge: JudgeSpec,
    outputs: list[RunOutput],
    eval_set: EvalSet,
    *,
    trial_id: str,
) -> list[JudgeReport]:
    """Run the judge over every output. Each judge call is one model_call;
    cost is accounted to the budget via the caller (run.py).

    The judge is given (input, expected, actual) and emits a score + rationale.
    """
    reports: list[JudgeReport] = []
    by_id = {c.case_id: c for c in eval_set.cases}

    for o in outputs:
        case = by_id.get(o.case_id)
        if case is None:
            continue

        prompt = _build_judge_prompt(judge, case, o.raw_output)
        # Use a fixed temperature=0 for judges so scores are reproducible.
        result = model_registry.model_call(
            system=judge.system_prompt,
            user=prompt,
            generation=GenerationConfig(model=judge.model, temperature=0.0, max_tokens=512),
        )
        raw_score, rationale, rubric = _parse_judge_output(result.text, judge)
        reports.append(JudgeReport(
            case_id=case.case_id,
            trial_id=trial_id,
            score=_normalize_score(raw_score, judge.score_kind),
            raw_score=raw_score,
            rationale=rationale,
            rubric_results=rubric,
        ))
    return reports


def _build_judge_prompt(judge: JudgeSpec, case: EvalCase, actual_output) -> str:
    """The user-prompt format the judge sees. Kept here so we can iterate
    on judge prompting without touching the rest of the framework."""
    parts = [
        f"INPUT:\n{case.input}\n",
        f"EXPECTED:\n{case.expected}\n" if case.expected is not None else "",
        f"ACTUAL OUTPUT:\n{actual_output}\n",
    ]
    if judge.score_kind == "rubric_pass_fail" and judge.rubric_items:
        rubric = "\n".join(f"- {item}" for item in judge.rubric_items)
        parts.append(f"RUBRIC (return JSON {{item_label: pass/fail, ...}}):\n{rubric}\n")
    elif judge.score_kind == "likert_5":
        parts.append("Return a JSON object {\"score\": int 1..5, \"rationale\": str}.")
    else:
        parts.append("Return a JSON object {\"score\": 0 or 1, \"rationale\": str}.")
    return "\n".join(p for p in parts if p)


def _parse_judge_output(text: str, judge: JudgeSpec):
    """Tolerant JSON parsing — judges sometimes wrap output in prose. We
    locate the first {...} block and parse that; on failure we default to
    a conservative score of 0 and flag the parse issue in the rationale."""
    import re
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return 0.0, f"[parse-failure] raw: {text[:200]}", None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return 0.0, f"[json-error] raw: {text[:200]}", None

    if judge.score_kind == "rubric_pass_fail":
        rubric = {k: bool(v) for k, v in obj.items() if k != "rationale"}
        score = sum(rubric.values()) / max(1, len(rubric))
        return score, obj.get("rationale", ""), rubric
    return float(obj.get("score", 0)), obj.get("rationale", ""), None


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

def calibrate(
    judge: JudgeSpec,
    eval_set: EvalSet,
    *,
    gold_labels: dict[str, float],   # case_id -> human-assigned normalized score
) -> JudgeCalibration:
    """Compare judge output against human gold labels on the calibration
    subset. We score *normalized* scores so the comparison is uniform across
    score_kinds.

    The Auditor consults `human_agreement`:
       >= 0.85 -> judge_score trusted
       0.70-0.85 -> WARN, downgrade confidence tier
       <  0.70 -> FAIL the run, demand a new judge
    """
    gold_cases = [c for c in eval_set.cases if c.case_id in gold_labels]
    if not gold_cases:
        return JudgeCalibration(judge_id=judge.judge_id, human_agreement=0.0, n_gold_cases=0)

    # Build stub RunOutputs containing each case's expected (treat as
    # actual) — calibration ought to be perfect *if* the judge is correctly
    # rewarding the gold answer. Drift over time = judge degradation.
    stub_outputs = [RunOutput(case_id=c.case_id, raw_output=c.expected) for c in gold_cases]
    reports = judge_outputs(judge, stub_outputs, eval_set, trial_id="calibration")

    # Simple accuracy-style agreement: 1 - mean abs error in [0,1].
    diffs = []
    per_tag_diffs: dict[str, list[float]] = {}
    by_id = {c.case_id: c for c in gold_cases}
    for r in reports:
        gold = gold_labels[r.case_id]
        diff = abs(r.score - gold)
        diffs.append(diff)
        for t in by_id[r.case_id].tags:
            per_tag_diffs.setdefault(t, []).append(diff)

    if not diffs:
        agreement = 0.0
    else:
        agreement = 1.0 - (sum(diffs) / len(diffs))
    per_tag = {t: 1.0 - sum(ds) / len(ds) for t, ds in per_tag_diffs.items()}

    return JudgeCalibration(
        judge_id=judge.judge_id,
        human_agreement=agreement,
        n_gold_cases=len(diffs),
        per_tag_agreement=per_tag,
    )


def latest_calibration(project_dir: Path) -> Optional[JudgeCalibration]:
    """Read the most recent calibration from disk. The Auditor calls this
    each iteration to ensure judge trust is still in bounds."""
    p = project_dir / "memory" / "JUDGE_CALIBRATION.json"
    if not p.exists():
        return None
    return JudgeCalibration.model_validate_json(p.read_text(encoding="utf-8"))


def write_calibration(project_dir: Path, calib: JudgeCalibration) -> None:
    """Persist calibration. Single-file (not append-only) because the
    Auditor only needs the *latest* — historical drift can be reconstructed
    by replaying."""
    p = project_dir / "memory" / "JUDGE_CALIBRATION.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(calib.model_dump_json(indent=2), encoding="utf-8")

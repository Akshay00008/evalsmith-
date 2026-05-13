# lib/skeptic.py
# The Auditor's deterministic checks. The Mediator (LLM) only steps in when
# Strategist and Auditor disagree — these checks themselves are pure Python
# so they're replayable and not subject to LLM drift.
#
# Mirrors the source repo's capability-dispatched skeptic. The check set
# active for a given trial depends on the Mission's task_modality.

from __future__ import annotations
from pathlib import Path
from typing import Optional
import hashlib

from .schemas.mission import Mission
from .schemas.variant import Variant
from .schemas.eval_case import EvalSet
from .schemas.trial import TrialResult
from .schemas.plan import StrategistProposal, AuditorVerdict, CheckResult
from .schemas.judge import JudgeCalibration


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def audit_proposal(
    *,
    mission: Mission,
    proposal: StrategistProposal,
    variant_full: Variant,
    eval_set: EvalSet,
    prior_trials: list[TrialResult],
    judge_calibration: Optional[JudgeCalibration],
    project_dir: Path,
) -> AuditorVerdict:
    """Run every applicable check against a proposed Variant.

    Returns an AuditorVerdict whose `verdict` is the *maximum severity* of
    any check that ran. We don't short-circuit on the first failure —
    seeing every concurrent issue helps the Strategist on retry.
    """
    checks: list[CheckResult] = []

    # Always-on checks
    checks.append(_check_prior_evidence_present(proposal))
    checks.append(_check_field_diff_nonempty(proposal))
    checks.append(_check_cost_projection(mission, variant_full, eval_set))
    checks.append(_check_eval_contamination(variant_full, eval_set))

    # Conditional checks — only matter for some modalities
    if mission.composition.eval_strategy == "judge_llm":
        checks.append(_check_judge_calibration(judge_calibration))

    if prior_trials and proposal.predicted_delta is not None:
        checks.append(_check_overclaim(proposal, prior_trials))

    # Compute aggregate verdict.
    severities = [c.severity for c in checks]
    if "catastrophic" in severities:
        verdict = "CATASTROPHIC"
        fail_reason = next(c.detail for c in checks if c.severity == "catastrophic")
        warn_reason = None
    elif "fail" in severities:
        verdict = "FAIL"
        fail_reason = next(c.detail for c in checks if c.severity == "fail")
        warn_reason = None
    elif "warn" in severities:
        verdict = "WARN"
        warn_reason = next(c.detail for c in checks if c.severity == "warn")
        fail_reason = None
    else:
        verdict = "ACCEPT"
        warn_reason = None
        fail_reason = None

    return AuditorVerdict(
        iteration=proposal.iteration,
        proposal_variant_id=variant_full.variant_id,
        verdict=verdict,
        check_results=checks,
        warn_reason=warn_reason,
        fail_reason=fail_reason,
    )


# ---------------------------------------------------------------------------
# Individual checks. Each is a pure function that returns a CheckResult.
# Adding a check: implement, add to audit_proposal above with the right
# conditional gating. Don't try to be clever about ordering — severity
# aggregation is what determines the verdict.
# ---------------------------------------------------------------------------

def _check_prior_evidence_present(proposal: StrategistProposal) -> CheckResult:
    """Refuse proposals that don't cite *why* this variant is worth trying.
    This is the structural guardrail against the Strategist hallucinating
    a 'try the bigger model' suggestion with no rationale."""
    pe = proposal.prior_evidence
    if not pe.reference:
        return CheckResult(name="prior_evidence_present", passed=False, severity="fail",
                           detail="Proposal missing prior_evidence.reference. Strategist must cite a sketch query, prior trial, seed, knowledge entry, or domain prior.")
    return CheckResult(name="prior_evidence_present", passed=True, severity="ok",
                       detail=f"Cited {pe.kind}: {pe.reference}")


def _check_field_diff_nonempty(proposal: StrategistProposal) -> CheckResult:
    """A diff with zero field changes is a no-op — usually a Strategist bug."""
    if not proposal.diff.field_changes:
        return CheckResult(name="field_diff_nonempty", passed=False, severity="fail",
                           detail="VariantDiff.field_changes is empty; nothing to test.")
    return CheckResult(name="field_diff_nonempty", passed=True, severity="ok",
                       detail=f"{len(proposal.diff.field_changes)} field(s) changed.")


def _check_cost_projection(mission: Mission, v: Variant, es: EvalSet) -> CheckResult:
    """Project the cost of running this variant over the full eval set.
    Block proposals that would blow the remaining budget on one trial."""
    # Heuristic: per-case cost = (avg_input_tokens + max_tokens) * pricing.
    # We use the eval profile when available; fall back to a flat estimate.
    from .registry import estimate_cost
    avg_input_tokens = 400   # ballpark; refined when E1 is available
    per_case = estimate_cost(v.generation.model, avg_input_tokens, v.generation.max_tokens)
    projected = per_case * len(es)
    # 25% of total budget is the soft ceiling per trial.
    if projected > 0.25 * mission.total_budget_usd:
        return CheckResult(
            name="cost_projection", passed=False, severity="warn",
            detail=f"Projected trial cost ${projected:.2f} exceeds 25% of total budget "
                   f"${mission.total_budget_usd:.2f}. Consider a smaller model or sample.",
        )
    return CheckResult(name="cost_projection", passed=True, severity="ok",
                       detail=f"Projected ${projected:.2f} (~{100*projected/mission.total_budget_usd:.1f}% of total).")


def _check_eval_contamination(v: Variant, es: EvalSet) -> CheckResult:
    """Detect the cardinal sin: the Strategist embedded an eval question
    verbatim into the system prompt (or a few-shot example). We do a
    cheap substring scan, not full embedding similarity, because the dev
    loop must stay fast — a slow auditor is an unused auditor.
    """
    haystack = (v.prompt.system + "\n" + v.prompt.user_template).lower()
    for q, _a in v.prompt.few_shots:
        haystack += "\n" + q.lower()
    # Sample up to 50 cases — full scan is O(n*m) which is fine for typical
    # eval sets but we still bound it.
    sample = es.cases[:50]
    for case in sample:
        text = str(case.input).lower()
        if len(text) > 20 and text in haystack:
            return CheckResult(
                name="eval_contamination", passed=False, severity="catastrophic",
                detail=f"Eval case {case.case_id!r} appears verbatim in the variant's prompt. "
                       f"This contaminates the metric and the run must halt.",
            )
    return CheckResult(name="eval_contamination", passed=True, severity="ok",
                       detail=f"Scanned {len(sample)} cases; no verbatim contamination.")


def _check_judge_calibration(calib: Optional[JudgeCalibration]) -> CheckResult:
    """Refuse judge_score-based wins when judge agreement is too low."""
    if calib is None:
        return CheckResult(name="judge_calibration", passed=False, severity="warn",
                           detail="No judge calibration available; judge_score wins will be reported as low-confidence.")
    if calib.n_gold_cases < 10:
        return CheckResult(name="judge_calibration", passed=False, severity="warn",
                           detail=f"Only {calib.n_gold_cases} gold cases; need >=10 to trust judge.")
    if calib.human_agreement < 0.70:
        return CheckResult(name="judge_calibration", passed=False, severity="fail",
                           detail=f"Judge agreement {calib.human_agreement:.2f} < 0.70 floor. Recalibrate or swap judge.")
    if calib.human_agreement < 0.85:
        return CheckResult(name="judge_calibration", passed=True, severity="warn",
                           detail=f"Judge agreement {calib.human_agreement:.2f} in 0.70-0.85 band; downgrade confidence in FINAL.md.")
    return CheckResult(name="judge_calibration", passed=True, severity="ok",
                       detail=f"Judge agreement {calib.human_agreement:.2f} — trusted.")


def _check_overclaim(proposal: StrategistProposal, prior_trials: list[TrialResult]) -> CheckResult:
    """Flag predictions that vastly exceed historical effect size for the
    technique family. Doesn't block — just warns and gets surfaced in the
    Inspector's synthesis."""
    arm = proposal.arm
    deltas = []
    # Crude: look at per-arm improvement over the *prior* trial in the log.
    for i in range(1, len(prior_trials)):
        if prior_trials[i].variant_id == proposal.diff.parent_variant_id:
            continue
        # Take the primary metric (first one in the list).
        if prior_trials[i].metrics and prior_trials[i-1].metrics:
            cur = prior_trials[i].metrics[0].value
            prev = prior_trials[i-1].metrics[0].value
            deltas.append(cur - prev)
    if not deltas or proposal.predicted_delta is None:
        return CheckResult(name="overclaim", passed=True, severity="ok", detail="No prior data to compare.")
    import statistics
    typical = statistics.mean([abs(d) for d in deltas]) if deltas else 0.0
    if abs(proposal.predicted_delta) > 5 * max(typical, 0.01):
        return CheckResult(name="overclaim", passed=True, severity="warn",
                           detail=f"Predicted delta {proposal.predicted_delta:.3f} >> typical |delta| {typical:.3f}. Suspect overclaim.")
    return CheckResult(name="overclaim", passed=True, severity="ok",
                       detail=f"Predicted delta {proposal.predicted_delta:.3f} in line with typical {typical:.3f}.")

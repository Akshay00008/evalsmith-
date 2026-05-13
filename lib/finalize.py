# lib/finalize.py
# Curator-side finalization. Produces FINAL.md — the counterfactual
# recommendation that ships out of the run. The Curator subagent's job is
# the prose; this module assembles the structured fields.
#
# Confidence tiers and gating logic mirror the source repo:
#   high      — judge calibration high, CIs tight, multiple supporting trials
#   medium    — calibration ok but small n, or wide CIs
#   low       — calibration ok but only one supporting trial, or warn-laden
#   no_signal — honest failure; collect more data

from __future__ import annotations
from pathlib import Path
from pydantic import BaseModel, Field
from typing import Literal, Optional
import json

from .schemas.mission import Mission
from .schemas.trial import TrialResult
from .schemas.judge import JudgeCalibration
from .schemas.state import RunState


ConfidenceTier = Literal["high", "medium", "low", "no_signal"]


class FinalRecommendation(BaseModel):
    """Structured output backing FINAL.md. The Curator can stylize the
    prose around this but the fields are authoritative."""
    mission_id: str
    confidence: ConfidenceTier
    decision_one_sentence: str
    rationale: str
    # Quantified counterfactual: (point estimate, ci low, ci high) on
    # primary metric vs the baseline trial.
    primary_metric_name: str
    primary_metric_baseline: float
    primary_metric_winner: float
    delta_point: float
    delta_ci_low: Optional[float] = None
    delta_ci_high: Optional[float] = None
    # Cost & latency tradeoffs explicitly surfaced.
    cost_delta_usd_per_1k: Optional[float] = None
    latency_delta_p95_ms: Optional[float] = None
    # Evidence chain — the trial_ids that support the claim. ≥3 → high tier
    # eligible; 1-2 → medium/low; 0 → no_signal.
    evidence_trial_ids: list[str] = Field(default_factory=list)
    # Caveats the user must agree to before deploying.
    causal_assumptions: list[str] = Field(default_factory=list)
    retraction_conditions: list[str] = Field(default_factory=list)
    # Failure modes the winner does NOT solve.
    unresolved_failure_modes: list[str] = Field(default_factory=list)


def assemble_recommendation(
    *,
    project_dir: Path,
    mission: Mission,
    run_state: RunState,
    log: list[TrialResult],
    judge_calibration: Optional[JudgeCalibration],
) -> FinalRecommendation:
    """Build the FinalRecommendation. The Curator subagent calls this
    after the run terminates; it then renders FINAL.md prose around the
    returned object."""

    if not log or not any(t.metrics for t in log):
        return _no_signal(mission, "No trials produced metrics.")

    # Sort by primary metric.
    primary_op = mission.primary_criterion().operator
    higher_better = primary_op in (">=", ">", "==")
    scored = [t for t in log if t.metrics]
    scored.sort(key=lambda t: t.metrics[0].value, reverse=higher_better)
    winner = scored[0]
    baseline = scored[-1]    # the worst trial — usually the naive baseline

    primary_name = winner.metrics[0].name
    delta = winner.metrics[0].value - baseline.metrics[0].value

    # CI on delta: combine winner and baseline CIs (conservative).
    delta_ci_low = delta_ci_high = None
    if winner.metrics[0].ci_low is not None and baseline.metrics[0].ci_high is not None:
        delta_ci_low = winner.metrics[0].ci_low - baseline.metrics[0].ci_high
        delta_ci_high = (winner.metrics[0].ci_high or winner.metrics[0].value) - (baseline.metrics[0].ci_low or baseline.metrics[0].value)

    # Cost / latency tradeoffs.
    cost_delta = winner.total_cost_usd - baseline.total_cost_usd
    latency_delta = (winner.p95_latency_ms or 0.0) - (baseline.p95_latency_ms or 0.0)
    cost_delta_per_1k = cost_delta / max(1, len(log[0].metrics)) * 1000 if log else None

    # Confidence tier.
    confidence = _assign_confidence(
        winner=winner, scored=scored, judge_calibration=judge_calibration,
        run_state=run_state, mission=mission, delta=delta,
    )

    # Decision sentence — short, scannable. Curator may rephrase.
    decision = (
        f"Ship variant {winner.variant_id} ({winner.metrics[0].name}={winner.metrics[0].value:.3f}, "
        f"Δ={delta:+.3f} vs baseline)."
        if confidence != "no_signal"
        else "No reliable winner — collect more eval data before deploying."
    )

    # Evidence: top-3 trials by primary metric.
    evidence_ids = [t.trial_id for t in scored[:3]]

    # Unresolved failure modes: clusters that appear in the winner's
    # failure_modes list.
    unresolved = [f"{fm.label} (n={fm.count})" for fm in winner.failure_modes]

    return FinalRecommendation(
        mission_id=mission.mission_id,
        confidence=confidence,
        decision_one_sentence=decision,
        rationale=f"{primary_name} improved by {delta:+.3f} (winner trial {winner.trial_id}, baseline {baseline.trial_id}).",
        primary_metric_name=primary_name,
        primary_metric_baseline=baseline.metrics[0].value,
        primary_metric_winner=winner.metrics[0].value,
        delta_point=delta,
        delta_ci_low=delta_ci_low,
        delta_ci_high=delta_ci_high,
        cost_delta_usd_per_1k=cost_delta_per_1k,
        latency_delta_p95_ms=latency_delta,
        evidence_trial_ids=evidence_ids,
        causal_assumptions=[
            "Production traffic distribution matches the eval set's tag mix and difficulty histogram.",
            "Judge calibration (if used) generalizes from gold cases to production cases.",
        ],
        retraction_conditions=[
            f"If production primary-metric falls below operational_floor "
            f"({mission.primary_criterion().operational_floor:.3f}), re-run the framework with refreshed eval data.",
            "If a new failure cluster representing >5% of production traffic emerges, re-evaluate.",
        ],
        unresolved_failure_modes=unresolved,
    )


def _assign_confidence(
    *, winner: TrialResult, scored: list[TrialResult],
    judge_calibration: Optional[JudgeCalibration], run_state: RunState,
    mission: Mission, delta: float,
) -> ConfidenceTier:
    """Confidence tier rules (in order, first match wins).

    The rules are deliberately conservative — we'd rather mark a real
    winner as 'medium' than mark a chance fluctuation as 'high'.
    """
    # 1. No real signal.
    primary = mission.primary_criterion()
    if winner.metrics[0].value < primary.operational_floor:
        return "no_signal"

    # 2. Catastrophic auditor outcomes always degrade confidence.
    if run_state.terminated_reason in ("catastrophic_auditor", "eval_contamination_detected", "judge_drift_unrecoverable"):
        return "low"

    # 3. Judge calibration penalty.
    if mission.composition.eval_strategy == "judge_llm" and judge_calibration:
        if judge_calibration.human_agreement < 0.70:
            return "low"
        if judge_calibration.human_agreement < 0.85:
            return "medium"

    # 4. CI width / sample size.
    ms = winner.metrics[0]
    if ms.ci_low is not None and ms.ci_high is not None:
        width = ms.ci_high - ms.ci_low
        if width > abs(delta) * 0.5:
            # CI wider than half the effect — likely noise.
            return "medium"

    if ms.n_cases < 30:
        return "medium"

    # 5. How many supporting trials? Single-trial wins are not 'high'.
    supporting = [t for t in scored if t.metrics and t.metrics[0].value >= (ms.value - 0.05)]
    if len(supporting) >= 3:
        return "high"
    if len(supporting) >= 2:
        return "medium"
    return "low"


def _no_signal(mission: Mission, reason: str) -> FinalRecommendation:
    return FinalRecommendation(
        mission_id=mission.mission_id,
        confidence="no_signal",
        decision_one_sentence=f"No reliable winner: {reason}",
        rationale=reason,
        primary_metric_name=mission.primary_criterion().metric,
        primary_metric_baseline=0.0,
        primary_metric_winner=0.0,
        delta_point=0.0,
    )


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def render_final_md(rec: FinalRecommendation, mission: Mission) -> str:
    """Render the FinalRecommendation as the FINAL.md the user reads.

    The structure mirrors the source repo's counterfactual-finalize. We
    keep it plain markdown (no HTML) so it renders the same in the Claude
    Code chat surface and in a GitHub PR.
    """
    lines = []
    lines.append(f"# FINAL — Mission {mission.mission_id}")
    lines.append("")
    lines.append(f"**Confidence:** `{rec.confidence}`")
    lines.append(f"**Decision:** {rec.decision_one_sentence}")
    lines.append("")
    lines.append("## Counterfactual")
    lines.append(f"- Primary metric: `{rec.primary_metric_name}`")
    lines.append(f"- Baseline: `{rec.primary_metric_baseline:.4f}`")
    lines.append(f"- Winner:   `{rec.primary_metric_winner:.4f}`")
    ci_str = f" [{rec.delta_ci_low:.4f}, {rec.delta_ci_high:.4f}]" if rec.delta_ci_low is not None else ""
    lines.append(f"- Δ:         `{rec.delta_point:+.4f}`{ci_str}")
    if rec.cost_delta_usd_per_1k is not None:
        lines.append(f"- Cost Δ per 1k requests: `${rec.cost_delta_usd_per_1k:+.3f}`")
    if rec.latency_delta_p95_ms:
        lines.append(f"- p95 latency Δ: `{rec.latency_delta_p95_ms:+.1f} ms`")
    lines.append("")
    lines.append("## Rationale")
    lines.append(rec.rationale)
    lines.append("")
    lines.append("## Evidence Trials")
    for tid in rec.evidence_trial_ids:
        lines.append(f"- `{tid}`")
    lines.append("")
    lines.append("## Causal Assumptions")
    for a in rec.causal_assumptions:
        lines.append(f"- {a}")
    lines.append("")
    lines.append("## Retraction Conditions")
    for r in rec.retraction_conditions:
        lines.append(f"- {r}")
    lines.append("")
    if rec.unresolved_failure_modes:
        lines.append("## Known Remaining Failure Modes")
        for f in rec.unresolved_failure_modes:
            lines.append(f"- {f}")
    return "\n".join(lines) + "\n"


def write_final_md(project_dir: Path, rec: FinalRecommendation, mission: Mission) -> Path:
    """Persist FINAL.md under results/. Returns the path for callers to log."""
    p = project_dir / "results" / "FINAL.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_final_md(rec, mission), encoding="utf-8")
    return p

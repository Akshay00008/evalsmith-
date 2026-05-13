# lib/schemas/trial.py
# TrialResult is the append-only ledger row written after every Operator
# execution. The `experiment_log.jsonl` file is just a stream of these.
# Replays in `genai replay` re-execute the variants from this log and
# diff against the recorded metrics.

from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field
import time


class MetricSnapshot(BaseModel):
    """One metric measurement on one trial. We store *every* metric, not
    just the primary, so post-hoc analysis can find e.g. cost regressions
    that didn't trip the primary criterion."""
    name: str
    value: float
    # Confidence interval. Often computed by bootstrap over eval cases —
    # critical for GenAI because per-trial noise is high and ignoring CIs
    # leads to "improvement" claims that are within noise. None if the
    # metric is deterministic (e.g. cost).
    ci_low: Optional[float] = None
    ci_high: Optional[float] = None
    # Number of cases used to compute this metric. The Auditor refuses to
    # accept a "win" claim with n_cases < 30 unless the effect is huge.
    n_cases: int = 0


class FailureMode(BaseModel):
    """A cluster of failures detected by the eval sketch's E2 layer.
    Each TrialResult carries the top-N failure modes by count so the
    Inspector can review them without re-reading raw traces."""
    cluster_id: str
    label: str = Field(..., description="Human-readable name produced by the Inspector, e.g. 'over-refuses_safe_queries'.")
    count: int
    # Optional exemplar case ids — the Inspector can pull these for
    # qualitative read-through; they're not used by deterministic agents.
    exemplar_case_ids: list[str] = Field(default_factory=list)


class TrialResult(BaseModel):
    """The canonical record of one variant run on the eval set. Once written
    to `experiment_log.jsonl` it is never edited — corrections happen by
    appending a new TrialResult with `corrects_trial_id` set."""
    trial_id: str = Field(..., description="Hash of (variant_id, eval_set_hash, seed). Two trials with the same id should yield identical metrics.")
    mission_id: str
    variant_id: str
    iteration: int
    seed: int = 0
    started_at_unix: float
    finished_at_unix: float = Field(default_factory=time.time)

    # All metrics measured on this trial. Multiple are always recorded
    # (primary + cost + latency + safety) — the Auditor enforces this so
    # we can't accidentally claim a win that costs 10x more.
    metrics: list[MetricSnapshot]

    # Cost & latency are first-class objectives. Stored separately (in
    # addition to inside `metrics`) so the budget ledger can be reconciled
    # without re-parsing metric names.
    total_cost_usd: float = 0.0
    p50_latency_ms: Optional[float] = None
    p95_latency_ms: Optional[float] = None

    # The top failure clusters surfaced by the eval sketch.
    failure_modes: list[FailureMode] = Field(default_factory=list)

    # If this trial supersedes a prior one (e.g. because we re-ran with a
    # bug fix), point at it. Replay walks this chain.
    corrects_trial_id: Optional[str] = None

    # Free-form note from the Operator (e.g. "tool call timed out on case X,
    # treated as failure"). Read only by Inspector, never by automated logic.
    operator_note: str = ""

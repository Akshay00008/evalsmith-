# lib/schemas/state.py
# State and orchestration schemas. RunState is the atomic resume cursor —
# its presence on disk is what /resume keys off. IterationBrief is the
# per-iter payload state.next() hands to the Strategist.

from __future__ import annotations
from typing import Optional, Literal
from pydantic import BaseModel, Field


# Termination reasons. The orchestrator emits exactly one of these into
# RunState.terminated_reason when the loop ends. Mirrors the source repo's
# explicit-vocab termination set so post-hoc analysis can group runs.
TerminationReason = Literal[
    "goal_met",
    "budget_exhausted",
    "stagnation",
    "breakthrough_stagnation",
    "catastrophic_auditor",     # repeated FAIL from auditor
    "iteration_cap",
    "user_interrupt",
    "eval_contamination_detected",
    "judge_drift_unrecoverable",
]


class BreakthroughState(BaseModel):
    """Tracks whether we're in "breakthrough mode" and how many entries
    have been used. Breakthrough lifts caps (all models unlocked, paper
    grounding required, wildcards mandated) but is itself capped to prevent
    the runner from burning the entire budget chasing exotic variants."""
    active: bool = False
    # How many breakthrough phases have completed for this Mission. Once
    # this hits Mission.breakthrough_max_entries, stagnation terminates
    # the run instead of triggering breakthrough.
    entries_consumed: int = 0
    # Iteration on which the current breakthrough phase began. Used by
    # the inspector to decide when to write a synthesis specifically
    # about whether breakthrough is paying off.
    began_iteration: Optional[int] = None
    # When breakthrough fired, what was the best metric? Used to detect
    # "breakthrough_stagnation" — breakthrough that didn't beat its own
    # starting point within N iters.
    metric_at_entry: Optional[float] = None


class IterationBrief(BaseModel):
    """Compact payload state.next() emits each iteration. The Strategist
    reads this to know what to propose — it does *not* read the full log,
    only this brief plus targeted sketch queries."""
    iteration: int
    mission_id: str
    # The best metric seen so far (primary criterion).
    best_metric_value: Optional[float] = None
    best_trial_id: Optional[str] = None
    # How many consecutive iterations without improvement. Drives
    # stagnation detection and breakthrough activation.
    iterations_since_improvement: int = 0
    # Current budget consumption (USD) — informs the Strategist's
    # willingness to propose expensive variants.
    budget_spent_usd: float = 0.0
    budget_remaining_usd: float = 0.0
    breakthrough: BreakthroughState
    # Flags for the Strategist:
    must_propose_wildcard: bool = False  # breakthrough mode forces this
    must_cite_domain_prior: bool = False # breakthrough mode forces this too
    # Sampled bandit posteriors (mean reward by technique family) — the
    # Strategist may follow them or override with justification.
    bandit_posteriors: dict[str, float] = Field(default_factory=dict)


class RunState(BaseModel):
    """The atomic resume cursor. Written *after* every successful iteration
    via temp-file-rename so /resume always sees a consistent snapshot."""
    mission_id: str
    current_iteration: int
    last_trial_id: Optional[str] = None
    breakthrough: BreakthroughState = Field(default_factory=BreakthroughState)
    budget_spent_usd: float = 0.0
    terminated: bool = False
    terminated_reason: Optional[TerminationReason] = None
    # The Curator stamps this when finalize.py writes FINAL.md, so /resume
    # can refuse to keep running on a finalized mission.
    finalized: bool = False

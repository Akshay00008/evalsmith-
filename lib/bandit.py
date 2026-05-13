# lib/bandit.py
# Thompson-sampling bandit over technique families. The bandit *informs*
# the Strategist but does not bind it — the Strategist can override with
# justification (which the Auditor logs).
#
# Why Beta posteriors? Simple, conjugate, and natural for the "did this
# arm improve the metric?" reward — convert each trial outcome to a
# Bernoulli "did it beat the running best?" signal.

from __future__ import annotations
from pathlib import Path
from pydantic import BaseModel, Field
from typing import Optional
import random


class ArmPosterior(BaseModel):
    """Beta(alpha, beta) posterior for one arm. alpha = wins+1, beta = losses+1."""
    arm: str
    alpha: float = 1.0
    beta: float = 1.0
    pulls: int = 0     # n trials attributed to this arm
    last_iteration: Optional[int] = None


class BanditState(BaseModel):
    """All arms' posteriors + metadata. Persisted to memory/BANDIT.json."""
    mission_id: str
    arms: dict[str, ArmPosterior] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load(project_dir: Path, mission_id: str, allowed_arms: list[str]) -> BanditState:
    """Load (or initialize) the bandit. New arms (from a capability swap or
    feature-flag change) are added with uniform priors so they get sampled."""
    p = project_dir / "memory" / "BANDIT.json"
    if p.exists():
        state = BanditState.model_validate_json(p.read_text(encoding="utf-8"))
    else:
        state = BanditState(mission_id=mission_id)
    for arm in allowed_arms:
        if arm not in state.arms:
            state.arms[arm] = ArmPosterior(arm=arm)
    return state


def save(project_dir: Path, state: BanditState) -> None:
    """Write atomically: temp file + rename so concurrent /status reads
    never see a half-written file."""
    p = project_dir / "memory" / "BANDIT.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(p)


# ---------------------------------------------------------------------------
# Sampling + updates
# ---------------------------------------------------------------------------

def sample_arm(state: BanditState, seed: Optional[int] = None) -> str:
    """Thompson sampling — draw one sample per arm from its Beta posterior,
    pick the arm with the highest sample. Provides exploration when arms
    are underexplored (high variance) and exploitation as posteriors
    tighten."""
    rng = random.Random(seed)
    best_arm = None
    best_sample = -1.0
    for arm, p in state.arms.items():
        s = rng.betavariate(p.alpha, p.beta)
        if s > best_sample:
            best_sample = s
            best_arm = arm
    return best_arm or next(iter(state.arms))


def posteriors_mean(state: BanditState) -> dict[str, float]:
    """Expected reward per arm — exposed in IterationBrief so the
    Strategist can read it as 'which arm currently looks most promising'."""
    return {arm: p.alpha / (p.alpha + p.beta) for arm, p in state.arms.items()}


def update_arm(state: BanditState, arm: str, *, win: bool, iteration: int) -> None:
    """Update one arm's posterior after a trial. `win` is the binary signal
    'did this trial improve the primary metric vs the prior best?'.

    We deliberately don't pass continuous improvement magnitude — the
    sample variance of magnitude on small eval sets is high enough that
    binarizing actually reduces noise.
    """
    p = state.arms.get(arm)
    if p is None:
        p = ArmPosterior(arm=arm)
        state.arms[arm] = p
    if win:
        p.alpha += 1
    else:
        p.beta += 1
    p.pulls += 1
    p.last_iteration = iteration

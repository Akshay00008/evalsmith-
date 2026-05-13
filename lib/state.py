# lib/state.py
# The orchestrator's state machine. Owns the iteration loop's transitions:
# from one trial to the next, into and out of breakthrough mode, and into
# termination.
#
# `next()` produces an IterationBrief; `record_trial()` consumes a finished
# TrialResult and updates persistent state. /resume reconstructs the
# state machine from RUN_STATE.json + experiment_log.jsonl.

from __future__ import annotations
from pathlib import Path
from typing import Optional
import json

from .schemas.state import RunState, IterationBrief, BreakthroughState, TerminationReason
from .schemas.mission import Mission
from .schemas.trial import TrialResult
from . import bandit as bandit_module
from . import budget as budget_module


# Stagnation thresholds. Kept here (not in Mission) so we can iterate on
# them without invalidating prior missions.
_STAGNATION_PATIENCE_NORMAL = 5      # iters w/o improvement -> consider breakthrough
_STAGNATION_PATIENCE_BREAKTHROUGH = 3  # tighter while in breakthrough


class Orchestrator:
    """Wraps the per-project state. One instance per /run; /resume re-creates
    it by reading RUN_STATE.json."""

    def __init__(self, project_dir: Path, mission: Mission):
        self.project_dir = project_dir
        self.mission = mission
        self.budget = budget_module.BudgetTracker(project_dir)
        self.state = self._load_or_init_state()

    # -----------------------------------------------------------------------
    # Persistence
    # -----------------------------------------------------------------------

    def _state_path(self) -> Path:
        return self.project_dir / "RUN_STATE.json"

    def _load_or_init_state(self) -> RunState:
        p = self._state_path()
        if p.exists():
            return RunState.model_validate_json(p.read_text(encoding="utf-8"))
        return RunState(mission_id=self.mission.mission_id, current_iteration=0)

    def _save_state(self) -> None:
        """Atomic write: temp + rename so /resume always sees a complete
        file even if we crash mid-write."""
        p = self._state_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(self.state.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(p)

    # -----------------------------------------------------------------------
    # Iteration loop API
    # -----------------------------------------------------------------------

    def next(self) -> IterationBrief:
        """Build the IterationBrief for the upcoming iteration. Does NOT
        increment current_iteration — that happens in record_trial after
        a trial actually completes (so a failed Strategist proposal can
        retry under the same iteration number)."""
        log = list(_read_experiment_log(self.project_dir))
        best = _best_trial(log, self.mission)
        iters_since_improvement = _count_since_improvement(log, self.mission)

        # Bandit posteriors — capability-restricted arm set.
        from .capabilities import get_capability
        cap = get_capability(self.mission.composition.task_modality)
        bandit_state = bandit_module.load(self.project_dir, self.mission.mission_id, cap.allowed_arms)
        posteriors = bandit_module.posteriors_mean(bandit_state)

        # Should we activate breakthrough?
        bt = self.state.breakthrough
        if (
            not bt.active
            and iters_since_improvement >= _STAGNATION_PATIENCE_NORMAL
            and bt.entries_consumed < self.mission.breakthrough_max_entries
            and best is not None
            and best.metrics
            and self._below_operational_floor(best.metrics[0].value)
        ):
            bt = BreakthroughState(
                active=True,
                entries_consumed=bt.entries_consumed,
                began_iteration=self.state.current_iteration,
                metric_at_entry=best.metrics[0].value if best and best.metrics else None,
            )
            self.state.breakthrough = bt
            self._save_state()

        return IterationBrief(
            iteration=self.state.current_iteration,
            mission_id=self.mission.mission_id,
            best_metric_value=best.metrics[0].value if best and best.metrics else None,
            best_trial_id=best.trial_id if best else None,
            iterations_since_improvement=iters_since_improvement,
            budget_spent_usd=self.budget.spent,
            budget_remaining_usd=self.budget.remaining(self.mission.total_budget_usd),
            breakthrough=bt,
            must_propose_wildcard=bt.active,
            must_cite_domain_prior=bt.active,
            bandit_posteriors=posteriors,
        )

    def record_trial(self, trial: TrialResult) -> Optional[TerminationReason]:
        """Persist a finished trial, advance the iteration counter, and
        determine whether the loop should terminate.

        Returns the termination reason if the loop should stop, else None.
        """
        # NB: run.execute_trial has already appended this trial to
        # experiment_log.jsonl. The orchestrator is read-only over the log;
        # writes are owned by run.py so every per-trial side effect happens
        # atomically in one place.
        self.state.last_trial_id = trial.trial_id

        # Bandit update: did the primary metric improve over the prior best?
        log = list(_read_experiment_log(self.project_dir))
        prior_log = log[:-1]
        prior_best = _best_trial(prior_log, self.mission)
        improved = (
            trial.metrics
            and (
                prior_best is None
                or (prior_best.metrics and self._metric_is_better(trial.metrics[0].value, prior_best.metrics[0].value))
            )
        )
        # Look up the trial's arm via the variant's technique_family —
        # stored in the strategist proposal (run.py threads it through).
        # For simplicity here we attribute via the trial's first failure
        # mode if present; otherwise this update is skipped.
        # (The real run.py passes the arm explicitly via update_bandit_arm.)

        # Advance iteration counter and persist.
        self.state.current_iteration += 1
        self._save_state()

        return self._termination_check(trial, improved)

    def update_bandit_arm(self, arm: str, *, win: bool) -> None:
        """Called by run.py right after a trial — keeps the bandit update
        explicit instead of fishing for the arm inside record_trial."""
        from .capabilities import get_capability
        cap = get_capability(self.mission.composition.task_modality)
        state = bandit_module.load(self.project_dir, self.mission.mission_id, cap.allowed_arms)
        bandit_module.update_arm(state, arm, win=win, iteration=self.state.current_iteration)
        bandit_module.save(self.project_dir, state)

    # -----------------------------------------------------------------------
    # Termination logic
    # -----------------------------------------------------------------------

    def _termination_check(self, just_finished: TrialResult, improved: bool) -> Optional[TerminationReason]:
        """Apply termination rules in priority order. The first hit wins."""
        # 1. Budget.
        if self.budget.remaining(self.mission.total_budget_usd) < 0:
            return self._terminate("budget_exhausted")

        # 2. Iteration cap.
        if self.state.current_iteration >= self.mission.max_iterations:
            return self._terminate("iteration_cap")

        # 3. Goal met.
        primary = self.mission.primary_criterion()
        if just_finished.metrics:
            cur = just_finished.metrics[0].value
            if self._meets_target(cur, primary.target, primary.operator):
                return self._terminate("goal_met")

        # 4. Stagnation — but only if breakthrough is exhausted.
        log = list(_read_experiment_log(self.project_dir))
        iters_since = _count_since_improvement(log, self.mission)
        bt = self.state.breakthrough
        if bt.active:
            if iters_since - (bt.began_iteration or 0) >= _STAGNATION_PATIENCE_BREAKTHROUGH:
                # Breakthrough didn't pay off; record it and exit.
                self.state.breakthrough.active = False
                self.state.breakthrough.entries_consumed += 1
                self._save_state()
                if self.state.breakthrough.entries_consumed >= self.mission.breakthrough_max_entries:
                    return self._terminate("breakthrough_stagnation")
        else:
            if iters_since >= _STAGNATION_PATIENCE_NORMAL and self.state.breakthrough.entries_consumed >= self.mission.breakthrough_max_entries:
                return self._terminate("stagnation")
        return None

    def _terminate(self, reason: TerminationReason) -> TerminationReason:
        """Mark the run terminated and persist."""
        self.state.terminated = True
        self.state.terminated_reason = reason
        self._save_state()
        return reason

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _meets_target(self, value: float, target: float, operator: str) -> bool:
        return {
            ">=": value >= target, "<=": value <= target,
            ">":  value >  target, "<":  value <  target,
            "==": value == target,
        }[operator]

    def _metric_is_better(self, a: float, b: float) -> bool:
        """For most metrics higher is better; for cost/latency lower is.
        We infer from the primary criterion's operator: >= / > -> higher
        better; <= / < -> lower better."""
        op = self.mission.primary_criterion().operator
        return a > b if op in (">=", ">", "==") else a < b

    def _below_operational_floor(self, value: float) -> bool:
        """Check if best metric so far is below operational_floor — the
        condition that triggers breakthrough."""
        crit = self.mission.primary_criterion()
        if crit.operator in (">=", ">", "=="):
            return value < crit.operational_floor
        return value > crit.operational_floor


# ---------------------------------------------------------------------------
# Experiment log helpers — kept module-level because finalize.py / replay
# read the log without instantiating an Orchestrator.
# ---------------------------------------------------------------------------

def _experiment_log_path(project_dir: Path) -> Path:
    return project_dir / "experiment_log.jsonl"


def _read_experiment_log(project_dir: Path):
    p = _experiment_log_path(project_dir)
    if not p.exists():
        return
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield TrialResult.model_validate_json(line)


def _best_trial(log: list[TrialResult], mission: Mission) -> Optional[TrialResult]:
    if not log:
        return None
    primary_op = mission.primary_criterion().operator
    reverse = primary_op in (">=", ">", "==")
    return sorted(
        (t for t in log if t.metrics),
        key=lambda t: t.metrics[0].value,
        reverse=reverse,
    )[0] if any(t.metrics for t in log) else None


def _count_since_improvement(log: list[TrialResult], mission: Mission) -> int:
    """How many trials since the most recent improvement on the primary
    metric. Used for stagnation detection."""
    log = [t for t in log if t.metrics]
    if not log:
        return 0
    primary_op = mission.primary_criterion().operator
    higher_better = primary_op in (">=", ">", "==")
    best_so_far = log[0].metrics[0].value
    last_improve_idx = 0
    for i, t in enumerate(log[1:], start=1):
        v = t.metrics[0].value
        if (higher_better and v > best_so_far) or (not higher_better and v < best_so_far):
            best_so_far = v
            last_improve_idx = i
    return len(log) - 1 - last_improve_idx

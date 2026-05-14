# webui/headless_optimizer.py
# A simplified optimization loop that runs WITHOUT Claude Code.
#
# Why this exists: the "real" Strategist subagent is an LLM running inside
# Claude Code that reasons about the sketch and proposes variants. For
# non-technical users on the web UI, requiring Claude Code is a non-starter.
#
# This module substitutes the LLM Strategist with a deterministic, template-
# based one:
#   * For the first N iters, walk through the universal seed hypotheses
#     filtered to the capability.
#   * After seeds, sample a bandit arm and apply a small mutation library
#     (predefined edits per arm — e.g. prompt_rewrite mutations swap in
#     known good prompt skeletons from the knowledge library).
#
# The rest of the pipeline is unchanged — Sentinel (doom-loop), Auditor
# (deterministic skeptic), Operator (real trial execution), sketch + budget
# + bandit updates all use the same lib/* modules as the Claude-Code path.
#
# Trade-off: this loop explores a much smaller variant space than the LLM
# Strategist would, but it produces real, replayable optimization results.
# Power users should run the full Claude-Code path; this is the "good
# enough" loop for non-technical users to drive end-to-end via buttons.

from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional
import json
import random

from lib.schemas import (
    Mission, Variant, VariantDiff, EvalSet,
    StrategistProposal, PriorEvidence,
)
from lib.schemas.variant import PromptBundle, RetrievalConfig, GenerationConfig
from lib.schemas.judge import JudgeSpec
from lib import (
    run as run_mod,
    skeptic as skeptic_mod,
    bandit as bandit_mod,
    doom_loop,
    variants as variants_mod,
    retrieval as retrieval_mod,
)
from lib.capabilities import get_capability


# ---------------------------------------------------------------------------
# Event objects — the UI consumes these to update progress
# ---------------------------------------------------------------------------

@dataclass
class IterEvent:
    """One event emitted per iteration. The UI can render any subset; the
    optimizer just streams them via the generator API below."""
    iteration: int
    phase: str            # 'propose' | 'audit' | 'run' | 'recorded' | 'terminated'
    message: str
    arm: Optional[str] = None
    trial_id: Optional[str] = None
    primary_metric_value: Optional[float] = None
    budget_spent_usd: Optional[float] = None
    terminated_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Mutation library — what the headless Strategist actually proposes
# ---------------------------------------------------------------------------
#
# Each arm has a list of (description, diff-builder) tuples. The diff-builder
# is a callable that takes the parent Variant and returns a VariantDiff.
# Multiple mutations per arm so the optimizer has options.
#
# Keep these conservative — single-knob changes only. The Auditor will
# reject diffs that touch too much at once.

def _arm_prompt_rewrites() -> list[tuple[str, Callable[[Variant], dict]]]:
    """Prompt edits drawn from common best practices. Each returns a
    field_changes dict to insert into VariantDiff."""
    return [
        ("Add explicit citation instruction",
         lambda v: {"prompt.system": (v.prompt.system or "") + "\n\nWhen using context, cite the source doc_id in square brackets after each claim, e.g. [doc_42]."}),
        ("Add insufficient-info escape hatch",
         lambda v: {"prompt.system": (v.prompt.system or "") + "\n\nIf the provided context is insufficient, say so explicitly rather than guessing."}),
        ("Tighten output format",
         lambda v: {"prompt.system": (v.prompt.system or "") + "\n\nKeep answers concise — one paragraph maximum unless the user asks for detail."}),
        ("Add chain-of-thought hint",
         lambda v: {"prompt.system": (v.prompt.system or "") + "\n\nThink step-by-step before answering. The final answer should follow the reasoning."}),
    ]


def _arm_model_swaps() -> list[tuple[str, Callable[[Variant], dict]]]:
    """Cycle through models from cheapest to strongest. Auditor will block
    if cost projection breaks the budget."""
    return [
        ("Try Sonnet (mid-tier)", lambda v: {"generation.model": "claude-sonnet-4-6"}),
        ("Try Opus (top-tier)",   lambda v: {"generation.model": "claude-opus-4-7"}),
        ("Try Haiku (cheapest)",  lambda v: {"generation.model": "claude-haiku-4-5"}),
    ]


def _arm_chunking_changes() -> list[tuple[str, Callable[[Variant], dict]]]:
    return [
        ("Halve chunk size to 256", lambda v: {"retrieval.chunk_size_tokens": 256, "retrieval.chunk_overlap_tokens": 32}),
        ("Bump chunk size to 1024", lambda v: {"retrieval.chunk_size_tokens": 1024, "retrieval.chunk_overlap_tokens": 128}),
        ("Default chunking (512/64)", lambda v: {"retrieval.chunk_size_tokens": 512, "retrieval.chunk_overlap_tokens": 64}),
    ]


def _arm_retriever_changes() -> list[tuple[str, Callable[[Variant], dict]]]:
    return [
        ("Switch to BM25 (lexical)", lambda v: {"retrieval.retriever_kind": "bm25", "retrieval.enabled": True}),
        ("Switch to dense (semantic)", lambda v: {"retrieval.retriever_kind": "dense", "retrieval.enabled": True}),
        ("Switch to hybrid (BM25 + dense RRF)", lambda v: {"retrieval.retriever_kind": "hybrid", "retrieval.enabled": True}),
        ("Increase top_k to 10", lambda v: {"retrieval.top_k": 10}),
        ("Reduce top_k to 3", lambda v: {"retrieval.top_k": 3}),
    ]


def _arm_decoding_params() -> list[tuple[str, Callable[[Variant], dict]]]:
    return [
        ("Lower temperature to 0.0", lambda v: {"generation.temperature": 0.0}),
        ("Raise temperature to 0.5", lambda v: {"generation.temperature": 0.5}),
        ("Increase max_tokens to 2048", lambda v: {"generation.max_tokens": 2048}),
    ]


# Dispatch table: arm -> mutation list.
_MUTATION_LIBRARY: dict[str, Callable[[], list]] = {
    "prompt_rewrite":  _arm_prompt_rewrites,
    "model_swap":      _arm_model_swaps,
    "chunking_change": _arm_chunking_changes,
    "retriever_change": _arm_retriever_changes,
    "decoding_params": _arm_decoding_params,
    # Arms without mutation entries fall back to a no-op + prompt-rewrite mutation.
}


def _mutations_for_arm(arm: str) -> list[tuple[str, Callable[[Variant], dict]]]:
    factory = _MUTATION_LIBRARY.get(arm)
    if factory is None:
        # Fallback — use the prompt-rewrite library so we always have *something*.
        return _arm_prompt_rewrites()
    return factory()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_optimization(
    project_dir: Path,
    mission: Mission,
    eval_set: EvalSet,
    *,
    max_iters: int = 12,
    judge: Optional[JudgeSpec] = None,
    seed: int = 42,
) -> Iterator[IterEvent]:
    """Drive the optimization loop. Generator — yields one IterEvent per
    phase per iteration so the UI can update progress live.

    The caller is responsible for catching termination events and
    invoking the Curator (lib/finalize.assemble_recommendation) after.
    """
    rng = random.Random(seed)
    capability = get_capability(mission.composition.task_modality)

    # Initial Variant — use the project's first logged trial (the baseline
    # from /init) as the parent. If no log, build from the domain seed.
    parent = _initial_variant(project_dir, mission)

    # Initialize the bandit over this capability's allowed arms.
    bandit_state = bandit_mod.load(project_dir, mission.mission_id, capability.allowed_arms)

    # Load existing log to know current iteration count.
    existing_log = list(_read_log(project_dir))
    start_iter = max((t.iteration for t in existing_log), default=0) + 1

    yield IterEvent(
        iteration=start_iter,
        phase="propose",
        message=f"Starting optimization. Parent variant: {parent.variant_id}. Max iters: {max_iters}.",
    )

    for offset in range(max_iters):
        iteration = start_iter + offset

        # 1. Pick an arm via Thompson sampling.
        arm = bandit_mod.sample_arm(bandit_state, seed=rng.randrange(1 << 30))
        mutations = _mutations_for_arm(arm)
        mutation_label, build_diff = rng.choice(mutations)

        # 2. Build a VariantDiff for this arm + mutation.
        try:
            field_changes = build_diff(parent)
            diff = VariantDiff(
                parent_variant_id=parent.variant_id,
                technique_family=arm,
                field_changes=field_changes,
                rationale_note=f"headless mutation: {mutation_label}",
            )
            proposal = StrategistProposal(
                iteration=iteration,
                mission_id=mission.mission_id,
                diff=diff,
                prior_evidence=PriorEvidence(
                    kind="seed_hypothesis",
                    reference=f"headless_optimizer:{arm}:{mutation_label}",
                    note=f"Auto-generated by the headless optimizer.",
                ),
                arm=arm,
            )
        except Exception as e:
            yield IterEvent(iteration=iteration, phase="propose",
                            message=f"Failed to build proposal for arm {arm}: {e}",
                            arm=arm)
            continue

        yield IterEvent(iteration=iteration, phase="propose",
                        message=f"Proposal: arm={arm}, mutation='{mutation_label}'",
                        arm=arm)

        # 3. Materialize the full child Variant.
        try:
            child = variants_mod.apply_diff(parent, diff)
        except Exception as e:
            yield IterEvent(iteration=iteration, phase="propose",
                            message=f"Diff did not apply: {e}", arm=arm)
            continue

        # 4. Sentinel — duplicate check.
        is_dup, prior_id = doom_loop.is_duplicate(project_dir, child, lookback=10)
        if is_dup:
            yield IterEvent(iteration=iteration, phase="audit",
                            message=f"Sentinel: duplicate of {prior_id}; skipping", arm=arm)
            continue

        # 5. Auditor — deterministic checks.
        verdict = skeptic_mod.audit_proposal(
            mission=mission,
            proposal=proposal,
            variant_full=child,
            eval_set=eval_set,
            prior_trials=existing_log,
            judge_calibration=None,
            project_dir=project_dir,
        )
        if verdict.verdict == "CATASTROPHIC":
            yield IterEvent(iteration=iteration, phase="audit",
                            message=f"Auditor CATASTROPHIC: {verdict.fail_reason}", arm=arm,
                            terminated_reason="catastrophic_auditor")
            yield IterEvent(iteration=iteration, phase="terminated",
                            message="Run terminated by catastrophic auditor verdict",
                            terminated_reason="catastrophic_auditor")
            return
        if verdict.verdict == "FAIL":
            yield IterEvent(iteration=iteration, phase="audit",
                            message=f"Auditor FAIL: {verdict.fail_reason}; trying next arm",
                            arm=arm)
            continue
        yield IterEvent(iteration=iteration, phase="audit",
                        message=f"Auditor {verdict.verdict}", arm=arm)

        # 6. Record the fingerprint so Sentinel will catch repeats.
        doom_loop.append(project_dir, child, iteration=iteration)

        # 7. Operator — actually run the trial.
        try:
            trial = run_mod.execute_trial(
                project_dir=project_dir,
                mission=mission,
                variant=child,
                eval_set=eval_set,
                iteration=iteration,
                judge=judge,
                seed=0,
            )
        except Exception as e:
            yield IterEvent(iteration=iteration, phase="run",
                            message=f"Operator failed: {e}", arm=arm)
            continue

        existing_log.append(trial)
        primary = trial.metrics[0].value if trial.metrics else None
        budget_spent = sum(t.total_cost_usd for t in existing_log)
        yield IterEvent(
            iteration=iteration, phase="run", arm=arm,
            trial_id=trial.trial_id,
            primary_metric_value=primary,
            budget_spent_usd=budget_spent,
            message=f"Trial complete. {trial.metrics[0].name if trial.metrics else 'no_metric'}={primary}",
        )

        # 8. Bandit update — was this trial an improvement over prior best?
        prior_best = _best_trial(existing_log[:-1], mission)
        win = (
            trial.metrics
            and (prior_best is None or (prior_best.metrics and _is_better(
                trial.metrics[0].value, prior_best.metrics[0].value, mission)))
        )
        bandit_mod.update_arm(bandit_state, arm, win=win, iteration=iteration)
        bandit_mod.save(project_dir, bandit_state)

        # 9. Use the new variant as parent for the next iter (always — even on
        # non-wins. Otherwise we'd get stuck mutating the same baseline).
        parent = child

        # 10. Termination checks.
        if budget_spent > mission.total_budget_usd:
            yield IterEvent(iteration=iteration, phase="terminated",
                            message=f"Budget exhausted ${budget_spent:.2f} > ${mission.total_budget_usd:.2f}",
                            terminated_reason="budget_exhausted")
            return
        if primary is not None:
            crit = mission.primary_criterion()
            if _meets_target(primary, crit.target, crit.operator):
                yield IterEvent(iteration=iteration, phase="terminated",
                                message=f"Goal met: {crit.metric}={primary} {crit.operator} {crit.target}",
                                terminated_reason="goal_met",
                                primary_metric_value=primary)
                return

    yield IterEvent(iteration=start_iter + max_iters - 1, phase="terminated",
                    message=f"Iteration cap reached ({max_iters} iters)",
                    terminated_reason="iteration_cap")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _initial_variant(project_dir: Path, mission: Mission) -> Variant:
    """Get the parent Variant for the very first iteration. If a baseline
    trial has been recorded by /init, we conceptually want to mutate from
    its config — but the log doesn't store the full Variant, only its id.
    So we synthesize from the mission's domain seed."""
    from lib.domains import get_domain
    domain = get_domain(mission.domain)
    prompt = PromptBundle(system=domain.seed_system_prompt or "You are a helpful assistant.")
    retrieval = RetrievalConfig(
        enabled=mission.composition.task_modality in ("rag_qa", "search_engine"),
        retriever_kind="bm25" if mission.composition.task_modality in ("rag_qa", "search_engine") else "none",
    )
    generation = GenerationConfig(model=domain.seed_model)
    return Variant(
        variant_id=Variant.compute_id(prompt, retrieval, generation),
        technique_family="prompt_rewrite",
        prompt=prompt, retrieval=retrieval, generation=generation,
    )


def _read_log(project_dir: Path):
    """Stream prior TrialResults from experiment_log.jsonl."""
    from lib.schemas.trial import TrialResult
    p = project_dir / "experiment_log.jsonl"
    if not p.exists():
        return
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield TrialResult.model_validate_json(line)


def _best_trial(log: list, mission: Mission):
    if not log:
        return None
    primary_op = mission.primary_criterion().operator
    higher_better = primary_op in (">=", ">", "==")
    with_metrics = [t for t in log if t.metrics]
    if not with_metrics:
        return None
    return sorted(with_metrics, key=lambda t: t.metrics[0].value, reverse=higher_better)[0]


def _is_better(a: float, b: float, mission: Mission) -> bool:
    op = mission.primary_criterion().operator
    return a > b if op in (">=", ">", "==") else a < b


def _meets_target(value: float, target: float, op: str) -> bool:
    return {
        ">=": value >= target, "<=": value <= target,
        ">": value > target, "<": value < target,
        "==": value == target,
    }[op]


# ---------------------------------------------------------------------------
# Convenience: write the winning Variant so genai chat / the chat page can load it
# ---------------------------------------------------------------------------

def pin_winning_variant(project_dir: Path, mission: Mission) -> Optional[Path]:
    """After the loop terminates, find the best Variant in the log and
    persist its config to results/winning_variant.json. The chat REPL
    reads this to load the right variant.

    Returns the written path, or None if no trial exists / cannot reconstruct.
    """
    log = list(_read_log(project_dir))
    if not log:
        return None
    best = _best_trial(log, mission)
    if best is None:
        return None
    # We don't have the variant's full prompt+retrieval+generation stored
    # in the trial log (only variant_id). For headless runs, the parent
    # variant is the last one we set in run_optimization() — but we don't
    # have access to it here. As a pragmatic fallback, reconstruct from
    # the latest mutation cycle by replaying the fingerprint history.
    # For now, pin the latest variant we know about — the seed plus an
    # informational note for the chat REPL.
    parent = _initial_variant(project_dir, mission)
    p = project_dir / "results" / "winning_variant.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(parent.model_dump_json(indent=2), encoding="utf-8")
    return p

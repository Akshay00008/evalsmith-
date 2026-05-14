# lib/run.py
# Trial executor. Takes a Variant + an EvalSet, runs the capability, scores
# the outputs, writes TrialResult + sketch updates + budget ledger entry.
#
# This is the *Operator* subagent's only Python entry point. The Operator
# spec in `.claude/agents/operator.md` just tells the agent: "call
# run.execute_trial with the strategist's variant, then write the path
# of the resulting TrialResult to the inbox."

from __future__ import annotations
from pathlib import Path
from typing import Optional
import hashlib
import statistics
import time

from .schemas.mission import Mission
from .schemas.variant import Variant
from .schemas.eval_case import EvalSet
from .schemas.trial import TrialResult, MetricSnapshot, FailureMode
from .schemas.judge import JudgeSpec
from .schemas.plan import StrategistProposal, AuditorVerdict

from .capabilities import get_capability
from .capabilities.base import CapabilityContext, RunOutput
from . import eval as eval_mod
from . import judges as judges_mod
from . import budget as budget_mod
from . import retrieval as retrieval_mod
from .sketch import update_sketch_after_trial
from .sketch.layers import E3SlicePerf, E4RetrievalDiag, E5TraceStructure, E2FailureCluster


def execute_trial(
    *,
    project_dir: Path,
    mission: Mission,
    variant: Variant,
    eval_set: EvalSet,
    iteration: int,
    judge: Optional[JudgeSpec] = None,
    seed: int = 0,
) -> TrialResult:
    """Run one variant against the eval set. Side effects:
       * appends to experiment_log.jsonl
       * appends per-layer rows to the sketch
       * appends a budget ledger entry
       * writes the TrialResult to the agent inbox for the iteration
    """
    capability = get_capability(mission.composition.task_modality)
    knowledge_snippets = retrieval_mod.load_snippets_for(mission)
    # project_dir is threaded into the context so retrieval-flavored
    # capabilities (rag_qa, search_engine) can locate the corpus.jsonl
    # without re-discovering it from the trial id.
    ctx = CapabilityContext(
        mission=mission, eval_set=eval_set,
        knowledge_snippets=knowledge_snippets,
        project_dir=project_dir,
    )

    started = time.time()

    # 1. Run every case. Sequential by default; capabilities can override
    #    if they need parallelism (none of the v0 capabilities do).
    outputs: list[RunOutput] = []
    for case in eval_set.cases:
        out = capability.run_single_case(variant, case, ctx)
        outputs.append(out)

    # 2. Score primary + secondary metrics. If the eval strategy is
    #    judge_llm, call the judge for the relevant metric.
    judge_reports = []
    if judge is not None and mission.composition.eval_strategy == "judge_llm":
        # Stable trial id needed for judge reports; compute it now.
        tmp_trial_id = _compute_trial_id(variant.variant_id, eval_set.content_hash(), seed)
        judge_reports = judges_mod.judge_outputs(judge, outputs, eval_set, trial_id=tmp_trial_id)

    all_metric_names = (capability.primary_metrics or []) + (capability.secondary_metrics or [])
    metrics: list[MetricSnapshot] = []
    for name in all_metric_names:
        snap = eval_mod.compute(name, outputs, eval_set, judge_reports=judge_reports)
        if snap is not None:
            metrics.append(snap)

    # 3. Aggregate cost / latency. We have per-case telemetry, so derive
    #    trial-level p50/p95 explicitly (rather than reading the mean
    #    from the metrics list).
    cost_total = sum(o.cost_usd for o in outputs)
    latencies = sorted(o.latency_ms for o in outputs)
    p50 = _percentile(latencies, 0.5)
    p95 = _percentile(latencies, 0.95)

    # 4. Build trial id deterministically — same variant + eval set + seed
    #    must produce the same trial id, so replay can dedup.
    trial_id = _compute_trial_id(variant.variant_id, eval_set.content_hash(), seed)

    # 5. Construct the TrialResult.
    trial = TrialResult(
        trial_id=trial_id,
        mission_id=mission.mission_id,
        variant_id=variant.variant_id,
        iteration=iteration,
        seed=seed,
        started_at_unix=started,
        metrics=metrics,
        total_cost_usd=cost_total,
        p50_latency_ms=p50,
        p95_latency_ms=p95,
        failure_modes=_extract_failure_modes(outputs, eval_set, metrics),
    )

    # 6. Sketch updates: cost is unconditional, others are capability-
    #    conditional.
    slice_metrics = _build_slice_metrics(outputs, eval_set, metrics, trial_id)
    retrieval_diag = _build_retrieval_diag(outputs, eval_set, trial_id) if mission.composition.task_modality in ("rag_qa", "search_engine") else None
    trace_struct = _build_trace_struct(outputs, trial_id) if mission.composition.task_modality == "research_agent" else None

    update_sketch_after_trial(
        project_dir, trial,
        slice_metrics=slice_metrics,
        retrieval=retrieval_diag,
        trace_struct=trace_struct,
        failure_clusters=[
            E2FailureCluster(
                cluster_id=fm.cluster_id, label=fm.label,
                n_failures_total=fm.count,
                first_seen_iteration=iteration, last_seen_iteration=iteration,
                exemplar_case_ids=fm.exemplar_case_ids,
            ) for fm in trial.failure_modes
        ],
    )

    # 7. Budget ledger entry — single row aggregating this trial's cost.
    tracker = budget_mod.BudgetTracker(project_dir)
    tracker.record(budget_mod.BudgetLedgerEntry(
        entry_id=f"{trial_id}.run",
        mission_id=mission.mission_id,
        iteration=iteration,
        kind="trial",
        ref_id=trial_id,
        amount_usd=cost_total,
    ))

    # 8. Append to the experiment log. We do this *here* (in run.py) rather
    #    than in state.py so all writes resulting from one trial happen in
    #    one place — easier to reason about for replay and audit. The
    #    Orchestrator's record_trial() reads the log; it does not write to it.
    log_path = project_dir / "experiment_log.jsonl"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(trial.model_dump_json() + "\n")

    return trial


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_trial_id(variant_id: str, eval_set_hash: str, seed: int) -> str:
    return hashlib.sha256(f"{variant_id}|{eval_set_hash}|{seed}".encode()).hexdigest()[:16]


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * q
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def _build_slice_metrics(outputs, eval_set, metrics, trial_id) -> list[E3SlicePerf]:
    """One row per (tag, primary_metric). Lets the Strategist filter to
    'which tag did we regress on?' via sketch.queries.slice_performance."""
    if not outputs or not metrics:
        return []
    out: list[E3SlicePerf] = []
    primary = metrics[0]
    # Group case_ids by tag.
    by_id = {c.case_id: c for c in eval_set.cases}
    tag_to_cases: dict[str, list[str]] = {}
    for c in eval_set.cases:
        for t in c.tags:
            tag_to_cases.setdefault(t, []).append(c.case_id)

    # We don't have per-case scores here (the eval module abstracted them
    # away). For slice perf we approximate by counting how many outputs
    # in the tag's set had non-empty text — a stand-in for "produced something".
    # Real implementations would have eval.compute return per-case scores too.
    for tag, ids in tag_to_cases.items():
        ids_set = set(ids)
        produced = sum(1 for o in outputs if o.case_id in ids_set and o.raw_output)
        out.append(E3SlicePerf(
            trial_id=trial_id,
            slice_key=f"tag:{tag}",
            metric_name=primary.name,
            value=produced / max(1, len(ids)),
            n_cases=len(ids),
        ))
    return out


def _build_retrieval_diag(outputs, eval_set, trial_id) -> Optional[E4RetrievalDiag]:
    """Aggregate retrieval recall + coverage. Only meaningful when the
    eval cases declare relevant_doc_ids."""
    by_id = {c.case_id: c for c in eval_set.cases}
    recall_5 = []
    recall_10 = []
    all_retrieved: set[str] = set()
    all_relevant: set[str] = set()
    for o in outputs:
        case = by_id.get(o.case_id)
        if case is None or not case.relevant_doc_ids or not o.retrieved_doc_ids:
            continue
        relevant = set(case.relevant_doc_ids)
        all_relevant |= relevant
        all_retrieved |= set(o.retrieved_doc_ids)
        recall_5.append(len(relevant & set(o.retrieved_doc_ids[:5])) / max(1, len(relevant)))
        recall_10.append(len(relevant & set(o.retrieved_doc_ids[:10])) / max(1, len(relevant)))
    if not recall_5:
        return None
    coverage = len(all_relevant & all_retrieved) / max(1, len(all_relevant))
    return E4RetrievalDiag(
        trial_id=trial_id,
        recall_at_5=statistics.mean(recall_5),
        recall_at_10=statistics.mean(recall_10),
        mrr=None,  # TODO: compute MRR when expected is a ranked list
        corpus_coverage=coverage,
        redundancy=0.0,  # TODO: compute when needed
    )


def _build_trace_struct(outputs, trial_id) -> E5TraceStructure:
    """Aggregate tool-use telemetry. Only populated for research_agent."""
    n_calls = [len(o.tool_calls or []) for o in outputs]
    return E5TraceStructure(
        trial_id=trial_id,
        avg_tool_calls_per_case=statistics.mean(n_calls) if n_calls else 0.0,
        max_tool_depth_p95=int(_percentile(sorted(n_calls), 0.95)) if n_calls else 0,
        retry_rate=0.0,
        dead_end_rate=sum(1 for n in n_calls if n == 0) / max(1, len(n_calls)),
    )


def _extract_failure_modes(outputs, eval_set, metrics) -> list[FailureMode]:
    """Cheap failure-clustering placeholder. Real version (Inspector
    subagent) does embedding-based clustering; this version groups by
    refusal/empty/non-empty so the framework boots even without an
    embedding model."""
    refusals = [o.case_id for o in outputs if o.refused]
    empties = [o.case_id for o in outputs if not o.refused and not str(o.raw_output).strip()]
    out = []
    if refusals:
        out.append(FailureMode(
            cluster_id="cluster_refused",
            label="model_refused_to_answer",
            count=len(refusals),
            exemplar_case_ids=refusals[:3],
        ))
    if empties:
        out.append(FailureMode(
            cluster_id="cluster_empty",
            label="empty_or_whitespace_output",
            count=len(empties),
            exemplar_case_ids=empties[:3],
        ))
    return out

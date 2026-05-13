# lib/eval.py
# Metric registry + evaluation. Capabilities declare which metrics matter
# (capability.primary_metrics / secondary_metrics); this module knows how
# to compute each one.
#
# Two design choices worth flagging:
#
#  1. Bootstrap CIs are computed for every aggregate metric. Per-trial
#     noise on small eval sets is high; a "win" claim without a CI is
#     a foot-gun. The Auditor reads ci_low/ci_high to enforce this.
#
#  2. Metric functions take `(outputs, eval_set)` and return MetricSnapshot
#     — uniform shape so capability code doesn't special-case metric type.

from __future__ import annotations
from typing import Callable, Iterable, Optional
import random
import statistics

from .schemas.eval_case import EvalSet, EvalCase
from .schemas.trial import MetricSnapshot
from .capabilities.base import RunOutput


# ---------------------------------------------------------------------------
# Metric registry
# ---------------------------------------------------------------------------

_METRICS: dict[str, Callable[..., float]] = {}


def register_metric(name: str) -> Callable:
    """Decorator. The eval loop only computes metrics that are *both* in
    the registry *and* declared by the capability — protects against typos
    in capability metric lists."""
    def deco(fn: Callable[..., float]) -> Callable[..., float]:
        _METRICS[name] = fn
        return fn
    return deco


def compute(name: str, outputs: list[RunOutput], eval_set: EvalSet, **kwargs) -> Optional[MetricSnapshot]:
    """Compute one metric over a batch of RunOutputs. Returns None if the
    metric is unknown — callers (run.py) log a warning and skip.

    Bootstrap CI: 200 resamples, 90% interval. Small n explicitly disables
    CI (we'd produce nonsense intervals from <10 cases)."""
    fn = _METRICS.get(name)
    if fn is None:
        return None

    # Per-case scores enable bootstrap. Metric functions that don't have a
    # natural per-case score (e.g. cost_usd which is a sum) return a flat
    # list anyway so we can still report n_cases.
    #
    # Filter kwargs to only those the metric function accepts. Most metric
    # functions are 2-arg (outputs, eval_set); only judge-based metrics
    # accept judge_reports. We inspect the signature to thread arguments
    # safely instead of forcing every metric to accept **kwargs.
    import inspect
    sig = inspect.signature(fn)
    has_var_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    if has_var_kwargs:
        allowed = dict(kwargs)
    else:
        allowed = {k: v for k, v in kwargs.items() if k in sig.parameters}
    per_case = fn(outputs, eval_set, **allowed)
    if not per_case:
        return MetricSnapshot(name=name, value=0.0, n_cases=0)

    value = statistics.mean(per_case)
    ci_low = ci_high = None
    if len(per_case) >= 10:
        # Cheap bootstrap; deterministic seed so trials reproduce.
        rng = random.Random(0xBADCAFE)
        boots = []
        for _ in range(200):
            sample = [per_case[rng.randrange(len(per_case))] for _ in per_case]
            boots.append(statistics.mean(sample))
        boots.sort()
        ci_low = boots[10]
        ci_high = boots[-10]

    return MetricSnapshot(
        name=name, value=value, ci_low=ci_low, ci_high=ci_high, n_cases=len(per_case),
    )


# ---------------------------------------------------------------------------
# Concrete metrics
# ---------------------------------------------------------------------------

@register_metric("exact_match_normalized")
def _exact_match(outputs: list[RunOutput], es: EvalSet) -> list[float]:
    """Whitespace+case-normalized exact match. Used by NLQ when we don't
    have an execution environment."""
    by_id = {c.case_id: c for c in es.cases}
    out = []
    for o in outputs:
        expected = by_id[o.case_id].expected
        if expected is None:
            continue
        norm_o = " ".join(str(o.raw_output).lower().split())
        norm_e = " ".join(str(expected).lower().split())
        out.append(1.0 if norm_o == norm_e else 0.0)
    return out


@register_metric("recall_at_5")
def _recall_at_5(outputs: list[RunOutput], es: EvalSet) -> list[float]:
    """Retrieval recall@5 for RAG missions. Uses EvalCase.relevant_doc_ids."""
    return _recall_at_k(outputs, es, k=5)


@register_metric("recall_at_10")
def _recall_at_10(outputs: list[RunOutput], es: EvalSet) -> list[float]:
    return _recall_at_k(outputs, es, k=10)


def _recall_at_k(outputs: list[RunOutput], es: EvalSet, k: int) -> list[float]:
    by_id = {c.case_id: c for c in es.cases}
    out = []
    for o in outputs:
        case = by_id[o.case_id]
        if not case.relevant_doc_ids or not o.retrieved_doc_ids:
            continue
        relevant = set(case.relevant_doc_ids)
        retrieved_topk = set(o.retrieved_doc_ids[:k])
        # Standard recall: fraction of relevant docs that were retrieved.
        out.append(len(relevant & retrieved_topk) / max(1, len(relevant)))
    return out


@register_metric("ndcg_at_10")
def _ndcg_at_10(outputs: list[RunOutput], es: EvalSet) -> list[float]:
    """NDCG@10 for search/ranking missions. Expected = ranked doc_id list."""
    import math
    by_id = {c.case_id: c for c in es.cases}
    out = []
    for o in outputs:
        expected_ranked = by_id[o.case_id].expected or []
        retrieved = o.retrieved_doc_ids or []
        if not expected_ranked or not retrieved:
            continue
        relevance = {d: 1.0 / (1 + i) for i, d in enumerate(expected_ranked)}
        dcg = sum(relevance.get(d, 0.0) / math.log2(i + 2) for i, d in enumerate(retrieved[:10]))
        ideal_relevances = sorted(relevance.values(), reverse=True)[:10]
        idcg = sum(r / math.log2(i + 2) for i, r in enumerate(ideal_relevances))
        out.append(dcg / idcg if idcg > 0 else 0.0)
    return out


@register_metric("map_at_10")
def _map_at_10(outputs: list[RunOutput], es: EvalSet) -> list[float]:
    """Mean Average Precision @ 10."""
    by_id = {c.case_id: c for c in es.cases}
    out = []
    for o in outputs:
        expected = set(by_id[o.case_id].expected or [])
        retrieved = o.retrieved_doc_ids or []
        if not expected or not retrieved:
            continue
        hits = 0
        ap = 0.0
        for i, d in enumerate(retrieved[:10]):
            if d in expected:
                hits += 1
                ap += hits / (i + 1)
        out.append(ap / min(len(expected), 10))
    return out


@register_metric("cost_usd")
def _cost(outputs: list[RunOutput], es: EvalSet) -> list[float]:
    """Per-case USD cost. Reported as mean so the bootstrap CI is over
    *case-level* variance, which is what budget projections need."""
    return [o.cost_usd for o in outputs]


@register_metric("p95_latency_ms")
def _p95_latency(outputs: list[RunOutput], es: EvalSet) -> list[float]:
    """Per-case latency — aggregated value will be the *mean* per-case, but
    run.py also stores the trial-level p95 separately on TrialResult."""
    return [o.latency_ms for o in outputs]


@register_metric("refusal_calibration")
def _refusal_calibration(outputs: list[RunOutput], es: EvalSet) -> list[float]:
    """For chatbot missions tagged with whether the case *should* be
    refused. Score = 1 if refused-when-should-refuse OR answered-when-
    should-answer, else 0."""
    by_id = {c.case_id: c for c in es.cases}
    out = []
    for o in outputs:
        case = by_id[o.case_id]
        should_refuse = "should_refuse" in case.tags
        correct = (o.refused and should_refuse) or (not o.refused and not should_refuse)
        out.append(1.0 if correct else 0.0)
    return out


@register_metric("schema_validity")
def _schema_validity(outputs: list[RunOutput], es: EvalSet) -> list[float]:
    """For insight_agent: did the output parse as valid JSON? The structured
    call wrapper sets raw_output to a dict on success, str on failure."""
    return [1.0 if isinstance(o.raw_output, (dict, list)) else 0.0 for o in outputs]


@register_metric("judge_score")
def _judge_score(outputs: list[RunOutput], es: EvalSet, *, judge_reports: list = None) -> list[float]:
    """LLM-judge aggregated score. `judge_reports` is a list of JudgeReport
    keyed by case_id, supplied by run.py after the judge has been called.
    """
    if not judge_reports:
        return []
    by_id = {r.case_id: r for r in judge_reports}
    return [by_id[o.case_id].score for o in outputs if o.case_id in by_id]


# Aliases used by capabilities — convenient at the cost of mild duplication.
@register_metric("execution_equivalence")
def _exec_equiv(outputs, es): return _exact_match(outputs, es)


@register_metric("syntactic_validity")
def _syntactic_validity(outputs, es): return _schema_validity(outputs, es)


@register_metric("citation_quality")
@register_metric("faithfulness")
@register_metric("coverage")
@register_metric("task_success_rate")
@register_metric("turn_coherence")
@register_metric("insight_precision")
@register_metric("insight_recall")
@register_metric("evidence_grounding")
@register_metric("n_tool_calls_avg")
@register_metric("recall_at_50")
def _placeholder(outputs: list[RunOutput], es: EvalSet, **kwargs) -> list[float]:
    """Placeholder for metrics that require a judge or specialized logic.
    Real implementations live in lib/judges.py + capability-specific scoring.
    The placeholder returns empty list -> MetricSnapshot with n_cases=0,
    which the Auditor flags as 'missing metric implementation'."""
    return []

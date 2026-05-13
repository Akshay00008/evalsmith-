# lib/sketch/__init__.py
# The Eval & Trace Sketch — analogous to the source repo's Process Data
# Sketch but built over GenAI primitives (eval cases, traces, judge reports,
# retrieval traces, failure clusters) instead of tabular distributions.
#
# Hard rule: subagents NEVER load full eval cases or full traces. They go
# through the queries in `queries.py` which return compact summaries.

from .builder import build_sketch, update_sketch_after_trial
from .queries import (
    eval_profile,
    slice_performance,
    failure_clusters,
    retrieval_diagnostics,
    trace_structure,
    cost_breakdown,
    safety_incidents,
)

__all__ = [
    "build_sketch", "update_sketch_after_trial",
    "eval_profile", "slice_performance", "failure_clusters",
    "retrieval_diagnostics", "trace_structure", "cost_breakdown",
    "safety_incidents",
]

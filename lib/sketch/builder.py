# lib/sketch/builder.py
# Builds and incrementally updates the Eval & Trace Sketch on disk.
#
# Design notes:
#   * The sketch is the substrate. Subagents query it via lib/sketch/queries.py
#     and never load it directly — we keep the file layout opaque on purpose.
#   * Writes are append-only jsonl per layer. This means concurrent readers
#     (MCP queries while a trial finishes) never see torn writes.
#   * E1 (profile) is computed once and never updated; the rest grow by one
#     row per trial.

from __future__ import annotations
from pathlib import Path
from typing import Iterable, Optional
import json
import statistics

from ..schemas.eval_case import EvalSet
from ..schemas.trial import TrialResult
from ..schemas.judge import JudgeReport
from .layers import (
    E1EvalProfile, E2FailureCluster, E3SlicePerf,
    E4RetrievalDiag, E5TraceStructure, E6CostLatency, E7SafetyIncident,
    SketchManifest,
)


def build_sketch(project_dir: Path, mission_id: str, eval_set: EvalSet) -> SketchManifest:
    """One-time sketch build, called by /init right after the eval set is
    locked. Computes E1 from the eval set; creates empty jsonl files for
    the trial-keyed layers so subsequent appends don't need to handle the
    'file might not exist' case."""

    sketch_dir = project_dir / "sketch"
    sketch_dir.mkdir(parents=True, exist_ok=True)

    # --- E1: eval profile -----------------------------------------------
    # Char-length distribution; we use a chars/4 heuristic for token counts
    # to avoid pulling in tiktoken as a hard dep. Strategist treats these
    # as ballpark numbers, not precise.
    input_lens = [len(str(c.input)) for c in eval_set.cases]
    expected_lens = [len(str(c.expected)) for c in eval_set.cases if c.expected is not None]

    tag_counts: dict[str, int] = {}
    for c in eval_set.cases:
        for t in c.tags:
            tag_counts[t] = tag_counts.get(t, 0) + 1

    difficulty_buckets = {"easy": 0, "medium": 0, "hard": 0, "unknown": 0}
    for c in eval_set.cases:
        if c.difficulty is None:
            difficulty_buckets["unknown"] += 1
        elif c.difficulty < 0.33:
            difficulty_buckets["easy"] += 1
        elif c.difficulty < 0.66:
            difficulty_buckets["medium"] += 1
        else:
            difficulty_buckets["hard"] += 1

    e1 = E1EvalProfile(
        n_cases=len(eval_set.cases),
        tag_counts=tag_counts,
        input_len_p50=int(_quantile(input_lens, 0.5)) if input_lens else 0,
        input_len_p95=int(_quantile(input_lens, 0.95)) if input_lens else 0,
        expected_output_len_p50=int(_quantile(expected_lens, 0.5)) if expected_lens else None,
        expected_output_len_p95=int(_quantile(expected_lens, 0.95)) if expected_lens else None,
        difficulty_histogram=difficulty_buckets,
        avg_input_tokens=int(statistics.mean(input_lens) / 4) if input_lens else 0,
    )

    # Write E1 as a single JSON (not jsonl) — it's a fixed-size summary,
    # not a stream.
    (sketch_dir / "e1_profile.json").write_text(e1.model_dump_json(indent=2), encoding="utf-8")

    # Touch empty jsonl files for the trial-keyed layers so we don't need
    # to special-case "first append" in the updater.
    for name in ("e2_clusters", "e3_slices", "e4_retrieval", "e5_traces", "e6_cost", "e7_safety"):
        (sketch_dir / f"{name}.jsonl").touch(exist_ok=True)

    manifest = SketchManifest(mission_id=mission_id, eval_set_hash=eval_set.content_hash())
    (sketch_dir / "manifest.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return manifest


def update_sketch_after_trial(
    project_dir: Path,
    trial: TrialResult,
    *,
    slice_metrics: Optional[Iterable[E3SlicePerf]] = None,
    retrieval: Optional[E4RetrievalDiag] = None,
    trace_struct: Optional[E5TraceStructure] = None,
    safety: Optional[Iterable[E7SafetyIncident]] = None,
    failure_clusters: Optional[Iterable[E2FailureCluster]] = None,
) -> None:
    """Append one row per relevant layer for a finished trial. The caller
    is responsible for *computing* the layer rows (run.py does this) —
    builder just persists them. Keeps the I/O surface in one place so
    future migrations only touch this module."""

    sketch_dir = project_dir / "sketch"

    # E6 cost/latency is always computable from TrialResult itself.
    e6 = E6CostLatency(
        trial_id=trial.trial_id,
        total_cost_usd=trial.total_cost_usd,
        p50_latency_ms=trial.p50_latency_ms or 0.0,
        p95_latency_ms=trial.p95_latency_ms or 0.0,
        p99_latency_ms=trial.p95_latency_ms or 0.0,  # approximated when p99 not measured
        cost_per_case_p95_usd=trial.total_cost_usd / max(1, sum(m.n_cases for m in trial.metrics) or 1),
    )
    _append_jsonl(sketch_dir / "e6_cost.jsonl", [e6])

    # The rest are optional — only some missions populate them.
    if slice_metrics is not None:
        _append_jsonl(sketch_dir / "e3_slices.jsonl", slice_metrics)
    if retrieval is not None:
        _append_jsonl(sketch_dir / "e4_retrieval.jsonl", [retrieval])
    if trace_struct is not None:
        _append_jsonl(sketch_dir / "e5_traces.jsonl", [trace_struct])
    if safety is not None:
        _append_jsonl(sketch_dir / "e7_safety.jsonl", safety)
    if failure_clusters is not None:
        _append_jsonl(sketch_dir / "e2_clusters.jsonl", failure_clusters)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _append_jsonl(path: Path, rows: Iterable) -> None:
    """Append pydantic models as JSON lines. We open in append-binary mode
    and write a trailing newline so concurrent readers always see whole
    rows."""
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(r.model_dump_json() + "\n")


def _quantile(values: list[float], q: float) -> float:
    """Plain quantile — we deliberately avoid numpy here so /init can run
    in a minimal environment."""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * q
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)

# lib/sketch/queries.py
# The query surface subagents use to interact with the sketch. Each query
# returns a *small* dict (typically <10KB) so the agent context stays clean.
#
# These functions are also wrapped by mcp_servers/eval_sketch_server.py so
# the same surface is callable from the Claude Code side via MCP.

from __future__ import annotations
from pathlib import Path
from typing import Optional
import json

from .layers import (
    E1EvalProfile, E2FailureCluster, E3SlicePerf,
    E4RetrievalDiag, E5TraceStructure, E6CostLatency, E7SafetyIncident,
)


def _read_jsonl(path: Path) -> list[dict]:
    """Read a sketch jsonl file. Missing files return an empty list because
    a freshly-/init'd project will not have any trial-keyed rows yet."""
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def eval_profile(project_dir: Path) -> dict:
    """E1 query — returns the static eval set profile. Strategist consults
    this when sizing chunk lengths or projecting cost."""
    p = project_dir / "sketch" / "e1_profile.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def slice_performance(
    project_dir: Path,
    *,
    trial_id: Optional[str] = None,
    slice_key: Optional[str] = None,
    metric_name: Optional[str] = None,
    top_n: int = 20,
) -> list[dict]:
    """E3 query — slice metrics with optional filters. Strategist uses this
    to find which slices a recent trial regressed on, then targets them
    with the next variant."""
    rows = _read_jsonl(project_dir / "sketch" / "e3_slices.jsonl")
    if trial_id is not None:
        rows = [r for r in rows if r.get("trial_id") == trial_id]
    if slice_key is not None:
        rows = [r for r in rows if r.get("slice_key") == slice_key]
    if metric_name is not None:
        rows = [r for r in rows if r.get("metric_name") == metric_name]
    # Return only the most recent `top_n` to bound payload size.
    return rows[-top_n:]


def failure_clusters(project_dir: Path, *, top_n: int = 10) -> list[dict]:
    """E2 query — the top failure clusters by count. Inspector populates
    this; Strategist reads it to target the dominant failure mode."""
    rows = _read_jsonl(project_dir / "sketch" / "e2_clusters.jsonl")
    # Dedupe by cluster_id (clusters get updated each iter) — keep the
    # latest row per cluster.
    latest: dict[str, dict] = {}
    for r in rows:
        latest[r["cluster_id"]] = r
    return sorted(latest.values(), key=lambda r: r.get("n_failures_total", 0), reverse=True)[:top_n]


def retrieval_diagnostics(project_dir: Path, *, trial_id: Optional[str] = None) -> list[dict]:
    """E4 query — recall@k, MRR, coverage. Only populated for RAG missions."""
    rows = _read_jsonl(project_dir / "sketch" / "e4_retrieval.jsonl")
    if trial_id is not None:
        rows = [r for r in rows if r.get("trial_id") == trial_id]
    return rows


def trace_structure(project_dir: Path, *, trial_id: Optional[str] = None) -> list[dict]:
    """E5 query — tool-call shape stats. Only populated for agent missions."""
    rows = _read_jsonl(project_dir / "sketch" / "e5_traces.jsonl")
    if trial_id is not None:
        rows = [r for r in rows if r.get("trial_id") == trial_id]
    return rows


def cost_breakdown(project_dir: Path, *, last_n: int = 5) -> list[dict]:
    """E6 query — cost & latency for the last N trials. Strategist reads
    this before proposing model swaps."""
    rows = _read_jsonl(project_dir / "sketch" / "e6_cost.jsonl")
    return rows[-last_n:]


def safety_incidents(project_dir: Path, *, trial_id: Optional[str] = None) -> list[dict]:
    """E7 query — safety pass rates per red-team pattern. Provoker
    populates; Auditor reads to enforce safety_floor."""
    rows = _read_jsonl(project_dir / "sketch" / "e7_safety.jsonl")
    if trial_id is not None:
        rows = [r for r in rows if r.get("trial_id") == trial_id]
    return rows

# lib/sketch/layers.py
# The 7 layers of the Eval & Trace Sketch. Each layer is a *fixed-size*
# JSON-serializable summary — total sketch size <1MB on disk. Subagents
# query specific layers; they never load all of them at once.
#
# Layers:
#   E1 — Eval profile      : taxonomy, length stats, token counts
#   E2 — Failure clusters  : embedding-clustered failure modes (per trial)
#   E3 — Slice performance : metrics broken down by tag / difficulty bucket
#   E4 — Retrieval quality : recall@k, MRR, doc-coverage (RAG missions only)
#   E5 — Trace structure   : tool-call depth, retry rate, dead-ends (agents)
#   E6 — Cost & latency    : per-iter distributions
#   E7 — Safety incidents  : red-team failures, refusals, PII flags

from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class E1EvalProfile(BaseModel):
    """Static profile of the eval set itself — computed once at /init and
    never updated. The Strategist consults it to understand what the eval
    measures (length distribution, tag mix, etc.)."""
    n_cases: int
    # Tag -> count. Lets the Strategist know e.g. that 60% of cases are
    # tagged 'multi-hop', so prompts should explicitly handle chained
    # reasoning.
    tag_counts: dict[str, int] = Field(default_factory=dict)
    # Input length distribution (in characters) — quantiles to keep the
    # layer compact. Subagent uses this to size chunks / max_tokens.
    input_len_p50: int = 0
    input_len_p95: int = 0
    expected_output_len_p50: Optional[int] = None
    expected_output_len_p95: Optional[int] = None
    # Difficulty histogram (bucketed). Computed from baseline-model
    # success rate during /init.
    difficulty_histogram: dict[str, int] = Field(default_factory=dict)
    # Approximate token counts (chars/4 heuristic) — used by the budget
    # ledger to project costs before running.
    avg_input_tokens: int = 0


class E2FailureCluster(BaseModel):
    """A failure cluster surfaced by the Inspector's embedding clustering
    pass. We store *centroids* and *exemplars*, not full failures."""
    cluster_id: str
    label: str = "unlabeled"
    n_failures_total: int = 0
    # First trial in which this cluster appeared. Helps Inspector track
    # whether a cluster is being *introduced* by recent variants or is
    # baseline-persistent.
    first_seen_iteration: int = 0
    last_seen_iteration: int = 0
    # Up to 3 exemplar case_ids — the Inspector pulls these for a
    # qualitative pass without flooding the sketch.
    exemplar_case_ids: list[str] = Field(default_factory=list, max_length=3)


class E3SlicePerf(BaseModel):
    """Per-slice metric on the most recent trial. Slices = tags + difficulty
    buckets. Stored as flat rows to keep the layer flat-table-friendly."""
    trial_id: str
    slice_key: str       # e.g. "tag:multi_hop" or "difficulty:hard"
    metric_name: str
    value: float
    n_cases: int


class E4RetrievalDiag(BaseModel):
    """Retrieval quality summary. Only populated for RAG missions where
    EvalCase.relevant_doc_ids is set."""
    trial_id: str
    recall_at_5: Optional[float] = None
    recall_at_10: Optional[float] = None
    mrr: Optional[float] = None
    # Fraction of unique relevant docs ever retrieved across all eval
    # cases. <0.5 => corpus may be missing content.
    corpus_coverage: float = 0.0
    # Average redundancy: 0 = all retrieved docs distinct, 1 = all duplicates.
    redundancy: float = 0.0


class E5TraceStructure(BaseModel):
    """Agent/tool-use trace summary. Only populated for missions whose
    GenerationConfig declares tools."""
    trial_id: str
    avg_tool_calls_per_case: float = 0.0
    max_tool_depth_p95: int = 0
    retry_rate: float = 0.0
    dead_end_rate: float = 0.0     # cases that exhausted tool budget without producing an answer


class E6CostLatency(BaseModel):
    """Cost & latency distribution per trial. The Auditor reads this to
    enforce the latency/cost budgets."""
    trial_id: str
    total_cost_usd: float
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    cost_per_case_p95_usd: float


class E7SafetyIncident(BaseModel):
    """Safety regressions tracked by the Provoker. Each row = one
    red-team pattern, not one case (so the layer stays compact even with
    a large red-team set)."""
    trial_id: str
    pattern_id: str     # from a fixed red-team library
    pattern_label: str
    pass_rate: float    # fraction of red-team prompts the system handled correctly
    n_prompts: int


class SketchManifest(BaseModel):
    """Pointers to where each layer lives on disk. The actual layers are
    stored as jsonl files (so updates are append-only). The manifest is
    the only file an MCP query needs to open to know what's available."""
    mission_id: str
    eval_set_hash: str
    e1_profile_path: str = "sketch/e1_profile.json"
    e2_clusters_path: str = "sketch/e2_clusters.jsonl"
    e3_slices_path: str = "sketch/e3_slices.jsonl"
    e4_retrieval_path: str = "sketch/e4_retrieval.jsonl"
    e5_traces_path: str = "sketch/e5_traces.jsonl"
    e6_cost_path: str = "sketch/e6_cost.jsonl"
    e7_safety_path: str = "sketch/e7_safety.jsonl"
    # Schema versions so we can migrate later without breaking replays.
    version: int = 1

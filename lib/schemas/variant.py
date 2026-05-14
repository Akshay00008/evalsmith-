# lib/schemas/variant.py
# A Variant is the GenAI analogue of an "experiment plan" — the concrete
# bundle of (prompt, model, retriever, chunking, tools, params) that the
# Operator will run against the eval set. VariantDiffs are what the
# Strategist actually proposes (diff-against-baseline keeps proposals small,
# auditable, and feedable into the doom-loop fingerprint).

from __future__ import annotations
from typing import Literal, Optional, Any
from pydantic import BaseModel, Field
import hashlib
import json


# Technique families — the *arms* of the bandit. The Strategist picks a
# family first (driven by bandit posteriors), then proposes a specific
# variant inside that family. Keeping arms coarse stops the bandit from
# degenerating into a giant action space with no data per arm.
TechniqueFamily = Literal[
    "prompt_rewrite",         # Edit the system or user prompt
    "few_shot_selection",     # Change which examples are in-context
    "model_swap",             # Change the LLM (e.g. Haiku -> Sonnet)
    "retriever_change",       # Swap embedder / BM25 / hybrid
    "chunking_change",        # Resize chunks, change overlap, change boundaries
    "rerank_add_or_change",   # Add/remove a reranker, swap reranker model
    "tool_schema_edit",       # For agent missions — change tool definitions
    "decoding_params",        # Temperature, top_p, max_tokens
    "router_policy",          # For chatbot/engine — change model-routing rules
    "guardrail_add",          # Add an input/output filter (jailbreak, PII, etc.)
]


class PromptBundle(BaseModel):
    """The prompt-side configuration of a variant."""
    system: str = ""
    # User-prompt template with {variable} placeholders that get filled
    # from the EvalCase.input at run time.
    user_template: str = "{input}"
    # In-context examples. Stored as (input, output) tuples so the
    # few_shot_selection family can swap them as a unit.
    few_shots: list[tuple[str, str]] = Field(default_factory=list)


class RetrievalConfig(BaseModel):
    """The retrieval-side configuration. Only meaningful for RAG-flavored
    missions; left at defaults for chatbot/research missions that don't
    retrieve."""
    enabled: bool = False
    embedder: str = "all-MiniLM-L6-v2"   # name resolved by lib/registry.py
    retriever_kind: Literal["dense", "bm25", "hybrid", "none"] = "none"
    chunk_size_tokens: int = 512
    chunk_overlap_tokens: int = 64
    top_k: int = 5
    reranker: Optional[str] = None        # e.g. "bge-reranker-v2-m3" or None


class GenerationConfig(BaseModel):
    """LLM call parameters."""
    model: str = "claude-haiku-4-5"
    temperature: float = 0.0
    max_tokens: int = 1024
    # For tool-use / agent-style missions:
    tools: list[dict] = Field(default_factory=list)
    max_tool_iterations: int = 4


class Variant(BaseModel):
    """A full, self-contained run configuration. The Operator can take a
    Variant and execute it against any EvalSet — no other state required."""
    variant_id: str = Field(..., description="Deterministic hash of the variant content; doubles as doom-loop fingerprint.")
    parent_variant_id: Optional[str] = None
    technique_family: TechniqueFamily

    prompt: PromptBundle = Field(default_factory=PromptBundle)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)

    # Free-form notes the Strategist writes — *not* read by other agents,
    # only by the Inspector during qualitative review.
    rationale_note: str = ""

    @classmethod
    def compute_id(cls, prompt: PromptBundle, retrieval: RetrievalConfig, generation: GenerationConfig) -> str:
        """Content-hash a variant. The bandit and doom-loop use this id as
        the canonical fingerprint — two variants with byte-identical config
        collide intentionally."""
        payload = json.dumps({
            "p": prompt.model_dump(mode="json"),
            "r": retrieval.model_dump(mode="json"),
            "g": generation.model_dump(mode="json"),
        }, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class VariantDiff(BaseModel):
    """What the Strategist actually emits. A diff against a parent Variant —
    cheap to fingerprint, easy for the Auditor to scrutinize one change at
    a time. The Operator materializes a full Variant by applying the diff."""
    parent_variant_id: str
    technique_family: TechniqueFamily
    # JSON-pointer-ish keys. Example: {"prompt.system": "...", "generation.model": "claude-sonnet-4-5"}.
    # The applier in lib/variants.py walks the dotted keys.
    field_changes: dict[str, Any]
    rationale_note: str = ""

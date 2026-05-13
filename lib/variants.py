# lib/variants.py
# Helpers for materializing a full Variant from a parent + VariantDiff,
# and for fingerprinting variants so the doom-loop detector can spot
# near-duplicates.

from __future__ import annotations
from copy import deepcopy
from typing import Any

from .schemas.variant import Variant, VariantDiff, PromptBundle, RetrievalConfig, GenerationConfig


def apply_diff(parent: Variant, diff: VariantDiff) -> Variant:
    """Apply a VariantDiff to a parent Variant, producing a fully-specified
    child Variant. The diff's keys are dotted paths into the variant tree
    (e.g. 'prompt.system', 'generation.model'). Unknown keys raise — we'd
    rather fail loud than silently drop a Strategist proposal."""
    new_state = parent.model_dump()
    # Mutate in-place on the dict copy, then re-validate through pydantic
    # so type errors in the diff surface immediately.
    for dotted, value in diff.field_changes.items():
        _set_dotted(new_state, dotted, value)

    new_prompt = PromptBundle.model_validate(new_state["prompt"])
    new_retrieval = RetrievalConfig.model_validate(new_state["retrieval"])
    new_generation = GenerationConfig.model_validate(new_state["generation"])

    child_id = Variant.compute_id(new_prompt, new_retrieval, new_generation)
    return Variant(
        variant_id=child_id,
        parent_variant_id=parent.variant_id,
        technique_family=diff.technique_family,
        prompt=new_prompt,
        retrieval=new_retrieval,
        generation=new_generation,
        rationale_note=diff.rationale_note,
    )


def _set_dotted(obj: dict, dotted: str, value: Any) -> None:
    """Recursive dict-setter for dotted keys. Raises on unknown keys so the
    Auditor can catch malformed diffs."""
    parts = dotted.split(".")
    cur = obj
    for p in parts[:-1]:
        if p not in cur:
            raise KeyError(f"Unknown variant field: {dotted}")
        cur = cur[p]
    leaf = parts[-1]
    if leaf not in cur and not isinstance(cur, dict):
        raise KeyError(f"Unknown variant field: {dotted}")
    cur[leaf] = value


def normalized_fingerprint(v: Variant) -> str:
    """Doom-loop fingerprint. Normalizes whitespace + lower-cases prompt
    text so paraphrases collide with their parent. Different from
    variant_id (which is exact-content) — used only by lib/doom_loop.py."""
    import hashlib
    norm_system = " ".join(v.prompt.system.lower().split())
    norm_user = " ".join(v.prompt.user_template.lower().split())
    payload = f"{norm_system}||{norm_user}||{v.generation.model}||{v.retrieval.retriever_kind}||{v.retrieval.top_k}||{v.retrieval.chunk_size_tokens}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

# lib/retrieval.py
# Cross-project knowledge retrieval. Reads from the global `knowledge/`
# directory at the framework root (a git-managed shared library) and
# returns snippets relevant to a freshly-/init'd Mission.
#
# Snippet selection is intentionally simple: tag-based filtering plus
# alphabetical sort. We don't want LLM-based retrieval here because this
# runs at /init time before the budget tracker is set up.

from __future__ import annotations
from pathlib import Path
from typing import Optional
import json

from .schemas.mission import Mission


# Files under knowledge/ — each is jsonl with one record per line.
KNOWLEDGE_FILES = [
    "prompt_pattern_library.jsonl",
    "failure_modes.jsonl",
    "rag_recipes.jsonl",
    "model_route_priors.jsonl",
    "eval_judge_templates.jsonl",
]


def knowledge_root() -> Path:
    """Resolve to the framework-level knowledge directory. This is one
    *up* from the per-project workspace so multiple projects share it."""
    # The framework is installed next to a `knowledge/` directory in the
    # repo root. We walk up until we find it; tests stub this via env var.
    import os
    env = os.environ.get("GENAI_KNOWLEDGE_ROOT")
    if env:
        return Path(env)
    # Default: assume the importable package and knowledge/ are siblings.
    here = Path(__file__).resolve().parent.parent  # AgenticGenAIDevTool/
    return here / "knowledge"


def load_snippets_for(mission: Mission, *, top_n_per_file: int = 5) -> list[dict]:
    """Pull relevant snippets from each knowledge file. Filtering is by
    overlap on tags: the snippet's `tags` array must intersect
    {task_modality, domain}.

    Returns flat list of dicts with at least {source_file, ...record fields}.
    """
    root = knowledge_root()
    if not root.exists():
        return []
    out: list[dict] = []
    wanted_tags = {mission.composition.task_modality, mission.domain}
    for fname in KNOWLEDGE_FILES:
        p = root / fname
        if not p.exists():
            continue
        records = []
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                tags = set(rec.get("tags", []))
                # An empty tags list means "applies to all" — useful for
                # universal patterns. We include those too, capped by
                # top_n_per_file so they don't crowd specific matches.
                if not tags or tags & wanted_tags:
                    records.append({**rec, "source_file": fname})
        # Specific matches first, then generals.
        specific = [r for r in records if r.get("tags")]
        general = [r for r in records if not r.get("tags")]
        out.extend((specific + general)[:top_n_per_file])
    return out


def write_pattern(record: dict, *, file_name: str = "prompt_pattern_library.jsonl") -> None:
    """Append a record to a knowledge file. Used by /contribute after the
    post-merge extractor anonymizes a project's findings."""
    root = knowledge_root()
    root.mkdir(parents=True, exist_ok=True)
    p = root / file_name
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

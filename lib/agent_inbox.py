# lib/agent_inbox.py
# The multi-agent JSON channel. Per-iteration directory under
# `memory/agent_inbox/iter_NNNN/`. Subagents never pass payloads in their
# context — they always write a JSON file and emit only the *path* to the
# next agent. This is critical for context budget and for replayability.

from __future__ import annotations
from pathlib import Path
from typing import Type, TypeVar
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def inbox_dir(project_dir: Path, iteration: int) -> Path:
    """Returns the directory for this iteration, creating it on demand.
    Iteration is zero-padded to 4 digits so listings stay sorted."""
    d = project_dir / "memory" / "agent_inbox" / f"iter_{iteration:04d}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write(project_dir: Path, iteration: int, filename: str, payload: BaseModel) -> Path:
    """Write a pydantic payload to the inbox. The caller chooses the
    filename — convention is `<sender>_<artifact>.json`, e.g.
    `strategist_proposal.json`. Returns the written path."""
    path = inbox_dir(project_dir, iteration) / filename
    path.write_text(payload.model_dump_json(indent=2), encoding="utf-8")
    return path


def read(project_dir: Path, iteration: int, filename: str, model: Type[T]) -> T:
    """Read + validate a payload. Raises if the file is missing — callers
    that want optional reads should check existence first via inbox_dir()."""
    path = inbox_dir(project_dir, iteration) / filename
    return model.model_validate_json(path.read_text(encoding="utf-8"))


def list_files(project_dir: Path, iteration: int) -> list[str]:
    """List filenames in this iteration's inbox. Used by /status and by
    the Inspector when assembling synthesis prose."""
    d = inbox_dir(project_dir, iteration)
    return sorted(p.name for p in d.iterdir() if p.is_file())

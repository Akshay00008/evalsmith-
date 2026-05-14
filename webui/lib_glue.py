# webui/lib_glue.py
# Helpers that bridge Streamlit pages and the framework's lib/* code.
# Kept in one place so pages stay focused on UI concerns and we have a
# single point to mock for UI tests.

from __future__ import annotations
from pathlib import Path
import json
import sys
import time


def framework_root() -> Path:
    """Walk up from webui/ to the framework repo root (parent of lib/)."""
    return Path(__file__).resolve().parent.parent


def projects_dir() -> Path:
    return framework_root() / "projects"


def ensure_lib_on_path() -> None:
    """Streamlit's working directory at launch is the repo root, so `lib`
    is usually importable. We add it defensively in case the user runs
    streamlit from a different cwd."""
    root = str(framework_root())
    if root not in sys.path:
        sys.path.insert(0, root)


def list_projects() -> list[dict]:
    """Return a list of {name, status, created_at, has_mission, has_log,
    finalized} dicts for every project."""
    out = []
    pdir = projects_dir()
    if not pdir.exists():
        return out
    for sub in sorted(pdir.iterdir()):
        if not sub.is_dir() or sub.name.startswith("."):
            continue
        proj = {}
        proj_path = sub / "PROJECT.json"
        if proj_path.exists():
            proj = json.loads(proj_path.read_text(encoding="utf-8"))
        mission_path = sub / "MISSION.json"
        log_path = sub / "experiment_log.jsonl"
        final_path = sub / "results" / "FINAL.md"

        n_trials = 0
        if log_path.exists():
            with log_path.open("r", encoding="utf-8") as f:
                n_trials = sum(1 for line in f if line.strip())

        out.append({
            "name": sub.name,
            "status": proj.get("status", "unknown"),
            "created_at": proj.get("created_at_unix", 0),
            "has_mission": mission_path.exists(),
            "has_log": log_path.exists(),
            "n_trials": n_trials,
            "finalized": final_path.exists(),
            "path": str(sub),
        })
    return out


def read_recipes() -> dict[str, dict]:
    """Load all shipped recipes for the New Project / Mission pages."""
    recipes_dir = framework_root() / "recipes"
    if not recipes_dir.exists():
        return {}
    out = {}
    for f in sorted(recipes_dir.glob("*.json")):
        try:
            out[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
    return out


def read_mission(project_name: str):
    """Return Mission or None. Defers the import so pages that don't need
    it don't pay the cost."""
    ensure_lib_on_path()
    from lib.schemas import Mission
    p = projects_dir() / project_name / "MISSION.json"
    if not p.exists():
        return None
    return Mission.model_validate_json(p.read_text(encoding="utf-8"))


def read_eval_set(project_name: str):
    """Return EvalSet or None."""
    ensure_lib_on_path()
    from lib.schemas import EvalCase, EvalSet
    p = projects_dir() / project_name / "data" / "eval_set.jsonl"
    if not p.exists():
        return None
    cases = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(EvalCase.model_validate_json(line))
    return EvalSet(eval_set_id=project_name, cases=cases)


def read_log(project_name: str) -> list[dict]:
    """Return a list of trial dicts from experiment_log.jsonl. Returns
    plain dicts (not TrialResult) so pages can render with pandas without
    pydantic friction."""
    p = projects_dir() / project_name / "experiment_log.jsonl"
    if not p.exists():
        return []
    rows = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def read_budget(project_name: str) -> float:
    """Total USD spent on the project (sum of budget.jsonl rows)."""
    p = projects_dir() / project_name / "budget.jsonl"
    if not p.exists():
        return 0.0
    total = 0.0
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                total += float(json.loads(line).get("amount_usd", 0.0))
            except Exception:
                continue
    return total

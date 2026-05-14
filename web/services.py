# web/services.py
# Business logic shared by the FastAPI routes. Keeping it separate from
# api.py means routes stay thin and we can unit-test the logic without
# spinning up the web server.
#
# Every function here reads / writes the same on-disk project artifacts
# the CLI and Streamlit UI work with — there's only one source of truth.

from __future__ import annotations
from pathlib import Path
from typing import Optional
import json
import shutil
import time
import re


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def framework_root() -> Path:
    """Project root = parent of this `web/` package."""
    return Path(__file__).resolve().parent.parent


def projects_dir() -> Path:
    return framework_root() / "projects"


def project_dir(name: str) -> Path:
    return projects_dir() / name


def valid_name(name: str) -> bool:
    """Project-name validator — restrict to filesystem-safe chars."""
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{2,40}", name or ""))


# ---------------------------------------------------------------------------
# Project list + status
# ---------------------------------------------------------------------------

def list_projects() -> list[dict]:
    """Return a summary record per project for the dashboard."""
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
            try:
                proj = json.loads(proj_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                proj = {}
        mission_path = sub / "MISSION.json"
        log_path = sub / "experiment_log.jsonl"
        final_path = sub / "results" / "FINAL.md"

        n_trials = 0
        if log_path.exists():
            with log_path.open("r", encoding="utf-8") as f:
                n_trials = sum(1 for line in f if line.strip())

        # Status badge logic mirrors the Streamlit Dashboard so users
        # switching between UIs see the same labels.
        if final_path.exists():
            badge = "Finalized"
        elif log_path.exists():
            badge = f"In progress ({n_trials} trials)"
        elif mission_path.exists():
            badge = "Mission locked"
        else:
            badge = "Awaiting setup"

        out.append({
            "name": sub.name,
            "status": proj.get("status", "unknown"),
            "badge": badge,
            "created_at": proj.get("created_at_unix", 0),
            "has_mission": mission_path.exists(),
            "has_log": log_path.exists(),
            "n_trials": n_trials,
            "finalized": final_path.exists(),
            "spent_usd": _budget_total(sub),
        })
    return out


def _budget_total(proj_path: Path) -> float:
    """Sum the project's budget ledger."""
    p = proj_path / "budget.jsonl"
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


# ---------------------------------------------------------------------------
# Project creation
# ---------------------------------------------------------------------------

def create_project(name: str, recipe: Optional[str], domain_hint: str) -> Path:
    """Create a new project workspace. Returns the project directory.
    Raises ValueError on invalid name or collision."""
    if not valid_name(name):
        raise ValueError("Project name must be 2-40 chars, letters/numbers/underscore/dash only.")
    target = project_dir(name)
    if target.exists():
        raise ValueError(f"Project '{name}' already exists.")

    template = projects_dir() / ".templates" / "_project_template"
    if not template.exists():
        raise RuntimeError(f"Project template missing at {template}.")
    shutil.copytree(template, target)

    # Stamp metadata.
    proj_meta = json.loads((target / "PROJECT.json").read_text(encoding="utf-8"))
    proj_meta["name"] = name
    proj_meta["created_at_unix"] = time.time()
    proj_meta["framework_version"] = "0.1.0"
    (target / "PROJECT.json").write_text(json.dumps(proj_meta, indent=2), encoding="utf-8")

    # Optional recipe + domain hint.
    if recipe:
        rp = framework_root() / "recipes" / f"{recipe}.json"
        if rp.exists():
            shutil.copy(rp, target / "recipe.json")
    if domain_hint:
        (target / "domain_hint.txt").write_text(domain_hint, encoding="utf-8")

    return target


def list_recipes() -> dict[str, dict]:
    """All shipped recipes for the New Project + Mission forms."""
    recipes_dir = framework_root() / "recipes"
    out = {}
    if not recipes_dir.exists():
        return out
    for f in sorted(recipes_dir.glob("*.json")):
        try:
            out[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
    return out


# ---------------------------------------------------------------------------
# Eval set upload + parsing
# ---------------------------------------------------------------------------

def save_eval_set_from_rows(name: str, rows: list[dict]) -> int:
    """Persist a list of dicts as eval_set.jsonl. Returns row count."""
    out_path = project_dir(name) / "data" / "eval_set.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for r in rows:
            # Required fields: case_id, input. Everything else optional.
            case = {
                "case_id": str(r.get("case_id", "")),
                "input": r.get("input", ""),
                "expected": r.get("expected") if r.get("expected") not in ("", None) else None,
                "tags": _split_csv(r.get("tags", "")),
            }
            if r.get("relevant_doc_ids"):
                case["relevant_doc_ids"] = _split_csv(r["relevant_doc_ids"])
            f.write(json.dumps(case, ensure_ascii=False, default=str) + "\n")
    return len(rows)


def _split_csv(value) -> list[str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return []
    return [s.strip() for s in str(value).split(",") if s.strip()]


def parse_uploaded_eval_file(filename: str, raw: bytes) -> list[dict]:
    """Parse a CSV / XLSX / JSONL upload into a list of row dicts. Caller
    persists via save_eval_set_from_rows. Raises ValueError on schema issues."""
    ext = Path(filename).suffix.lower()
    if ext == ".csv":
        import pandas as pd
        import io
        df = pd.read_csv(io.BytesIO(raw))
    elif ext == ".xlsx":
        import pandas as pd
        import io
        df = pd.read_excel(io.BytesIO(raw))
    elif ext in (".jsonl", ".json"):
        rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
        return rows
    else:
        raise ValueError(f"Unsupported file type: {ext}")

    missing = {"case_id", "input"} - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {missing}")
    return df.to_dict(orient="records")


def eval_set_size(name: str) -> int:
    p = project_dir(name) / "data" / "eval_set.jsonl"
    if not p.exists():
        return 0
    return sum(1 for line in p.read_text(encoding="utf-8").splitlines() if line.strip())


# ---------------------------------------------------------------------------
# PDFs + ingestion
# ---------------------------------------------------------------------------

def save_uploaded_pdfs(name: str, files: list[tuple[str, bytes]]) -> int:
    """files: list of (filename, bytes). Returns count saved."""
    raw_dir = project_dir(name) / "data" / "raw_pdfs"
    raw_dir.mkdir(parents=True, exist_ok=True)
    for fname, data in files:
        (raw_dir / fname).write_bytes(data)
    return len(files)


def count_pdfs(name: str) -> int:
    raw_dir = project_dir(name) / "data" / "raw_pdfs"
    if not raw_dir.exists():
        return 0
    return len(list(raw_dir.glob("*.pdf")))


def ingest_pdfs(name: str, chunk_size: int, overlap: int) -> int:
    """Call tools/ingest_pdfs.py logic in-process. Returns chunk count."""
    # Defer imports — pypdf is a soft dep.
    import sys
    sys.path.insert(0, str(framework_root()))
    from tools.ingest_pdfs import ingest_project
    return ingest_project(project_dir(name), chunk_size_chars=chunk_size, overlap_chars=overlap, dry_run=False)


def count_corpus_chunks(name: str) -> int:
    p = project_dir(name) / "data" / "corpus.jsonl"
    if not p.exists():
        return 0
    return sum(1 for line in p.read_text(encoding="utf-8").splitlines() if line.strip())


# ---------------------------------------------------------------------------
# Auto-generated eval sets (LLM-driven, from corpus or DB)
# ---------------------------------------------------------------------------

def generate_eval_from_corpus(name: str, *, n_cases: int = 20, model: str = "claude-haiku-4-5") -> dict:
    """LLM-generate questions+answers from the project's PDF corpus.
    Returns a dict suitable for the UI to render previews. Does NOT save
    to disk — caller saves explicitly after user review."""
    import sys
    sys.path.insert(0, str(framework_root()))
    from lib import eval_gen
    result = eval_gen.generate_from_corpus(project_dir(name), n_cases=n_cases, model=model)
    return _gen_result_to_dict(result)


def generate_eval_from_db(name: str, *, n_cases: int = 20, model: str = "claude-sonnet-4-6") -> dict:
    """LLM-generate NL → SQL pairs from the project's DB schema + sample rows."""
    import sys
    sys.path.insert(0, str(framework_root()))
    from lib import eval_gen
    result = eval_gen.generate_from_db(project_dir(name), n_cases=n_cases, model=model)
    return _gen_result_to_dict(result)


def save_generated_eval(name: str, cases: list[dict], *, append: bool = False) -> int:
    """Persist user-reviewed generated cases. Accepts plain dicts so the
    UI doesn't need to round-trip through EvalCase."""
    import sys
    sys.path.insert(0, str(framework_root()))
    from lib import eval_gen
    from lib.schemas import EvalCase
    typed = [EvalCase.model_validate(c) for c in cases]
    return eval_gen.write_eval_set(project_dir(name), typed, append=append)


def _gen_result_to_dict(result) -> dict:
    """Flatten a GenerationResult to JSON-serializable shape for the UI."""
    return {
        "cases": [c.model_dump(mode="json") for c in result.cases],
        "n_attempted": result.n_attempted,
        "n_parsed": result.n_parsed,
        "cost_usd": result.cost_usd,
        "latency_ms": result.latency_ms,
        "model": result.model,
        "warnings": result.warnings,
    }


# ---------------------------------------------------------------------------
# DB connection
# ---------------------------------------------------------------------------

def save_db_config(name: str, url: str, timeout_ms: int, max_rows: int) -> None:
    """Persist data/db.json (gitignored). url is already a SQLAlchemy URL."""
    path = project_dir(name) / "data" / "db.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "url": url,
        "query_timeout_ms": int(timeout_ms),
        "max_rows": int(max_rows),
        "read_only": True,
    }, indent=2), encoding="utf-8")


def introspect_db(name: str) -> tuple[bool, str]:
    """Connect + dump schema. Returns (ok, schema_text_or_error_msg)."""
    import sys
    sys.path.insert(0, str(framework_root()))
    from lib import db as db_mod
    cfg_path = project_dir(name) / "data" / "db.json"
    if not cfg_path.exists():
        return False, "No db.json — configure the connection first."
    try:
        cfg = db_mod.DBConfig.model_validate_json(cfg_path.read_text(encoding="utf-8"))
        schema = db_mod.introspect_schema(cfg)
        if not schema:
            return False, "Connected but no tables found (check permissions)."
        text = db_mod.schema_to_prompt(schema)
        (project_dir(name) / "data" / "schema.txt").write_text(text, encoding="utf-8")
        return True, text
    except Exception as e:
        return False, f"Connection failed: {e}"


# ---------------------------------------------------------------------------
# Mission
# ---------------------------------------------------------------------------

def build_db_url(db_type: str, host: str = "", port: str = "", dbname: str = "",
                 user: str = "", password: str = "", sqlite_path: str = "", service: str = "") -> str:
    """Compose a SQLAlchemy URL from form fields. Pulls dialect-specific
    template so the user never sees a URL until it's correct."""
    if db_type == "sqlite":
        return f"sqlite:///{sqlite_path}"
    if db_type == "postgresql":
        return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{dbname}"
    if db_type == "mysql":
        return f"mysql+pymysql://{user}:{password}@{host}:{port}/{dbname}"
    if db_type == "oracle":
        return f"oracle+oracledb://{user}:{password}@{host}:{port}/?service_name={service}"
    if db_type == "mssql":
        return f"mssql+pyodbc://{user}:{password}@{host}/{dbname}?driver=ODBC+Driver+18+for+SQL+Server"
    raise ValueError(f"Unknown db type: {db_type}")


def lock_mission(
    name: str, *, goal_prose: str, modality: str, eval_strategy: str,
    metric: str, target: float, floor: float,
    domain: str, total_budget_usd: float, max_iterations: int,
) -> dict:
    """Build + persist MISSION.json. Returns its dict form."""
    import sys
    sys.path.insert(0, str(framework_root()))
    from lib.schemas import Mission, MissionTuple, SuccessCriterion
    from lib.schemas import EvalSet, EvalCase

    p = project_dir(name)
    cases = []
    with (p / "data" / "eval_set.jsonl").open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(EvalCase.model_validate_json(line))
    eval_set = EvalSet(eval_set_id=name, cases=cases)
    if len(eval_set) < 20:
        raise ValueError(f"Eval set has {len(eval_set)} cases; need at least 20.")

    comp = MissionTuple(task_modality=modality, eval_strategy=eval_strategy)
    crit = SuccessCriterion(
        metric=metric,
        operator=">=" if metric != "p95_latency_ms" else "<=",
        target=target, operational_floor=floor, is_primary=True,
    )
    mission = Mission(
        mission_id=Mission.compute_id(name, comp, eval_set.content_hash(), goal_prose),
        project_name=name,
        framework_version="0.1.0",
        goal_prose=goal_prose,
        composition=comp,
        success_criteria=[crit],
        domain=domain,
        eval_set_hash=eval_set.content_hash(),
        total_budget_usd=float(total_budget_usd),
        max_iterations=int(max_iterations),
    )
    (p / "MISSION.json").write_text(mission.model_dump_json(indent=2), encoding="utf-8")

    # Update project status.
    proj_meta = json.loads((p / "PROJECT.json").read_text(encoding="utf-8"))
    proj_meta["status"] = "planned"
    (p / "PROJECT.json").write_text(json.dumps(proj_meta, indent=2), encoding="utf-8")

    return mission.model_dump(mode="json")


def read_mission_dict(name: str) -> Optional[dict]:
    p = project_dir(name) / "MISSION.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Trial log + run + finalize
# ---------------------------------------------------------------------------

def read_log(name: str) -> list[dict]:
    p = project_dir(name) / "experiment_log.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def finalize_project(name: str) -> dict:
    """Compose FINAL.md + winning variant. Returns {confidence, decision, path}."""
    import sys
    sys.path.insert(0, str(framework_root()))
    from lib import finalize as fin_mod
    from lib.schemas import Mission
    from lib.schemas.state import RunState
    from lib.schemas.trial import TrialResult
    from webui.headless_optimizer import pin_winning_variant

    p = project_dir(name)
    mission = Mission.model_validate_json((p / "MISSION.json").read_text(encoding="utf-8"))
    log_rows = read_log(name)
    if not log_rows:
        raise ValueError("No trials run yet — nothing to finalize.")
    trials = [TrialResult.model_validate(r) for r in log_rows]
    rs = RunState(
        mission_id=mission.mission_id, current_iteration=len(trials),
        terminated=True, terminated_reason="iteration_cap",
    )
    rec = fin_mod.assemble_recommendation(
        project_dir=p, mission=mission, run_state=rs, log=trials, judge_calibration=None,
    )
    fin_path = fin_mod.write_final_md(p, rec, mission)
    pin_winning_variant(p, mission)
    # Empty bundle as a placeholder for /contribute later.
    (p / "results" / "knowledge_bundle.json").write_text(
        json.dumps({"prompt_patterns": [], "rag_recipes": [], "failure_modes": [],
                    "model_routes": [], "judge_templates": []}, indent=2),
        encoding="utf-8",
    )
    return {
        "confidence": rec.confidence,
        "decision": rec.decision_one_sentence,
        "path": str(fin_path.relative_to(framework_root())),
    }


def read_final_md(name: str) -> Optional[str]:
    p = project_dir(name) / "results" / "FINAL.md"
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")

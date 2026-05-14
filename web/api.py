# web/api.py
# FastAPI application — the Docker-deployable frontend.
#
# Routes are organized by screen, mirroring the user's mental model:
#   GET  /                         → dashboard
#   GET  /projects/new             → wizard form
#   POST /projects/new             → create + redirect
#   GET  /projects/{name}          → project home (status + actions)
#   GET  /projects/{name}/upload   → upload data form
#   POST /projects/{name}/upload/eval     → save eval set
#   POST /projects/{name}/upload/pdfs     → save PDFs
#   POST /projects/{name}/upload/ingest   → ingest PDFs
#   POST /projects/{name}/upload/db       → save DB config + introspect
#   GET  /projects/{name}/mission  → mission form
#   POST /projects/{name}/mission  → lock mission
#   GET  /projects/{name}/run      → run page (with SSE for progress)
#   POST /projects/{name}/run/start         → kick off N iters (returns SSE)
#   POST /projects/{name}/run/finalize      → write FINAL.md
#   GET  /projects/{name}/results  → render FINAL.md
#   GET  /projects/{name}/chat     → chat page
#   POST /projects/{name}/chat     → send a message, get the reply
#
# Server-Sent Events stream progress events line-by-line as the headless
# optimizer yields them, so the run page updates live without WebSockets.

from __future__ import annotations
from pathlib import Path
from typing import Optional
import json
import sys

from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Make `lib`, `webui`, `tools` importable from anywhere.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from web import services  # noqa: E402


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="evalsmith web",
    description="No-code Docker-deployable UI for the evalsmith framework.",
    version="0.1.0",
)

# Static + templates relative to this file. Works inside Docker too because
# the WORKDIR is the repo root.
_WEB_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(_WEB_DIR / "templates"))


# Inject a couple of helpers into every template's globals so we don't
# have to pass them as kwargs everywhere.
@app.middleware("http")
async def add_globals(request: Request, call_next):
    response = await call_next(request)
    return response


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    projects = services.list_projects()
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "projects": projects,
        "page_title": "Dashboard",
    })


# ---------------------------------------------------------------------------
# New project
# ---------------------------------------------------------------------------

# Friendly modality labels → recipe filenames.
MODALITY_OPTIONS = [
    ("Question answering over my documents (RAG)", "rag_qa"),
    ("Natural language → SQL query", "nlq_sql"),
    ("Multi-step research with citations", "research_citation"),
    ("Extract structured info from documents", "insight_extraction"),
    ("Search / rank documents", "search_engine"),
    ("Customer support chatbot", "chatbot_support"),
]
DOMAIN_OPTIONS = ["general", "support_bot", "code_assistant", "search_qa", "extraction"]


@app.get("/projects/new", response_class=HTMLResponse)
async def new_project_form(request: Request):
    return templates.TemplateResponse("new_project.html", {
        "request": request,
        "modality_options": MODALITY_OPTIONS,
        "domain_options": DOMAIN_OPTIONS,
        "page_title": "New project",
    })


@app.post("/projects/new")
async def new_project_submit(
    request: Request,
    name: str = Form(...),
    recipe: str = Form(...),
    domain: str = Form("general"),
):
    try:
        services.create_project(name, recipe=recipe, domain_hint=domain)
    except (ValueError, RuntimeError) as e:
        # Render the form again with the error message.
        return templates.TemplateResponse("new_project.html", {
            "request": request,
            "modality_options": MODALITY_OPTIONS,
            "domain_options": DOMAIN_OPTIONS,
            "error": str(e),
            "form_name": name,
            "form_recipe": recipe,
            "form_domain": domain,
            "page_title": "New project",
        }, status_code=400)
    return RedirectResponse(f"/projects/{name}", status_code=303)


# ---------------------------------------------------------------------------
# Project home
# ---------------------------------------------------------------------------

@app.get("/projects/{name}", response_class=HTMLResponse)
async def project_home(request: Request, name: str):
    if not services.project_dir(name).exists():
        raise HTTPException(404, f"Project '{name}' not found.")
    mission = services.read_mission_dict(name)
    n_eval = services.eval_set_size(name)
    n_pdfs = services.count_pdfs(name)
    n_chunks = services.count_corpus_chunks(name)
    n_trials = len(services.read_log(name))
    has_final = services.read_final_md(name) is not None
    return templates.TemplateResponse("project_home.html", {
        "request": request,
        "name": name,
        "mission": mission,
        "n_eval": n_eval,
        "n_pdfs": n_pdfs,
        "n_chunks": n_chunks,
        "n_trials": n_trials,
        "has_final": has_final,
        "page_title": name,
    })


# ---------------------------------------------------------------------------
# Upload data
# ---------------------------------------------------------------------------

@app.get("/projects/{name}/upload", response_class=HTMLResponse)
async def upload_form(request: Request, name: str):
    if not services.project_dir(name).exists():
        raise HTTPException(404)
    return templates.TemplateResponse("upload.html", {
        "request": request,
        "name": name,
        "n_eval": services.eval_set_size(name),
        "n_pdfs": services.count_pdfs(name),
        "n_chunks": services.count_corpus_chunks(name),
        "page_title": f"{name} · Upload",
    })


@app.post("/projects/{name}/upload/eval")
async def upload_eval(name: str, file: UploadFile = File(...)):
    raw = await file.read()
    try:
        rows = services.parse_uploaded_eval_file(file.filename, raw)
        n = services.save_eval_set_from_rows(name, rows)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse({"saved": n, "message": f"Saved {n} eval cases."})


@app.post("/projects/{name}/upload/pdfs")
async def upload_pdfs(name: str, files: list[UploadFile] = File(...)):
    saved = []
    for f in files:
        saved.append((f.filename, await f.read()))
    n = services.save_uploaded_pdfs(name, saved)
    return JSONResponse({"saved": n, "message": f"Saved {n} PDF(s)."})


@app.post("/projects/{name}/upload/ingest")
async def ingest(
    name: str,
    chunk_size: int = Form(1500),
    overlap: int = Form(200),
):
    try:
        n = services.ingest_pdfs(name, chunk_size, overlap)
    except Exception as e:
        raise HTTPException(500, f"Ingestion failed: {e}")
    return JSONResponse({"chunks": n, "message": f"Ingested {n} chunks."})


@app.post("/projects/{name}/upload/db")
async def save_db(
    name: str,
    db_type: str = Form(...),
    host: str = Form(""),
    port: str = Form(""),
    dbname: str = Form(""),
    user: str = Form(""),
    password: str = Form(""),
    sqlite_path: str = Form(""),
    service: str = Form(""),
    timeout_ms: int = Form(5000),
    max_rows: int = Form(1000),
):
    url = services.build_db_url(
        db_type, host=host, port=port, dbname=dbname,
        user=user, password=password, sqlite_path=sqlite_path, service=service,
    )
    services.save_db_config(name, url, timeout_ms, max_rows)
    ok, msg = services.introspect_db(name)
    return JSONResponse({"ok": ok, "schema": msg if ok else None, "error": None if ok else msg})


# ---------------------------------------------------------------------------
# Mission
# ---------------------------------------------------------------------------

METRIC_OPTIONS = [
    ("judge_score", "Judge score (LLM rates 1-5; default for RAG/chatbot)"),
    ("exact_match_normalized", "Exact match (answer text must match)"),
    ("execution_equivalence", "SQL execution equivalence (NLQ + DB)"),
    ("recall_at_5", "Retrieval recall@5 (RAG)"),
    ("ndcg_at_10", "Ranking NDCG@10 (search)"),
    ("task_success_rate", "Task success rate (chatbot)"),
    ("insight_precision", "Insight precision (extraction)"),
    ("insight_recall", "Insight recall (extraction)"),
]
STRATEGY_OPTIONS = [
    ("judge_llm", "LLM-as-judge — best for open-ended answers"),
    ("exact_match", "Exact match — answer text must match"),
    ("tool_call_match", "Tool-call match — NLQ without a DB"),
    ("embedding_similarity", "Embedding similarity — semantic match"),
]
MODALITY_BY_RECIPE = {
    "rag_qa": "rag_qa", "nlq_sql": "nlq_to_query", "research_citation": "research_agent",
    "insight_extraction": "insight_agent", "search_engine": "search_engine",
    "chatbot_support": "chatbot",
}


@app.get("/projects/{name}/mission", response_class=HTMLResponse)
async def mission_form(request: Request, name: str):
    if not services.project_dir(name).exists():
        raise HTTPException(404)
    existing = services.read_mission_dict(name)
    # Try to pre-fill from the recipe.
    recipe_path = services.project_dir(name) / "recipe.json"
    recipe = json.loads(recipe_path.read_text(encoding="utf-8")) if recipe_path.exists() else {}
    domain_hint_path = services.project_dir(name) / "domain_hint.txt"
    default_domain = domain_hint_path.read_text(encoding="utf-8").strip() if domain_hint_path.exists() else "general"

    recipe_modality = recipe.get("composition", {}).get("task_modality", "rag_qa")
    recipe_strategy = recipe.get("composition", {}).get("eval_strategy", "judge_llm")
    crit = (recipe.get("success_criteria") or [{}])[0]

    return templates.TemplateResponse("mission.html", {
        "request": request,
        "name": name,
        "existing": existing,
        "n_eval": services.eval_set_size(name),
        "metric_options": METRIC_OPTIONS,
        "strategy_options": STRATEGY_OPTIONS,
        "domain_options": DOMAIN_OPTIONS,
        "default_metric": crit.get("metric", "judge_score"),
        "default_target": crit.get("target", 0.80),
        "default_floor": crit.get("operational_floor", 0.65),
        "default_strategy": recipe_strategy,
        "default_domain": default_domain,
        "default_modality": recipe_modality,
        "page_title": f"{name} · Mission",
    })


@app.post("/projects/{name}/mission")
async def mission_submit(
    request: Request, name: str,
    goal_prose: str = Form(...),
    modality: str = Form(...),
    eval_strategy: str = Form(...),
    metric: str = Form(...),
    target: float = Form(...),
    floor: float = Form(...),
    domain: str = Form("general"),
    total_budget_usd: float = Form(50.0),
    max_iterations: int = Form(15),
):
    try:
        services.lock_mission(
            name, goal_prose=goal_prose, modality=modality,
            eval_strategy=eval_strategy, metric=metric, target=target, floor=floor,
            domain=domain, total_budget_usd=total_budget_usd, max_iterations=max_iterations,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return RedirectResponse(f"/projects/{name}/run", status_code=303)


# ---------------------------------------------------------------------------
# Run + Finalize (Server-Sent Events for live progress)
# ---------------------------------------------------------------------------

@app.get("/projects/{name}/run", response_class=HTMLResponse)
async def run_page(request: Request, name: str):
    mission = services.read_mission_dict(name)
    if not mission:
        raise HTTPException(400, "Mission not locked yet.")
    log = services.read_log(name)
    spent = sum(t.get("total_cost_usd", 0) for t in log)
    return templates.TemplateResponse("run.html", {
        "request": request,
        "name": name,
        "mission": mission,
        "n_trials": len(log),
        "spent": spent,
        "page_title": f"{name} · Run",
    })


@app.post("/projects/{name}/run/start")
async def run_start(name: str, max_iters: int = Form(8)):
    """Kick off the headless optimizer; returns an SSE stream of progress events."""
    sys.path.insert(0, str(_PROJECT_ROOT))
    from lib.schemas import Mission, EvalCase, EvalSet
    from webui.headless_optimizer import run_optimization

    proj_path = services.project_dir(name)
    mission = Mission.model_validate_json((proj_path / "MISSION.json").read_text(encoding="utf-8"))
    eval_cases = []
    with (proj_path / "data" / "eval_set.jsonl").open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                eval_cases.append(EvalCase.model_validate_json(line))
    eval_set = EvalSet(eval_set_id=name, cases=eval_cases)

    def gen():
        """SSE event generator. Each yield is one event delivered to the
        browser as it happens. Format: `data: <json>\\n\\n`."""
        try:
            for evt in run_optimization(proj_path, mission, eval_set, max_iters=int(max_iters)):
                payload = {
                    "iter": evt.iteration,
                    "phase": evt.phase,
                    "arm": evt.arm or "",
                    "message": evt.message,
                    "metric": evt.primary_metric_value,
                    "spent": evt.budget_spent_usd,
                    "terminated_reason": evt.terminated_reason,
                }
                yield f"data: {json.dumps(payload)}\n\n"
                if evt.terminated_reason:
                    break
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e), 'phase': 'error'})}\n\n"
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/projects/{name}/run/finalize")
async def run_finalize(name: str):
    try:
        result = services.finalize_project(name)
    except Exception as e:
        raise HTTPException(500, f"Finalize failed: {e}")
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@app.get("/projects/{name}/results", response_class=HTMLResponse)
async def results_page(request: Request, name: str):
    md = services.read_final_md(name)
    if md is None:
        raise HTTPException(404, "No FINAL.md — run + finalize first.")
    log = services.read_log(name)
    chart_rows = []
    for t in log:
        metrics = t.get("metrics") or []
        if metrics:
            chart_rows.append({
                "iter": t.get("iteration", 0),
                "value": metrics[0].get("value", 0),
                "cost": t.get("total_cost_usd", 0),
            })
    return templates.TemplateResponse("results.html", {
        "request": request,
        "name": name,
        "final_md": md,
        "chart_rows": json.dumps(chart_rows),
        "n_trials": len(log),
        "page_title": f"{name} · Results",
    })


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

# Per-session message buffers, keyed by (project, browser session via cookie).
# For Docker single-user deployments this in-memory store is fine; for
# multi-user deployments you'd swap it for Redis or a DB.
_CHAT_BUFFERS: dict[str, list[dict]] = {}


@app.get("/projects/{name}/chat", response_class=HTMLResponse)
async def chat_page(request: Request, name: str):
    if not services.read_mission_dict(name):
        raise HTTPException(400, "Mission not locked yet.")
    msgs = _CHAT_BUFFERS.get(name, [])
    return templates.TemplateResponse("chat.html", {
        "request": request,
        "name": name,
        "messages": msgs,
        "page_title": f"{name} · Chat",
    })


@app.post("/projects/{name}/chat")
async def chat_send(name: str, message: str = Form(...)):
    """Process one user message; return the assistant reply as HTML
    fragments so HTMX can append them to the chat window."""
    sys.path.insert(0, str(_PROJECT_ROOT))
    from lib import chat as chat_mod, registry as model_registry

    proj_path = services.project_dir(name)
    mission_obj, variant = chat_mod.load_winning_variant(proj_path)
    modality = mission_obj.composition.task_modality

    buf = _CHAT_BUFFERS.setdefault(name, [])
    buf.append({"role": "user", "content": message})

    # Dispatch by modality — same shape as the Streamlit chat page.
    try:
        if modality == "chatbot":
            turns = [{"role": m["role"], "content": m["content"]} for m in buf]
            call = model_registry.chat_call(
                system=variant.prompt.system, turns=turns,
                generation=variant.generation, few_shots=variant.prompt.few_shots,
            )
            response_text = call.text
            diag = f"{call.input_tokens}+{call.output_tokens} tok · ${call.cost_usd:.4f}"
        elif modality == "rag_qa":
            retrieved = model_registry.retrieve(query=message, config=variant.retrieval, corpus_dir=proj_path)
            ctx_block = "\n\n".join(f"[{d['doc_id']}] {d['text']}" for d in retrieved)
            rendered = (
                variant.prompt.user_template.format(input=message, context=ctx_block)
                if "{context}" in variant.prompt.user_template
                else f"Context:\n{ctx_block}\n\nQuestion: {message}"
            )
            call = model_registry.model_call(
                system=variant.prompt.system, user=rendered,
                generation=variant.generation, few_shots=variant.prompt.few_shots,
            )
            response_text = call.text
            cite = ", ".join(d["doc_id"] for d in retrieved[:3]) if retrieved else "(no chunks)"
            diag = f"retrieved: {cite} · {call.input_tokens}+{call.output_tokens} tok · ${call.cost_usd:.4f}"
        elif modality == "nlq_to_query":
            from lib.capabilities.nlq import _extract_sql
            call = model_registry.model_call(
                system=variant.prompt.system,
                user=variant.prompt.user_template.format(input=message),
                generation=variant.generation, few_shots=variant.prompt.few_shots,
            )
            sql = _extract_sql(call.text)
            response_text = f"```sql\n{sql}\n```"
            db_cfg = proj_path / "data" / "db.json"
            if db_cfg.exists():
                from lib import db as db_mod
                cfg = db_mod.DBConfig.model_validate_json(db_cfg.read_text(encoding="utf-8"))
                res = db_mod.safe_execute(cfg, sql)
                if res.ok:
                    rows = res.rows[:10]
                    table = ["| " + " | ".join(res.columns) + " |", "|" + "|".join(["---"] * len(res.columns)) + "|"]
                    for r in rows:
                        table.append("| " + " | ".join(str(v) for v in r) + " |")
                    response_text += "\n\n" + "\n".join(table)
                else:
                    response_text += f"\n\n_Execution failed: {res.error_message}_"
            diag = f"{call.input_tokens}+{call.output_tokens} tok · ${call.cost_usd:.4f}"
        else:
            call = model_registry.model_call(
                system=variant.prompt.system,
                user=variant.prompt.user_template.format(input=message),
                generation=variant.generation, few_shots=variant.prompt.few_shots,
            )
            response_text = call.text
            diag = f"{call.input_tokens}+{call.output_tokens} tok · ${call.cost_usd:.4f}"
    except Exception as e:
        response_text = f"_Error: {e}_"
        diag = ""

    buf.append({"role": "assistant", "content": response_text, "diag": diag})

    # Return both messages as HTML fragments — HTMX appends them.
    return templates.TemplateResponse("chat_fragment.html", {
        "request": None,
        "user_msg": message,
        "assistant_msg": response_text,
        "diag": diag,
    })


@app.post("/projects/{name}/chat/reset")
async def chat_reset(name: str):
    _CHAT_BUFFERS.pop(name, None)
    return RedirectResponse(f"/projects/{name}/chat", status_code=303)


# ---------------------------------------------------------------------------
# Health / version (useful behind a load balancer)
# ---------------------------------------------------------------------------

@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": "0.1.0"}

# Mission — replaces the /plan Architect Q&A. The user reviews + locks
# the optimization goals (metric, target, floor, budget) via a form.

from __future__ import annotations

import sys
from pathlib import Path
# Streamlit runs each page directly, so the project root isn't on sys.path
# automatically. We inject it so the webui imports resolve.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
import json
from pathlib import Path

import streamlit as st

from webui.lib_glue import ensure_lib_on_path, projects_dir, read_recipes, read_eval_set, read_mission

ensure_lib_on_path()

st.title("🎯 Mission")
st.markdown(
    "This screen locks the **goals** for the optimizer. Fill in the form below and click **Lock Mission** when you're satisfied. "
    "Once locked, the Mission is immutable — you'd start a new project to change it."
)

proj_name = st.session_state.get("active_project")
if not proj_name:
    st.warning("No active project. Pick one on the **📊 Dashboard** first.")
    st.stop()

project_dir = projects_dir() / proj_name

# Show current Mission if one exists already.
existing_mission = read_mission(proj_name)
if existing_mission:
    st.success(f"✅ Mission already locked. ID: `{existing_mission.mission_id}`")
    st.json(existing_mission.model_dump(mode="json"))
    if st.button("🗑 Discard current Mission and re-lock"):
        (project_dir / "MISSION.json").unlink()
        st.rerun()
    st.stop()

# Mission requires an eval set first.
eval_set = read_eval_set(proj_name)
if not eval_set:
    st.error("This project has no eval set yet. Go to **📁 Upload Data** to add one.")
    st.stop()

if len(eval_set) < 20:
    st.error(f"Eval set has only {len(eval_set)} cases. Need at least **20** before locking a Mission.")
    st.stop()

# Pull recipe defaults if present — gives the user sensible starting values.
recipe_path = project_dir / "recipe.json"
recipe: dict = {}
if recipe_path.exists():
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))

domain_hint_path = project_dir / "domain_hint.txt"
default_domain = domain_hint_path.read_text(encoding="utf-8").strip() if domain_hint_path.exists() else "general"

recipe_composition = recipe.get("composition", {})
recipe_crit = recipe.get("success_criteria", [{}])[0]


# Form -------------------------------------------------------------

st.subheader("Goal")
goal_prose = st.text_area(
    "Describe what success looks like, in your own words",
    placeholder="e.g. 'Customers should get accurate, sourced answers to product policy questions in under 4 seconds.'",
    height=80,
)

# Metric + target.
st.subheader("Primary success metric")
metric_options = {
    "judge_score":           "Judge score (LLM rates the answer quality 1-5; default for RAG/chatbot)",
    "exact_match_normalized": "Exact match (answer text must match expected; default for NLQ)",
    "execution_equivalence":  "SQL execution equivalence (run both queries, compare results; NLQ + DB)",
    "recall_at_5":           "Retrieval recall@5 (RAG only)",
    "ndcg_at_10":            "Ranking NDCG@10 (search only)",
    "task_success_rate":     "Task success rate (chatbot)",
    "insight_precision":     "Insight precision (extraction)",
    "insight_recall":        "Insight recall (extraction)",
}
default_metric = recipe_crit.get("metric", "judge_score")
metric = st.selectbox(
    "What should we maximize?",
    list(metric_options.keys()),
    format_func=lambda k: metric_options[k],
    index=list(metric_options.keys()).index(default_metric) if default_metric in metric_options else 0,
)

col1, col2 = st.columns(2)
target = col1.slider(
    "Target value (the optimizer stops when this is hit)",
    min_value=0.0, max_value=1.0, value=float(recipe_crit.get("target", 0.8)), step=0.05,
)
floor = col2.slider(
    "Operational floor (below this, the project has failed)",
    min_value=0.0, max_value=1.0, value=float(recipe_crit.get("operational_floor", 0.6)), step=0.05,
    help="If the best variant lands below this floor, FINAL.md will say 'no_signal — collect more data'.",
)


st.subheader("Budgets")
col3, col4 = st.columns(2)
total_budget = col3.number_input(
    "Total budget (USD)",
    min_value=0.5, max_value=10_000.0,
    value=float(recipe.get("composition", {}).get("cost_budget_usd_per_1k", 2.0) * 10),
    step=1.0,
    help="The optimizer stops when this is spent.",
)
max_iters = col4.number_input(
    "Max iterations",
    min_value=3, max_value=200, value=15, step=1,
    help="Each iteration runs all your eval cases once. More iters = more exploration but more cost/time.",
)


st.subheader("Domain hint")
DOMAIN_OPTIONS = ["general", "support_bot", "code_assistant", "search_qa", "extraction"]
domain = st.selectbox(
    "Domain (priors to seed the optimizer with)",
    DOMAIN_OPTIONS,
    index=DOMAIN_OPTIONS.index(default_domain) if default_domain in DOMAIN_OPTIONS else 0,
)


st.subheader("Advanced — eval strategy")
eval_strategy_label = {
    "judge_llm":         "LLM-as-judge — best for open-ended answers (default)",
    "exact_match":       "Exact match — answer text must match expected",
    "tool_call_match":   "Tool-call match — for NLQ projects without a DB",
    "embedding_similarity": "Embedding similarity — semantic match",
}
default_strategy = recipe_composition.get("eval_strategy", "judge_llm")
eval_strategy = st.selectbox(
    "How should the optimizer score answers?",
    list(eval_strategy_label.keys()),
    format_func=lambda k: eval_strategy_label[k],
    index=list(eval_strategy_label.keys()).index(default_strategy) if default_strategy in eval_strategy_label else 0,
)


# Lock --------------------------------------------------------------

st.divider()

if st.button("🔒 Lock Mission", type="primary", disabled=not goal_prose):
    from lib.schemas import Mission, MissionTuple, SuccessCriterion

    comp = MissionTuple(
        task_modality=recipe_composition.get("task_modality", "rag_qa"),
        eval_strategy=eval_strategy,
        safety_floor=recipe_composition.get("safety_floor", 0.95),
        recommendation_shape=recipe_composition.get("recommendation_shape", "config_bundle"),
    )
    crit = SuccessCriterion(
        metric=metric,
        operator=">=" if metric != "p95_latency_ms" else "<=",
        target=target,
        operational_floor=floor,
        is_primary=True,
    )
    mission = Mission(
        mission_id=Mission.compute_id(proj_name, comp, eval_set.content_hash(), goal_prose),
        project_name=proj_name,
        framework_version="0.1.0",
        goal_prose=goal_prose,
        composition=comp,
        success_criteria=[crit],
        domain=domain,
        eval_set_hash=eval_set.content_hash(),
        total_budget_usd=float(total_budget),
        max_iterations=int(max_iters),
    )
    (project_dir / "MISSION.json").write_text(mission.model_dump_json(indent=2), encoding="utf-8")

    # Update PROJECT.json status.
    proj_meta = json.loads((project_dir / "PROJECT.json").read_text(encoding="utf-8"))
    proj_meta["status"] = "planned"
    (project_dir / "PROJECT.json").write_text(json.dumps(proj_meta, indent=2), encoding="utf-8")

    st.success(f"✅ Mission locked. ID: `{mission.mission_id}`. Next: go to **🚀 Run**.")
    st.balloons()

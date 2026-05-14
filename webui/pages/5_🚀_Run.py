# Run — kicks off the headless optimizer and streams progress live.

from __future__ import annotations

import sys
from pathlib import Path
# Streamlit runs each page directly, so the project root isn't on sys.path
# automatically. We inject it so the webui imports resolve.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
from pathlib import Path
import time

import streamlit as st
import pandas as pd

from webui.lib_glue import ensure_lib_on_path, projects_dir, read_mission, read_eval_set, read_log, read_budget

ensure_lib_on_path()

st.title("🚀 Run")

proj_name = st.session_state.get("active_project")
if not proj_name:
    st.warning("No active project. Pick one on the **📊 Dashboard** first.")
    st.stop()

project_dir = projects_dir() / proj_name
mission = read_mission(proj_name)
if not mission:
    st.error("This project has no Mission yet. Go to **🎯 Mission** first.")
    st.stop()

eval_set = read_eval_set(proj_name)
if not eval_set:
    st.error("This project has no eval set yet. Go to **📁 Upload Data** first.")
    st.stop()


# Mission summary at the top --------------------------------------

primary = mission.primary_criterion()
cols = st.columns(4)
cols[0].metric("Project", proj_name)
cols[1].metric("Goal", f"{primary.metric} {primary.operator} {primary.target}", help=f"Floor: {primary.operational_floor}")
cols[2].metric("Budget", f"${mission.total_budget_usd:.0f}")
cols[3].metric("Max iters", mission.max_iterations)

# Current state --------------------------------------------------

prior_log = read_log(proj_name)
spent = read_budget(proj_name)
if prior_log:
    best = max(prior_log, key=lambda t: t.get("metrics", [{}])[0].get("value", -1e9) if t.get("metrics") else -1e9)
    best_val = best.get("metrics", [{}])[0].get("value") if best.get("metrics") else None
    # Format separately so the f-string doesn't get tangled with a
    # ternary — Python's f-string syntax does not support conditional
    # format specifiers directly.
    best_str = f"{best_val:.3f}" if best_val is not None else "n/a"
    st.info(
        f"📊 Current state: **{len(prior_log)} trials run**, "
        f"best `{primary.metric}` = {best_str},  "
        f"${spent:.2f} of ${mission.total_budget_usd:.2f} spent."
    )

st.divider()


# Controls ---------------------------------------------------------

st.subheader("Start optimization")

st.markdown(
    "Click the button below to start a batch of iterations. Each iteration runs your entire "
    "eval set once against a new variant the optimizer proposes. "
    "You can stop and resume any time."
)

col1, col2 = st.columns([1, 3])
batch_iters = col1.number_input("Iterations to run now", min_value=1, max_value=50, value=8, step=1)

if col2.button(f"▶️ Run {batch_iters} iterations", type="primary"):
    # Live progress containers — we'll write into these as events stream.
    status_box = st.empty()
    metrics_box = st.empty()
    log_table = st.empty()

    events: list[dict] = []

    try:
        from webui.headless_optimizer import run_optimization
        for evt in run_optimization(project_dir, mission, eval_set, max_iters=int(batch_iters)):
            events.append({
                "iter": evt.iteration,
                "phase": evt.phase,
                "arm": evt.arm or "—",
                "metric": f"{evt.primary_metric_value:.3f}" if evt.primary_metric_value is not None else "—",
                "$ spent": f"{evt.budget_spent_usd:.2f}" if evt.budget_spent_usd is not None else "—",
                "message": evt.message,
            })
            status_box.info(f"Iter {evt.iteration} · {evt.phase} · {evt.message}")
            log_table.dataframe(pd.DataFrame(events), use_container_width=True, hide_index=True)
            if evt.terminated_reason:
                status_box.success(f"✅ Run complete — {evt.terminated_reason}")
                break
        else:
            status_box.success("✅ Batch complete.")
    except Exception as e:
        st.error(f"Optimization crashed: {e}")
        st.exception(e)

    # Refresh the prior log + budget after the batch.
    prior_log = read_log(proj_name)
    spent = read_budget(proj_name)
    if prior_log:
        best = max(prior_log, key=lambda t: t.get("metrics", [{}])[0].get("value", -1e9) if t.get("metrics") else -1e9)
        best_val = best.get("metrics", [{}])[0].get("value") if best.get("metrics") else None
        best_str = f"{best_val:.3f}" if best_val is not None else "n/a"
        st.success(
            f"📊 After this batch: {len(prior_log)} trials total, "
            f"best `{primary.metric}` = {best_str},  "
            f"${spent:.2f} of ${mission.total_budget_usd:.2f} spent."
        )


# History ---------------------------------------------------------

if prior_log:
    st.divider()
    st.subheader("Trial history")
    rows = []
    for t in prior_log:
        metrics = t.get("metrics") or []
        primary_metric = metrics[0] if metrics else {}
        rows.append({
            "iter": t.get("iteration", "?"),
            "trial_id": t.get("trial_id", "")[:12],
            "metric": primary_metric.get("name", ""),
            "value": f"{primary_metric.get('value', 0):.4f}",
            "cost_usd": f"{t.get('total_cost_usd', 0):.4f}",
            "p95_ms": f"{t.get('p95_latency_ms', 0):.0f}" if t.get("p95_latency_ms") else "—",
        })
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)


# Finalize button -----------------------------------------------

st.divider()
st.subheader("Finalize")

st.markdown(
    "When you're satisfied with the results, click **Finalize**. The framework will pick the best "
    "variant, write a `FINAL.md` recommendation, and pin the winning configuration so you can chat with it."
)

if st.button("📑 Finalize — produce FINAL.md"):
    try:
        from lib import finalize as fin_mod
        from lib.schemas.state import RunState
        from lib.schemas.trial import TrialResult
        from webui.headless_optimizer import pin_winning_variant

        # Reconstruct TrialResults from the log.
        trials = []
        for row in prior_log:
            trials.append(TrialResult.model_validate(row))

        # Synthesize a RunState — the headless path doesn't write one
        # eagerly, so we synthesize a sensible one based on what's in the log.
        rs = RunState(
            mission_id=mission.mission_id,
            current_iteration=len(trials),
            terminated=True,
            terminated_reason="iteration_cap",
        )
        rec = fin_mod.assemble_recommendation(
            project_dir=project_dir, mission=mission, run_state=rs,
            log=trials, judge_calibration=None,
        )
        fin_path = fin_mod.write_final_md(project_dir, rec, mission)
        pin_winning_variant(project_dir, mission)

        # Persist the bundle for /contribute later.
        bundle = {
            "prompt_patterns": [],
            "rag_recipes": [],
            "failure_modes": [],
            "model_routes": [],
            "judge_templates": [],
        }
        (project_dir / "results" / "knowledge_bundle.json").write_text(
            __import__("json").dumps(bundle, indent=2), encoding="utf-8",
        )

        st.success(f"✅ Finalized. {fin_path}")
        st.markdown(f"**Confidence: `{rec.confidence}`**")
        st.markdown(rec.decision_one_sentence)
        st.info("Head to **📑 Results** to read the full recommendation, or **💬 Chat** to try the winning variant.")
    except Exception as e:
        st.error(f"Finalize failed: {e}")
        st.exception(e)

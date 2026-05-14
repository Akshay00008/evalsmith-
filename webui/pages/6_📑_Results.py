# Results — render FINAL.md plus a structured metrics view.

from __future__ import annotations
from pathlib import Path

import streamlit as st
import pandas as pd

from webui.lib_glue import ensure_lib_on_path, projects_dir, read_log, read_mission, read_budget

ensure_lib_on_path()

st.title("📑 Results")

proj_name = st.session_state.get("active_project")
if not proj_name:
    st.warning("No active project. Pick one on the **📊 Dashboard** first.")
    st.stop()

project_dir = projects_dir() / proj_name
final_path = project_dir / "results" / "FINAL.md"

if not final_path.exists():
    st.info("No `FINAL.md` yet. Go to **🚀 Run**, run some iterations, then click **Finalize**.")
    st.stop()


# Render FINAL.md ------------------------------------------------

st.markdown(final_path.read_text(encoding="utf-8"))

st.divider()


# Show trial chart + table ---------------------------------------

prior_log = read_log(proj_name)
mission = read_mission(proj_name)
primary = mission.primary_criterion() if mission else None

if prior_log:
    st.subheader("Metric over iterations")
    chart_rows = []
    for t in prior_log:
        metrics = t.get("metrics") or []
        if not metrics:
            continue
        chart_rows.append({
            "iter": t.get("iteration", 0),
            "value": metrics[0].get("value", 0),
            "cost_$": t.get("total_cost_usd", 0),
        })
    if chart_rows:
        chart_df = pd.DataFrame(chart_rows).set_index("iter")
        st.line_chart(chart_df["value"])

    st.subheader("All trials")
    table_rows = []
    for t in prior_log:
        metrics = t.get("metrics") or []
        primary_metric = metrics[0] if metrics else {}
        table_rows.append({
            "iter": t.get("iteration", "?"),
            "trial_id": t.get("trial_id", "")[:12],
            "metric": primary_metric.get("name", ""),
            "value": f"{primary_metric.get('value', 0):.4f}",
            "ci_low": f"{primary_metric.get('ci_low', 0):.4f}" if primary_metric.get("ci_low") is not None else "—",
            "ci_high": f"{primary_metric.get('ci_high', 0):.4f}" if primary_metric.get("ci_high") is not None else "—",
            "n_cases": primary_metric.get("n_cases", 0),
            "cost_$": f"{t.get('total_cost_usd', 0):.4f}",
            "p95_ms": f"{t.get('p95_latency_ms', 0):.0f}" if t.get("p95_latency_ms") else "—",
        })
    st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)

st.divider()


# Download buttons --------------------------------------------

st.subheader("Download artifacts")
col1, col2, col3 = st.columns(3)

col1.download_button(
    "⬇️ FINAL.md",
    final_path.read_text(encoding="utf-8"),
    file_name=f"{proj_name}_FINAL.md",
    mime="text/markdown",
)

log_path = project_dir / "experiment_log.jsonl"
if log_path.exists():
    col2.download_button(
        "⬇️ experiment_log.jsonl",
        log_path.read_text(encoding="utf-8"),
        file_name=f"{proj_name}_experiment_log.jsonl",
        mime="application/jsonl",
    )

mission_path = project_dir / "MISSION.json"
if mission_path.exists():
    col3.download_button(
        "⬇️ MISSION.json",
        mission_path.read_text(encoding="utf-8"),
        file_name=f"{proj_name}_MISSION.json",
        mime="application/json",
    )

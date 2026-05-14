# Dashboard — project listing + selector.
# Lets the user see all projects + their current state, and pick one to
# "make active" for downstream pages. No write actions on this page.

from __future__ import annotations

import sys
from pathlib import Path
# Streamlit runs each page directly, so the project root isn't on sys.path
# automatically. We inject it so the webui imports resolve.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
import streamlit as st
import pandas as pd
from datetime import datetime

from webui.lib_glue import ensure_lib_on_path, list_projects, read_mission, read_budget

ensure_lib_on_path()

st.title("📊 Dashboard")
st.markdown("All your evalsmith projects at a glance. Click **Make active** on the one you want to work on.")

projects = list_projects()

if not projects:
    st.warning("No projects yet. Use **🆕 New Project** in the sidebar to create one.")
    st.stop()

# Build a pandas frame for the summary table. Each row gets formatted
# nicely — we hide raw timestamps and show status badges instead.
rows = []
for p in projects:
    created = (
        datetime.fromtimestamp(p["created_at"]).strftime("%Y-%m-%d %H:%M")
        if p["created_at"]
        else "—"
    )
    if p["finalized"]:
        badge = "✅ Finalized"
    elif p["has_log"]:
        badge = f"🚀 In progress ({p['n_trials']} trials)"
    elif p["has_mission"]:
        badge = "🎯 Mission locked"
    else:
        badge = "🆕 Awaiting setup"

    spent = read_budget(p["name"])
    rows.append({
        "Project": p["name"],
        "Status": badge,
        "Trials": p["n_trials"],
        "Spent ($)": f"{spent:.2f}",
        "Created": created,
    })

df = pd.DataFrame(rows)
st.dataframe(df, use_container_width=True, hide_index=True)

st.divider()
st.subheader("Select active project")

# Use a radio with the list of project names so the choice is obvious.
# Each picked project goes into session_state and persists across pages.
project_names = [p["name"] for p in projects]
current = st.session_state.get("active_project")
default_index = project_names.index(current) if current in project_names else 0

picked = st.radio(
    "Which project would you like to work on?",
    project_names,
    index=default_index,
    horizontal=True,
)

if st.button("✅ Make active", type="primary"):
    st.session_state["active_project"] = picked
    st.success(f"Active project is now **{picked}**. Use the sidebar to navigate to the next step.")
    st.rerun()

# Show details for the currently-selected project below.
if st.session_state.get("active_project"):
    proj_name = st.session_state["active_project"]
    st.divider()
    st.subheader(f"Details — {proj_name}")

    mission = read_mission(proj_name)
    proj_info = next((p for p in projects if p["name"] == proj_name), None)

    cols = st.columns(3)
    if mission:
        cols[0].metric("Task type", mission.composition.task_modality)
        cols[1].metric("Domain", mission.domain)
        primary = mission.primary_criterion()
        cols[2].metric(
            "Goal",
            f"{primary.metric} {primary.operator} {primary.target}",
            help=f"Operational floor: {primary.operational_floor}",
        )
    else:
        st.info("This project hasn't had its Mission set yet. Go to **🎯 Mission** to lock it.")

    if proj_info:
        st.markdown(
            f"**Trials run:** {proj_info['n_trials']}    "
            f"**Spent:** ${read_budget(proj_name):.2f}    "
            f"**Finalized:** {'✅' if proj_info['finalized'] else '—'}"
        )

# webui/app.py
# Streamlit entry point. The other pages live under `pages/` and are
# auto-discovered by Streamlit (sorted by their numeric prefix).
#
# Run with:  streamlit run webui/app.py
#
# Design philosophy: every page assumes the user is non-technical. No raw
# JSON, no CLI commands, no Claude Code knowledge required. Each page has
# a clear "what is this screen for" header and forms the user fills in.

from __future__ import annotations
import sys
from pathlib import Path

# Streamlit runs this file directly, so the project root isn't on sys.path
# automatically. We inject it so `from webui.lib_glue import ...` resolves.
# Must happen BEFORE the webui import.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Auto-load .env from repo root so ANTHROPIC_API_KEY is picked up when
# running `streamlit run webui/app.py` without manually exporting vars.
try:
    from dotenv import load_dotenv as _load_dotenv  # type: ignore
    _load_dotenv(_PROJECT_ROOT / ".env", override=False)
except ImportError:
    pass  # python-dotenv not installed — rely on shell env or system vars

import streamlit as st

from webui.lib_glue import ensure_lib_on_path, list_projects

ensure_lib_on_path()

st.set_page_config(
    page_title="evalsmith",
    page_icon="🛠",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Persist the active project name across pages via session_state. Pages
# read this to know which project to operate on. Defaults to None — the
# Dashboard prompts the user to select or create one.
if "active_project" not in st.session_state:
    st.session_state["active_project"] = None


st.title("🛠 evalsmith")
st.markdown(
    """
    **A guided way to optimize and ship GenAI applications** — RAG systems,
    chatbots, NL-to-SQL, document insight extractors, and more.

    Use the sidebar on the left to walk through the steps:

    1. **📊 Dashboard** — see all your projects and what state they're in
    2. **🆕 New Project** — start a project with a guided wizard
    3. **📁 Upload Data** — add your documents, eval questions, or database connection
    4. **🎯 Mission** — review and lock the optimization goals
    5. **🚀 Run** — start the optimization, watch progress live
    6. **📑 Results** — read the final recommendation
    7. **💬 Chat** — talk to your optimized assistant before shipping

    > **No code required.** Everything you need is buttons and forms.
    """
)

st.divider()

# Quick stats on the home page — gives the user a sense of activity.
projects = list_projects()
if projects:
    finalized = sum(1 for p in projects if p["finalized"])
    running = sum(1 for p in projects if p["has_log"] and not p["finalized"])
    created = sum(1 for p in projects if not p["has_log"])

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total projects", len(projects))
    col2.metric("Finalized", finalized)
    col3.metric("In progress", running)
    col4.metric("Awaiting setup", created)
else:
    st.info(
        "No projects yet. Head to **🆕 New Project** in the sidebar to create your first one."
    )

st.divider()

# Active project indicator. Once a user picks a project on the Dashboard,
# every other page operates on it implicitly. This keeps the per-page UX
# tight — no "which project?" picker on every screen.
if st.session_state["active_project"]:
    st.success(f"📌 Active project: **{st.session_state['active_project']}**")
else:
    st.caption("No active project selected. Pick one from the Dashboard, or create a new one.")

# Footer with help link.
st.markdown(
    "---\n"
    "Need help? See the [walkthrough guide](https://github.com/AkshayDat/evalsmith-/blob/main/docs/NONTECH_GUIDE.md) or contact the framework maintainer."
)

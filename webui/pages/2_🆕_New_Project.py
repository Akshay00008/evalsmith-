# New Project — guided wizard. Replaces `genai new-project --recipe X`.
# Asks the user three plain-English questions, then scaffolds the project.

from __future__ import annotations
from pathlib import Path
import shutil
import json
import time
import re

import streamlit as st

from webui.lib_glue import ensure_lib_on_path, projects_dir, framework_root, read_recipes

ensure_lib_on_path()

st.title("🆕 New Project")
st.markdown(
    "Set up a new evalsmith project in three quick steps. "
    "Don't worry about getting everything perfect — you can adjust later."
)

# Modality cards — each maps to a recipe + an explanation a non-tech
# user can understand. The recipe field names are exact matches with
# the recipe JSON files under recipes/.
MODALITY_OPTIONS = {
    "Question answering over my documents (RAG)":   "rag_qa",
    "Natural language → SQL query":                 "nlq_sql",
    "Multi-step research with citations":           "research_citation",
    "Extract structured info from documents":       "insight_extraction",
    "Search / rank documents":                      "search_engine",
    "Customer support chatbot":                     "chatbot_support",
}

# Domain hints aligned with lib/domains/*.
DOMAIN_OPTIONS = ["general", "support_bot", "code_assistant", "search_qa", "extraction"]


# Step 1 -------------------------------------------------------------

st.subheader("Step 1 — Name your project")
project_name = st.text_input(
    "Project name",
    placeholder="e.g. internal_docs_qa, contract_extractor, support_bot_v2",
    help="Letters, numbers, underscores. No spaces.",
)

# Sanity check the name so we don't end up with weird filesystem paths.
def _valid_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{2,40}", name or ""))


# Step 2 -------------------------------------------------------------

st.subheader("Step 2 — What kind of project is this?")
modality_label = st.selectbox(
    "Pick the option that best describes what you're building:",
    list(MODALITY_OPTIONS.keys()),
    help="Each option is a *recipe* — a pre-configured starting point for the optimizer.",
)
selected_recipe = MODALITY_OPTIONS[modality_label]

# Pull the recipe metadata to show the user what defaults they'd be getting.
recipes = read_recipes()
recipe_data = recipes.get(selected_recipe, {})
if recipe_data:
    with st.expander("ℹ️ What does this recipe configure?"):
        st.write(recipe_data.get("description", "(no description)"))
        crit = recipe_data.get("success_criteria", [])
        if crit:
            primary = next((c for c in crit if c.get("is_primary")), crit[0])
            st.markdown(
                f"**Goal:** `{primary['metric']} {primary['operator']} {primary['target']}` "
                f"(floor: `{primary['operational_floor']}`)"
            )

# Step 3 -------------------------------------------------------------

st.subheader("Step 3 — Pick a domain hint (optional)")
domain_choice = st.selectbox(
    "Choose the domain that best matches your use case:",
    DOMAIN_OPTIONS,
    help="This injects domain-specific best practices into the optimizer. 'general' is fine if nothing else fits.",
)


# Submit ------------------------------------------------------------

st.divider()

ready = bool(project_name and _valid_name(project_name) and selected_recipe)
if project_name and not _valid_name(project_name):
    st.error("Project name must be 2–40 characters, letters/numbers/underscore/dash only.")

if st.button("Create project", disabled=not ready, type="primary"):
    target = projects_dir() / project_name
    if target.exists():
        st.error(f"Project '{project_name}' already exists. Pick a different name or delete it from disk first.")
    else:
        template = projects_dir() / ".templates" / "_project_template"
        if not template.exists():
            st.error(f"Project template missing at {template}. Re-clone the repo.")
        else:
            shutil.copytree(template, target)
            # Stamp PROJECT.json with the chosen name + creation timestamp.
            proj_meta = json.loads((target / "PROJECT.json").read_text(encoding="utf-8"))
            proj_meta["name"] = project_name
            proj_meta["created_at_unix"] = time.time()
            proj_meta["framework_version"] = "0.1.0"
            (target / "PROJECT.json").write_text(json.dumps(proj_meta, indent=2), encoding="utf-8")

            # Drop in the recipe and stash the chosen domain so the
            # Mission page can use it as a default.
            recipe_path = framework_root() / "recipes" / f"{selected_recipe}.json"
            if recipe_path.exists():
                shutil.copy(recipe_path, target / "recipe.json")
            (target / "domain_hint.txt").write_text(domain_choice, encoding="utf-8")

            st.session_state["active_project"] = project_name
            st.success(f"✅ Created project '{project_name}'. Next: go to **📁 Upload Data**.")
            st.balloons()

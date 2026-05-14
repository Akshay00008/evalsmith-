# webui/__init__.py
# The web UI surface — Streamlit pages + headless optimizer. Designed
# for non-technical users (PMs, BAs, analysts) who shouldn't have to
# touch the CLI, Claude Code, or any JSON file directly.
#
# Dependency direction: webui/ imports from lib/, never the reverse.
# The framework is fully usable without the UI.

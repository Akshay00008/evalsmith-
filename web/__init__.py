# web/__init__.py
# FastAPI + HTMX web frontend. The Docker-deployable alternative to the
# Streamlit UI under webui/. Designed for the same audience (non-technical
# users — PMs, BAs) but production-ready:
#   * Single Python process (uvicorn) — one port, one container.
#   * Server-rendered Jinja2 templates + HTMX for interactivity (no JS build).
#   * Bootstrap 5 via CDN for styling — zero CSS toolchain.
#   * Live optimization progress via Server-Sent Events.
#
# Run locally:    uvicorn web.api:app --reload
# Run in Docker:  docker compose up

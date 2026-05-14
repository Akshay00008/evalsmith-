# Dockerfile
# Container image for the evalsmith FastAPI web frontend.
#
# Build:    docker build -t evalsmith:latest .
# Run:      docker run -p 8000:8000 -v $(pwd)/projects:/app/projects evalsmith:latest
# Compose:  docker compose up
#
# Image philosophy:
#   * Single stage — simplicity > size for a personal-dev tool.
#   * python:3.11-slim base — modern Python + small footprint.
#   * Non-root user — defensive even though this is a single-user app.
#   * /app/projects is the only mutable path — mount it as a volume for
#     state to survive container restarts.

FROM python:3.11-slim

# Don't write .pyc files and don't buffer stdout — both help when running
# under docker (no .pyc clutter on host volumes, logs flush to journald).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# OS packages: just the SSL libs and gcc that some Python wheels need.
# We keep this minimal — anything we install costs image size.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gcc \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user. Even single-user apps shouldn't run as root in
# a container — limits blast radius if anything goes sideways.
RUN useradd --create-home --shell /bin/bash app
WORKDIR /app

# Install Python deps in their own layer for cache friendliness — we copy
# the requirements file first so a code edit doesn't bust the install layer.
COPY requirements-full.txt /app/requirements-full.txt
RUN pip install --upgrade pip && pip install -r requirements-full.txt

# Copy the rest of the project.
COPY . /app

# Install the package itself in editable mode so `lib.*` is importable
# without sys.path gymnastics inside the container.
RUN pip install -e .

# Ownership + permissions.
RUN chown -R app:app /app
USER app

# Health check via the /healthz endpoint we wired up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

EXPOSE 8000

# Use uvicorn directly (no shell exec form) so signals propagate cleanly
# and the container can be stopped with Ctrl-C / `docker stop`.
CMD ["uvicorn", "web.api:app", "--host", "0.0.0.0", "--port", "8000"]

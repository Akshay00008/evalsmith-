# Docker Deployment Guide — evalsmith web UI

How to run the evalsmith web app on any machine that has Docker — locally, on a teammate's laptop, on a VPS, or behind a reverse proxy.

The web app (under `web/`) is a single FastAPI + Jinja2 + HTMX server. One container, one port, no JavaScript build step.

> **Related guides**
> - [WALKTHROUGH.md](WALKTHROUGH.md) — the general pipeline tour.
> - [NONTECH_GUIDE.md](NONTECH_GUIDE.md) — screen-by-screen walkthrough for non-tech users.

---

## TL;DR

```bash
git clone https://github.com/Akshay00008/evalsmith-.git
cd evalsmith-
docker compose up --build
# Open http://localhost:8000
```

That's it. Press `Ctrl+C` to stop, `docker compose down` to remove.

To set an LLM API key:
```bash
export ANTHROPIC_API_KEY=sk-ant-...
docker compose up
```

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [First run (local)](#2-first-run-local)
3. [What's actually running](#3-whats-actually-running)
4. [Data persistence](#4-data-persistence)
5. [Setting API keys](#5-setting-api-keys)
6. [Production deployment notes](#6-production-deployment-notes)
7. [Troubleshooting](#7-troubleshooting)
8. [Local dev workflow](#8-local-dev-workflow)

---

## 1. Prerequisites

| Requirement                              | Install link / check                           |
|-------------------------------------------|-----------------------------------------------|
| **Docker Desktop** (macOS / Windows)     | https://www.docker.com/products/docker-desktop |
| **Docker Engine + Compose** (Linux)      | https://docs.docker.com/engine/install/        |
| Verify install                            | `docker --version` and `docker compose version` |

That's all. Python is **not** needed on the host — it runs inside the container.

---

## 2. First run (local)

```bash
git clone https://github.com/Akshay00008/evalsmith-.git
cd evalsmith-

# Build the image and start the container.
# First run downloads the Python 3.11 base image (~50MB) + pip-installs all deps
# (~500MB). Subsequent runs are <5s (image cached, deps cached, only your code copies).
docker compose up --build
```

Open **http://localhost:8000** in your browser. You should see the evalsmith dashboard.

To stop:
- Press `Ctrl+C` in the terminal where `docker compose up` is running, OR
- From any terminal: `docker compose down`

To re-run later:
```bash
cd evalsmith-
docker compose up
# No --build needed unless you changed Dockerfile / requirements
```

---

## 3. What's actually running

```
┌──────────────────────────────────────────────────────────────────┐
│  Container: evalsmith                                            │
│  Base: python:3.11-slim                                          │
│  Port: 8000 (mapped to host :8000)                               │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  Process: uvicorn web.api:app                            │    │
│  │  ├─ GET  /                  → dashboard.html             │    │
│  │  ├─ GET  /projects/new      → new_project.html           │    │
│  │  ├─ POST /projects/new      → creates project            │    │
│  │  ├─ GET  /projects/{name}   → project_home.html          │    │
│  │  ├─ POST /upload/eval       → save eval set              │    │
│  │  ├─ POST /upload/pdfs       → save PDFs                  │    │
│  │  ├─ POST /upload/ingest     → run pypdf chunking         │    │
│  │  ├─ POST /upload/db         → save DB config + test      │    │
│  │  ├─ POST /mission           → lock MISSION.json          │    │
│  │  ├─ POST /run/start         → SSE stream of progress     │    │
│  │  ├─ POST /run/finalize      → write FINAL.md             │    │
│  │  ├─ GET  /results           → results.html (Chart.js)    │    │
│  │  ├─ GET  /chat              → chat.html                  │    │
│  │  └─ POST /chat              → next message (HTMX)        │    │
│  └──────────────────────────────────────────────────────────┘    │
│                                                                  │
│  Volumes (bind-mounted from host):                               │
│  ├─ ./projects   ─→ /app/projects   (project workspaces)         │
│  └─ ./knowledge  ─→ /app/knowledge  (shared knowledge library)   │
└──────────────────────────────────────────────────────────────────┘
```

**No external services.** No database, no cache, no message queue. The container is self-contained.

---

## 4. Data persistence

Two host folders are bind-mounted into the container:

| Host path           | Container path        | What lives there                                            |
|---------------------|----------------------|-------------------------------------------------------------|
| `./projects/`       | `/app/projects/`      | Each project's workspace — MISSION, eval set, trial log, FINAL.md, etc. |
| `./knowledge/`      | `/app/knowledge/`     | Cross-project learnings (prompt patterns, RAG recipes, etc.) |

These survive `docker compose down` and `docker compose down -v` alike. To wipe a project, just delete its folder under `./projects/` on the host.

> ⚠️ **`data/db.json` is gitignored at the project-template level.** If you store DB credentials there, they're on the host filesystem only (never in any container image or git repo). For production, prefer environment variables or a secret-manager mount.

---

## 5. Setting API keys

The Anthropic / OpenAI keys are picked up from the **host shell** at `docker compose up` time:

### Option A — Shell env vars
```bash
export ANTHROPIC_API_KEY=sk-ant-...
docker compose up
```

### Option B — `.env` file at repo root
Create `evalsmith-/.env`:
```
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
```
Compose auto-loads `.env`. The file is gitignored.

### Without any keys
The framework falls back to **deterministic stub mode** — every model call returns a placeholder. Useful for exploring the UI without burning API budget.

---

## 6. Production deployment notes

This setup is fine for **one user, one machine**. For multi-user / public deployments:

### Reverse proxy + HTTPS

Run behind nginx / Caddy / Traefik. Minimal Caddy example:

```
your-domain.com {
    reverse_proxy localhost:8000
}
```

Caddy handles TLS automatically. nginx with certbot works similarly.

### Authentication

The current app has **no auth**. Anyone who reaches `:8000` can use it. For shared / public deployment:
- Front it with an auth proxy (oauth2-proxy, Cloudflare Access, Tailscale).
- Or add HTTP Basic Auth in nginx/Caddy as a quick gate.

### Resource limits

The headless optimizer can drive several iterations × N eval cases per minute. For shared boxes, cap container resources in `docker-compose.yml`:

```yaml
services:
  evalsmith:
    deploy:
      resources:
        limits:
          cpus: '2.0'
          memory: 4G
```

### Persistent storage on a remote host

Bind-mounts (`./projects`) work for single-host. For a real deploy:
- Use a Docker named volume + back it up with `docker run --rm -v evalsmith_projects:/data -v $(pwd):/backup busybox tar czf /backup/projects.tar.gz /data`.
- Or mount NFS / EFS / a managed-block-storage volume.

### Updates

```bash
git pull
docker compose up --build -d
```

The `projects/` volume is unaffected — only the image is rebuilt.

### Multi-user concerns

The current chat REPL uses an in-process dict (`_CHAT_BUFFERS`) keyed by project name. That works for single-user. Multi-user requires session-keying (cookie + Redis or similar). Open an issue if you need this.

---

## 7. Troubleshooting

### `docker compose: command not found`
You have Docker but not Compose v2. Either:
- Install Compose v2 plugin: https://docs.docker.com/compose/install/
- Or use the legacy `docker-compose` (with hyphen) if Compose v1 is installed.

### `port 8000 already in use`
Edit `docker-compose.yml` and change the `ports:` mapping to a free host port, e.g. `"8080:8000"`. Then open `http://localhost:8080`.

### Container starts but `localhost:8000` shows "connection refused"
Check the logs: `docker compose logs evalsmith`. If you see a Python traceback, it's an app-level issue — file an issue with the traceback. If you see `Application startup complete`, your host firewall is blocking — try `http://127.0.0.1:8000` explicitly.

### "Repository not found" during `git clone`
The repo is private. The maintainer needs to invite you as a collaborator on GitHub.

### Build takes 5+ minutes
The slow step is `pip install` for the ~500MB of full deps. To speed up subsequent builds, the layer is cached as long as `requirements-full.txt` doesn't change. Avoid editing it between builds.

### Image is huge (>2GB)
You probably pulled in `chromadb` + `sentence-transformers` (both heavy). For a lean image, comment out those lines in `requirements-full.txt` and rebuild — the web UI doesn't need them unless you're doing dense retrieval.

### Data disappears after `docker compose down -v`
The `-v` flag removes **named volumes**, but our `projects/` and `knowledge/` are **bind-mounts** (host folders), so they survive `-v` too. If you're seeing data loss, you might be on a system where Docker Desktop has bind-mount issues — try `docker compose down` (without `-v`) and check `./projects/` on the host.

### `docker build` fails with `cannot find user 'app'`
This is a Linux-only quirk — the `useradd` step in the Dockerfile failed. Usually means your Docker daemon is old. Update Docker to 20.10+.

---

## 8. Local dev workflow

If you're iterating on the app code itself (not just using it):

```bash
# Install web deps locally (host Python)
pip install -e ".[web,llm,rag,db]"

# Run with hot reload
uvicorn web.api:app --reload --host 0.0.0.0 --port 8000
```

Edit any file in `web/` or `lib/` and uvicorn reloads automatically. Faster than Docker for the inner loop.

Use Docker for:
- Sharing with teammates ("just `docker compose up`")
- Deploying to a server
- Reproducible builds for CI

Use bare uvicorn for:
- Active development
- Quick smoke tests of the framework's Python code

Both run the same `web/api.py`. The container is just the deployment shell.

---

## What's next?

After getting the container running:

- Open the dashboard, click **New project**, fill the wizard.
- Upload an eval set (CSV / Excel / JSONL — see [NONTECH_GUIDE.md § Upload Data](NONTECH_GUIDE.md#screen-2--📁-upload-data) for the schema).
- Lock the Mission, hit Run, watch the live progress, click Finalize.
- Chat with the winner via the Chat tab to sanity-check before shipping.

For the full screen-by-screen walkthrough see [NONTECH_GUIDE.md](NONTECH_GUIDE.md).

# Multi-stage build for Haku's UI service.
#   stage 1 (node):   build the React SPA → /app/frontend/dist
#   stage 2 (python): install the FastAPI backend + copy the built SPA in
# The image runs uvicorn on :8080 as a non-root user. Built by Forgejo CI
# (see ../.forgejo/workflows/build-ui.yaml) — NOT by ducktape's Bazel/BuildBuddy.

# --- stage 1: build the SPA -------------------------------------------------
FROM node:22-slim AS frontend
WORKDIR /app/frontend
# Install deps from the lockfile first (cache-friendly), then build.
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- stage 2: python runtime ------------------------------------------------
FROM python:3.13-slim AS runtime
# pygit2 needs libgit2 at runtime; build wheels may need the dev headers.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgit2-1.5 git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
# The built SPA is served same-origin by the backend (HAKU_UI_STATIC_DIR).
COPY --from=frontend /app/frontend/dist ./web

# Run as a non-root user; /data holds the haku-state clone (writable).
RUN useradd --create-home --uid 10001 haku \
    && mkdir -p /data \
    && chown -R haku:haku /app /data
USER haku

ENV HAKU_UI_STATIC_DIR=/app/web \
    HAKU_UI_CLONE_DIR=/data/haku-state \
    PYTHONUNBUFFERED=1

EXPOSE 8080
WORKDIR /app/backend
CMD ["python", "app.py"]

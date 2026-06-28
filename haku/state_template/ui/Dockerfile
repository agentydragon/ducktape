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
# Pure-Python deps (httpx talks to the in-cluster plaintext-HTTP Forgejo API), so no
# system libs / git needed and no writable volume — the backend keeps no local clone.
FROM python:3.13-slim AS runtime
WORKDIR /app
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
# The built SPA is served same-origin by the backend (HAKU_UI_STATIC_DIR).
COPY --from=frontend /app/frontend/dist ./web

# Run as a non-root user. Use the numeric UID (not the name): Kubernetes
# runAsNonRoot can only verify a non-root user from a numeric USER.
RUN useradd --create-home --uid 10001 haku && chown -R haku:haku /app
USER 10001

ENV HAKU_UI_STATIC_DIR=/app/web \
    PYTHONUNBUFFERED=1

EXPOSE 8080
WORKDIR /app/backend
CMD ["python", "app.py"]

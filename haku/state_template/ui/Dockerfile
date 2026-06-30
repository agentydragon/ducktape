# Multi-stage build for Haku's UI service.
#   stage 1 (frontend):       build the React SPA → /app/frontend/dist (tsc typecheck + vite)
#   stage 1b (frontend-test): RUN vitest over the SPA's pure logic — a CI gate
#   stage 2 (backend-base):   install the FastAPI backend + its deps
#   stage 3 (test):           install dev deps and RUN pytest — a CI gate
#   stage 4 (runtime):        backend-base + the built SPA, runs uvicorn as non-root
# Built by Forgejo CI (see ../.forgejo/workflows/build-ui.yaml) — NOT by ducktape's
# Bazel/BuildBuddy. CI builds the `test` + `frontend-test` targets first and only pushes
# if they pass.

# --- stage 1: build the SPA -------------------------------------------------
FROM node:22-slim AS frontend
WORKDIR /app/frontend
# Install deps from the lockfile first (cache-friendly), then build.
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- stage 1b: frontend tests (a CI gate) -----------------------------------
# Vitest over the SPA's pure logic (e.g. garden link resolution). FROM frontend so the
# deps (incl. the vitest dev dep from `npm ci`) and source are already present; a failing
# test fails `docker build --target frontend-test`, which CI runs before any push.
FROM frontend AS frontend-test
RUN npm run test

# --- stage 2: python base (shared by `test` and `runtime`) ------------------
# Pure-Python deps (httpx talks to the in-cluster plaintext-HTTP Forgejo API), so no
# system libs / git needed and no writable volume — the backend keeps no local clone.
FROM python:3.13-slim AS backend-base
WORKDIR /app
# `pip install .` needs pyproject.toml + the flat modules it declares, so copy the
# backend dir first, then install. Source changes invalidate the dep-install layer, but
# the backend is tiny and rebuilt rarely.
COPY backend/ ./backend/
RUN pip install --no-cache-dir ./backend

# --- stage 3: backend tests (the CI gate) -----------------------------------
# CI builds this target FIRST (`docker build --target test`). The `RUN pytest` fails the
# build if any test fails, so the build/push job — which `needs:` the test job — never
# runs and no image is pushed or tagged. Test-only deps stay here, never in `runtime`.
FROM backend-base AS test
RUN pip install --no-cache-dir ./backend[dev]
WORKDIR /app/backend
RUN python -m pytest -q

# --- stage 4: python runtime ------------------------------------------------
FROM backend-base AS runtime
# The git commit this image is built from, passed by CI (--build-arg GIT_SHA=${{ github.sha }}).
# Surfaced in the UI footer as a Forgejo commit link. Empty in a plain local build.
ARG GIT_SHA=""
# Standard OCI provenance: point the image at its source repo. This is *also* the label
# Forgejo/Gitea use to auto-link a container package to a repo — BUT this Forgejo (15) did
# NOT auto-link from it (verified 2026-06-29: label present + repo URL exact-match, package
# still unlinked). The repo↔package link is therefore maintained out-of-band (it's durable
# once set; the repo-scoped `package` webhook only fires while linked — see
# improvements.yaml:flux-image-webhook-not-firing and tf/gitops/haku-state). Keep the label
# regardless — it's correct provenance and would auto-link on a Forgejo that honors it.
LABEL org.opencontainers.image.source="https://git.allegedly.works/haku/haku-state" \
      org.opencontainers.image.revision="$GIT_SHA"
# The built SPA is served same-origin by the backend (HAKU_UI_STATIC_DIR).
COPY --from=frontend /app/frontend/dist ./web

# Run as a non-root user.
RUN useradd --create-home --uid 10001 haku && chown -R haku:haku /app
USER 10001

ENV HAKU_UI_STATIC_DIR=/app/web \
    PYTHONUNBUFFERED=1 \
    HAKU_UI_GIT_SHA=$GIT_SHA

EXPOSE 8080
WORKDIR /app/backend
CMD ["python", "app.py"]

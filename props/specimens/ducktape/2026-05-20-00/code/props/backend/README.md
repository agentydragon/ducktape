# Props Dashboard Backend

FastAPI backend for the props evaluation dashboard. API at `http://localhost:8000`.

## Quick Start

```bash
cd props && docker compose up -d       # Start infrastructure
bazelisk run //props/frontend:dev      # Frontend + backend dev servers
```

## API

- `GET /health` — health check
- `GET /api/stats/overview` — definitions leaderboard
- Full endpoint list: see <../docs/SPEC.md> and <../docs/backend_api.md>

## Structure

```
backend/
├── app.py         # FastAPI app, lifespan
├── auth.py        # Auth middleware (postgres creds → RLS)
├── routes/        # API endpoints (stats, runs, ground_truth, llm, registry)
└── TODO.md        # Implementation tasks
```

## Development

```bash
bazel build //props/frontend:bundle    # Regenerate API types after schema changes
```

Imports from `props.core` (agent registry), `props.db` (ORM, config). Frontend in `../frontend/`.

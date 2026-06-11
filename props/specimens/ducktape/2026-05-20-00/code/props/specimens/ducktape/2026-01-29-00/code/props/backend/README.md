# Props Dashboard Backend

FastAPI backend for the props training/evaluation dashboard.

## Quick Start

```bash
# Start infrastructure (from props/)
cd props && docker compose up -d

# Run frontend + backend dev servers with watch
bazelisk run //props/frontend:dev
```

The API will be available at `http://localhost:8000`.

## API Endpoints

- `GET /health` - Health check
- `GET /api/stats/overview` - Main dashboard data (definitions leaderboard)

## Project Structure

```
backend/
├── __init__.py          # Package root
├── app.py               # FastAPI app, lifespan
├── routes/
│   ├── runs.py          # Runs API + WebSocket
│   └── stats.py         # Stats API
├── TODO.md              # Implementation tasks
├── SPEC.md              # Feature specification
└── AGENTS.md            # Agent instructions
```

Frontend lives in `../frontend/`.

## Development

Requires the `props` package (workspace member) for database access.

```bash
# Start infrastructure (from props/)
cd props && docker compose up -d

# Run frontend + backend dev servers
bazelisk run //props/frontend:dev

# Regenerate API types after schema changes
bazel build //props/frontend:bundle
```

## Key Dependencies

- **Backend:** FastAPI, SQLAlchemy, props.db, props.core.agent_registry
- **Frontend:** Svelte 5, Tailwind, openapi-fetch

## Props Integration

Backend imports from `props.core` package:

- `props.core.agent_registry.AgentRegistry` - Run critic/grader agents
- `props.db.models` - ORM models, views
- `props.db.config` - Database connection

Shared database is managed by Docker Compose (see `props/compose.yaml`).

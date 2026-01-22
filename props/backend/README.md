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

### Stats API (`/api/stats`)
- `GET /api/stats/overview` - Main dashboard data (definitions leaderboard)
- `GET /api/stats/definitions` - List all agent definitions
- `GET /api/stats/definitions/{image_digest}` - Get specific definition stats
- `GET /api/stats/examples` - List training examples

### Runs API (`/api/runs`)
- `GET /api/runs/active` - List active agent runs
- `GET /api/runs/jobs` - List agent jobs
- `GET /api/runs` - List all runs
- `POST /api/runs/validation` - Submit validation run
- `GET /api/runs/run/{run_id}` - Get run details
- `WS /api/runs/run/{run_id}/stream` - Stream run events (WebSocket)
- `WS /api/runs/feed` - Subscribe to run updates (WebSocket)

### Ground Truth API (`/api/gt`)
- `GET /api/gt/snapshots` - List snapshots
- `GET /api/gt/snapshots/{snapshot_slug}` - Get snapshot details
- `GET /api/gt/snapshots/{snapshot_slug}/tree` - Get snapshot file tree
- `GET /api/gt/snapshots/{snapshot_slug}/files/{file_path}` - Get file contents

### Health
- `GET /health` - Health check

## Project Structure

```
backend/
├── __init__.py          # Package root
├── app.py               # FastAPI app, lifespan
├── routes/
│   ├── runs.py          # Runs API + WebSocket
│   ├── stats.py         # Stats API
│   └── ground_truth.py  # Ground truth/snapshot API
├── TODO.md              # Implementation tasks
├── SPEC.md              # Feature specification
└── AGENTS.md            # Agent instructions
```

Frontend lives in `../frontend/`.

## Development

Requires the `props` package (workspace member) for database access.

**Required Environment Variables:**
- `PROPS_GRADER_MODEL` - Model to use for grading (e.g., `gpt-4o`)

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

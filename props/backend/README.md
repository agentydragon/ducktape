# Props Dashboard Backend

FastAPI backend for the props evaluation dashboard. API at `http://localhost:8000`.

## Quick Start

```bash
cd props && docker compose up -d       # Start infrastructure
bazelisk run //props/frontend:dev      # Frontend + backend dev servers
```

## API

See <../docs/backend_api.md> for backend endpoint and agent-workflow details.

## Development

```bash
bazel build //props/frontend:bundle    # Regenerate API types after schema changes
```

Imports from `props.core` (agent registry), `props.db` (ORM, config). Frontend in `../frontend/`.

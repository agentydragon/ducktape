# Props Ecosystem

High-level architecture and shared infrastructure for the props evaluation system.

## Directory Structure

```
props/
├── .envrc                    # Devenv entry point for env vars
├── devenv.nix                # Devenv config: sets PG* env vars for Docker Compose access
├── compose.yaml              # Docker Compose for postgres, registry, proxy
├── core/                     # Core Python library (props_core)
│   ├── pyproject.toml        # Package: props-core
│   ├── src/props_core/       # The Python package
│   └── tests/                # Tests for props_core
├── backend/                  # FastAPI dashboard backend
│   ├── __init__.py           # Python package root
│   ├── routes/               # API endpoints
│   └── tests/                # Tests for props.backend
└── frontend/                 # Svelte UI
    ├── package.json
    └── src/
```

## Initial Setup

### Prerequisites

- Specimens repository cloned at `../specimens` (relative to ducktape root):
  `git clone https://github.com/agentydragon/specimens ../specimens`

### First-Time Setup

```bash
cd props

# 1. Allow direnv (generates PGPASSWORD, sets env vars)
direnv allow

# 2. Build and load proxy image
bazelisk run //props/registry_proxy:load

# 3. Start infrastructure
docker compose up -d

# 4. Initialize database (runs migrations, syncs specimens)
bazelisk run //props/core/cli -- db recreate

# 5. Push agent images to registry
bazelisk run //props/core/agent_defs/critic:push
bazelisk run //props/core/agent_defs/grader:push
bazelisk run //props/core/agent_defs/improvement:push
bazelisk run //props/core/agent_defs/prompt_optimizer:push
```

## Development

**Build system:** Bazel (see root AGENTS.md).

```bash
docker compose up -d                       # Start infrastructure
docker compose down                        # Stop infrastructure
docker compose logs -f postgres            # View logs
bazelisk run //props/frontend:dev          # Frontend + backend with watch
bazelisk test //props/...                  # Run all tests
bazelisk build --config=check //props/...  # Lint + typecheck
```

### Service URLs

- Frontend: <http://localhost:5173>
- Backend: <http://localhost:8000>
- PostgreSQL: localhost:5433
- Registry: localhost:5050 (direct), localhost:5051 (proxy with ACL)

## Database Management

```bash
# psql access (uses PG* environment variables from devenv)
psql

# Recreate database from scratch (drops all data, runs migrations, syncs specimens)
bazelisk run //props/core/cli -- db recreate

# Backup and restore
bazelisk run //props/core/cli -- db backup
bazelisk run //props/core/cli -- db restore <backup_file>
```

## Specimens Dataset

**Specimens data lives in a separate repository**: <https://github.com/agentydragon/specimens>

The `ADGN_PROPS_SPECIMENS_ROOT` environment variable points to the specimens repo (typically `~/code/specimens`).
The props package loads specimen data from this external location.

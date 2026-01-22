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
bazelisk run //props/cli -- db recreate

# 5. Push agent images to registry
bazelisk run //props/core/critic:push
bazelisk run //props/core/grader:push
bazelisk run //props/core/critic_dev/improve:push
bazelisk run //props/core/critic_dev/optimize:push
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
bazelisk run //props/cli -- db recreate

# Backup and restore
bazelisk run //props/cli -- db backup
bazelisk run //props/cli -- db restore <backup_file>
```

## Specimens Dataset

**Specimens data lives in a separate repository**: <https://github.com/agentydragon/specimens>

The `ADGN_PROPS_SPECIMENS_ROOT` environment variable points to the specimens repo (typically `~/code/specimens`).
The props package loads specimen data from this external location.

For testing with in-repo fixtures (as used in CI and by GitHub Copilot agents):

```bash
export ADGN_PROPS_SPECIMENS_ROOT="$PWD/props/testing/fixtures/testdata/specimens"
```

## GitHub Copilot Agent Setup

GitHub Copilot agents working on the props codebase should use the automated environment setup configured in `.github/workflows/copilot-setup-steps.yml`. This workflow sets up:

**Environment Variables** (analogous to Claude code hooks setup):

- `ADGN_PROPS_SPECIMENS_ROOT`: Points to in-repo test fixtures
- `PGHOST`, `PGPORT`, `PGUSER`, `PGDATABASE`: PostgreSQL connection
- `AGENT_PGHOST`: PostgreSQL host for agent containers
- `PROPS_REGISTRY_PROXY_HOST`, `PROPS_REGISTRY_PROXY_PORT`: Registry proxy config
- `PROPS_DOCKER_NETWORK`: Docker network for agent containers (props-agents)
- `PROPS_E2E_HOST_HOSTNAME`: Host network address for containers (172.17.0.1)

**Network Setup Differences:**

- Claude hooks: Uses `host` network with HTTP proxy
- GitHub Copilot: Uses `props-agents` Docker network (simpler, no proxy needed)

For detailed setup instructions, see: `.github/docs/PROPS_ENVIRONMENT_SETUP.md`

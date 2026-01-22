# Props Ecosystem

High-level architecture and shared infrastructure for the props evaluation system.

## Directory Structure

```
props/
├── .envrc                    # Devenv entry point for env vars
├── compose.yaml              # Docker Compose for postgres, registry, proxy
├── BUILD.bazel               # Bazel build file
├── AGENTS.md                 # Agent instructions
├── core/                     # Core Python library
│   ├── agent_registry.py     # Agent execution registry
│   ├── agent_types.py        # Agent type definitions
│   ├── models/               # Data models
│   ├── gepa/                 # GEPA prompt optimization
│   └── docs/                 # Core documentation
├── cli/                      # Command-line interface
│   ├── __main__.py           # CLI entry point
│   ├── cmd_db.py             # Database commands
│   ├── cmd_snapshot.py       # Snapshot commands
│   └── ...                   # Other command modules
├── db/                       # Database layer
│   ├── models.py             # SQLAlchemy models
│   ├── migrations/           # Alembic migrations
│   └── sync/                 # Specimen sync utilities
├── backend/                  # FastAPI dashboard backend
│   ├── app.py                # FastAPI app
│   └── routes/               # API endpoints
├── frontend/                 # Svelte UI
│   ├── package.json
│   └── src/                  # Frontend source
├── critic/                   # Critic agent definitions
├── grader/                   # Grader agent definitions
├── critic_dev/               # Development critic agents
│   ├── improve/              # Improvement agent
│   └── optimize/             # Optimization agent
├── llm_proxy/                # LLM proxy service
├── registry_proxy/           # Container registry proxy
├── standards/                # Property definitions
│   ├── python/               # Python-specific properties
│   ├── markdown/             # Markdown-specific properties
│   └── domain-types-and-units/
├── testing/                  # Testing utilities
├── docs/                     # Documentation
└── prompts/                  # Prompt templates
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
bazelisk run //props/critic:push
bazelisk run //props/grader:push
bazelisk run //props/critic_dev/improve:push
bazelisk run //props/critic_dev/optimize:push
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

# Props Ecosystem

High-level architecture and shared infrastructure for the props evaluation system.

## Directory Structure

```
props/
├── .envrc                    # Devenv entry point for env vars
├── compose.yaml              # Docker Compose for postgres, registry, backend
├── core/                     # Core types and models
│   ├── agent_types.py        # Agent type definitions and TypeConfig union
│   ├── eval_api_models.py    # Request/response models for eval API
│   ├── models/               # Data models (examples, true_positive, etc.)
│   └── gepa/                 # GEPA prompt optimization
├── cli/                      # Command-line interface
│   └── cmd_*.py              # Subcommand modules (db, stats, etc.)
├── db/                       # Database layer
│   ├── models.py             # SQLAlchemy ORM models
│   ├── migrations/           # Alembic migration (single complete_schema)
│   └── sync/                 # Specimen sync from filesystem to DB
├── orchestration/            # Agent execution
│   ├── agent_registry.py     # Container lifecycle, image pull, role creation
│   └── agent_credentials.py  # Per-agent PostgreSQL role management
├── backend/                  # Unified FastAPI server
│   ├── app.py                # FastAPI app with lifespan
│   ├── auth.py               # Auth middleware (postgres creds → RLS)
│   └── routes/               # API endpoints
│       ├── stats.py          # Dashboard stats API
│       ├── runs.py           # Agent runs + validation API
│       ├── ground_truth.py   # Snapshot/issue browsing API
│       ├── llm.py            # LLM proxy (OpenAI-compatible)
│       └── registry.py       # OCI registry proxy
├── frontend/                 # Svelte dashboard UI
├── agents/                   # Agent implementations
│   ├── critic/               # Critic agent (finds issues in code)
│   ├── grader/               # Grader daemon (matches issues to ground truth)
│   └── critic_dev/           # Meta-agents that develop critics
│       ├── improve/          # Creates improved critic definitions
│       └── optimize/         # Runs eval loops to select best critics
├── standards/                # Property definitions (issue taxonomies)
├── testing/                  # Shared test fixtures and utilities
├── docs/                     # Documentation
└── prompts/                  # Mako prompt templates
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

# 2. Build and load backend image
bazelisk run //props/backend:load

# 3. Start infrastructure
docker compose up -d

# 4. Initialize database (runs migrations, syncs specimens)
bazelisk run //props/cli -- db recreate

# 5. Push agent images to registry
bazelisk run //props/agents/critic:push
bazelisk run //props/agents/grader:push
bazelisk run //props/agents/critic_dev/improve:push
bazelisk run //props/agents/critic_dev/optimize:push
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
- Backend API: <http://localhost:8000> — see <docs/backend_api.md> for endpoint reference
- PostgreSQL: localhost:5433
- Registry: localhost:5000 (direct, for debugging)

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

Specimens are frozen code states with labeled issues (true positives and false positives) used for training and evaluating the LLM critic. The dataset includes:

- Per-snapshot directories with `manifest.yaml` (source, split, bundle metadata) and issue files (`.yaml`)
- Each snapshot has its own `manifest.yaml` defining source commit and train/valid/test split

### Configuration

Set the `ADGN_PROPS_SPECIMENS_ROOT` environment variable to point to the specimens repository:

```bash
export ADGN_PROPS_SPECIMENS_ROOT=/path/to/specimens
```

When using direnv (recommended), this is configured in `.envrc`:

```bash
REPO_ROOT=$(git rev-parse --show-toplevel)
export ADGN_PROPS_SPECIMENS_ROOT="$REPO_ROOT/../specimens"
```

**Required**: The environment variable must be set. The package will raise an error if it's not configured.

### Authoring

See the [specimens repository](https://github.com/agentydragon/specimens) for format specs and authoring guides.

## Evaluation Workflow

The props system evaluates LLM critic agents through a fitness-based selection process:

1. **Specimens** - Code snapshots with canonical issues identified by humans (maintained in separate [specimens repository](https://github.com/agentydragon/specimens))
2. **Critiques** - Agent-generated issue reports for each specimen
3. **Grading** - Comparison of critiques against canonical issues to compute metrics (TP/FP/FN/recall/precision)
4. **Selection** - Agents are selected based on fitness scores derived from how well they identify canonical issues

For detailed information on training strategies and per-file examples, see [Training Strategy](docs/training_strategy.md).

## Usage Workflow

### 1. Run Critic on a Specimen

Run critic agent to find issues in a specimen:

```bash
# Run critic with a specific definition
props run ducktape/2025-11-20-00 --definition-id critic

# Run with a different model
props run ducktape/2025-11-20-00 --definition-id critic --model gpt-4o

# Filter to specific files
props run ducktape/2025-11-20-00 --definition-id critic --files src/foo.py src/bar.py
```

This:

- Loads the specimen from the database
- Runs the critic agent (Docker-based)
- Stores the critique in the database
- Returns the agent_run_id for grading

### 2. Grading

Grading is handled automatically by snapshot grader daemons. Use the frontend UI (`POST /api/runs/validation`) to trigger validation runs on specific definitions.

### Specimen Inspection

Specimen source code is stored in PostgreSQL and fetched by agent init scripts at runtime. To inspect specimen files, query the database directly or use the sync'd specimens repository.

## GitHub Copilot Agent Setup

GitHub Copilot agents working on the props codebase should use the automated environment setup configured in `.github/workflows/copilot-setup-steps.yml`. This workflow sets up:

**Environment Variables** (analogous to Claude code hooks setup):

- `ADGN_PROPS_SPECIMENS_ROOT`: Points to in-repo test fixtures for CI/testing
- `PGHOST`, `PGPORT`, `PGUSER`, `PGDATABASE`: PostgreSQL connection
- `AGENT_PGHOST`: PostgreSQL host for agent containers
- `PROPS_REGISTRY_PROXY_HOST`, `PROPS_REGISTRY_PROXY_PORT`: Registry proxy config
- `PROPS_DOCKER_NETWORK`: Docker network for agent containers (props-agents)
- `PROPS_E2E_HOST_HOSTNAME`: Host network address for containers (172.17.0.1)

**Network Setup Differences:**

- Claude hooks: Uses `host` network with HTTP proxy
- GitHub Actions: Uses `props-agents` Docker network (simpler, no HTTP proxy needed)

For the automated workflow, see `.github/workflows/copilot-setup-steps.yml`.

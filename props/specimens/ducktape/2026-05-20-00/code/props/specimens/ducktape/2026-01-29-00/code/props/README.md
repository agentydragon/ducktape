# Props Ecosystem

High-level architecture and shared infrastructure for the props evaluation system.

## Directory Structure

```
props/
├── .envrc                    # Devenv entry point for env vars
├── compose.yaml              # Docker Compose for postgres, registry, backend
├── BUILD.bazel               # Bazel build file
├── AGENTS.md                 # Agent instructions
├── core/                     # Core Python library
│   ├── agent_registry.py     # Agent execution registry
│   ├── agent_types.py        # Agent type definitions
│   ├── models/               # Data models
│   └── gepa/                 # GEPA prompt optimization
├── cli/                      # Command-line interface
│   ├── __main__.py           # CLI entry point
│   ├── cmd_db.py             # Database commands
│   ├── cmd_snapshot.py       # Snapshot commands
│   └── ...                   # Other command modules
├── db/                       # Database layer
│   ├── models.py             # SQLAlchemy models
│   ├── migrations/           # Alembic migrations
│   └── sync/                 # Specimen sync utilities
├── backend/                  # Unified FastAPI server
│   ├── app.py                # FastAPI app
│   ├── auth.py               # Auth middleware (postgres creds)
│   └── routes/               # API endpoints
│       ├── stats.py          # Dashboard stats API
│       ├── runs.py           # Agent runs API
│       ├── ground_truth.py   # Ground truth API
│       ├── eval.py           # Eval API (for PO/PI agents)
│       ├── llm.py            # LLM proxy (OpenAI API)
│       └── registry.py       # Registry proxy (OCI Distribution API)
├── frontend/                 # Svelte UI
│   ├── package.json
│   └── src/                  # Frontend source
├── critic/                   # Critic agent definitions
├── grader/                   # Grader agent definitions
├── critic_dev/               # Development critic agents
│   ├── improve/              # Improvement agent
│   └── optimize/             # Optimization agent
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

# 2. Build and load backend image
bazelisk run //props/backend:load

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
- Backend API: <http://localhost:8000>
  - Dashboard: `/api/stats`, `/api/runs`, `/api/gt`
  - LLM Proxy: `/v1/responses`
  - Registry Proxy: `/v2/*`
  - Eval API: `/api/eval/*`
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

### 2. Grade a Critique

Grade stored critiques against canonical findings:

```bash
# Grade validation set
props grade-validation

# Use different model for grading
props grade-validation --model gpt-4o
```

This:

- Fetches critiques from the database
- Loads the specimens' canonical issues
- Runs the grader to compute metrics (TP/FP/FN/recall/precision)
- Stores grader results in the database

### 3. Query Results

Query stored agent runs from the database:

```python
from props.db.session import get_session
from props.db.models import AgentRun, ReportedIssue

with get_session() as session:
    # Get all agent runs for a snapshot
    runs = session.query(AgentRun).filter(
        AgentRun.type_config["snapshot_slug"].astext == "ducktape/2025-11-20-00"
    ).all()

    # Get reported issues for a run
    for issue in session.query(ReportedIssue).filter_by(agent_run_id=run_id):
        print(f"[{issue.issue_id}] {issue.rationale}")
```

All structured runs are persisted with:

- Agent configuration (type_config JSONB column storing snapshot_slug, files, model, etc.)
- Reported issues and occurrences in normalized tables (issue_id, rationale, locations)
- Specimen splits for train/valid/test separation
- Execution traces in LLM requests table (prompt, completion, tokens, cost)

### Specimen Inspection (for assistants)

**Note:** The `snapshot exec` command is currently disabled. Snapshot source code is now stored in PostgreSQL and fetched by agent init scripts at runtime. To inspect specimen files, query the database directly or use the sync'd specimens repository.

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

For complete setup instructions, see `.github/COPILOT_INSTRUCTIONS.md`.

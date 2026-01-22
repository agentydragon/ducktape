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

## Properties and Standards

Property definitions and coding standards are maintained in the `standards/` directory. See [standards/README.md](standards/README.md) for details on conventions, property files, GAP markers, and specimen-driven property evolution.

## Training Strategy: Per-File Examples

**Goal:** Train the LLM critic to behavior-clone the user's subjective code review judgment by using fine-grained training examples.

**Approach:** Generate multiple focused training examples per snapshot (single files, file pairs, component groups) in addition to the full-repo review. This provides tighter feedback loops and more training signal for optimization.

**Dataset model:**

- **Snapshot:** Frozen code state at a specific commit with labeled issues (TPs and FPs) — specimens from separate repo
- **Training Example:** `(snapshot, targeted_files)` pair where recall denominator is computed based on which issues are in expected recall scope for those files
- **True Positive filtering:** Uses `critic_scopes_expected_to_recall` to determine which issues should be detectable given a file set

For detailed information, see [Training Strategy](docs/training_strategy.md).

```mermaid
flowchart TD
  A[Specimen: code + freeform review items] --> B[Draft/refine property definition]
  B --> C[Generate/adjust reviewer prompts]
  C --> D[Run analyzers/reviewers on specimen]
  D --> E{Backtest results}
  E -->|Found expected issues| F[Success metrics ↑]
  E -->|Missed expected issues| B
  E -->|Flagged acceptable items| C
  D --> G{Novel findings?}
  G -->|Yes| H[Augment specimen: add "should find" / "do-not-flag"]
  H --> D
  G -->|No| I[Freeze specimen snapshot]

  %% Also allow direct property → reviewers check on arbitrary code
  B -.-> J[LLM analyzers check arbitrary code]
  J -.-> E
```

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
- Runs the critic agent with MCP tools (Docker-based)
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

- Input/output payloads (JSONB columns in database)
- Specimen splits for train/valid/test separation
- Execution traces in events table

### Specimen Inspection (for assistants)

**Note:** The `snapshot exec` command is currently disabled. Snapshot source code is now stored in PostgreSQL and fetched by agent init scripts at runtime. To inspect specimen files, query the database directly or use the sync'd specimens repository.

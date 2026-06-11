@README.md

# Props Core Library

Core Python library for the props evaluation system.

## Agent-Facing Documentation

@docs/AGENTS.md

**Key documentation (transcluded below):**

@docs/db/ground_truth.md.j2
@docs/db/examples.md.j2
@docs/db/evaluation_flow.md.j2
@docs/database_access.md
@docs/authoring_agents.md.j2

## CLI

The `props` CLI provides all core functionality:

```bash
props --help
props run --help
props critic-agent --help
props grader-agent --help
```

## Database Migrations (Alembic)

**All schema changes must go through Alembic migrations.** Do not edit the database schema directly.

- **Migrations location:** `db/migrations/versions/`
- **Configuration:** `db/migrations/env.py`
- **Alembic CLI:** Run from `db/` directory with `direnv exec . alembic <command>`

**Project conventions:**

- Use YYYYMMDD000000 timestamp format for revision IDs (e.g., `20251213000000`)
- ORM models in `db/*.py` are still required for application code
- RLS policies: managed in `db/setup.py` via `enable_rls()` (not in migrations)
- RLS helper functions: should be created in migrations (they're part of the schema)

**CASCADE WARNING:** When dropping views with CASCADE (e.g., `DROP VIEW IF EXISTS recall_by_run
CASCADE`), all dependent views are also dropped. Before writing such a migration:

1. Query `pg_depend` or check the schema to list ALL dependent views
2. Recreate all dropped views in correct dependency order in the same migration
3. Re-grant permissions (`GRANT SELECT ON TABLE view_name TO agent_base`)

## Temporary Database Users (Scoped Access)

**Pattern:** Task-specific agents create temporary PostgreSQL users with RLS-scoped access for the duration of their execution.

**Why temporary users?**

- Enforces data isolation (e.g., TRAIN-only access for optimization agents)
- Prevents accidental leakage of validation/test data during training
- No persistent credentials to manage or rotate
- Automatic cleanup on agent exit

**Function-Based RLS:**

- Username pattern encodes scope (e.g., `prompt_optimizer_agent_{uuid}`)
- PostgreSQL function extracts ID from username: `current_prompt_optimizer_run_id()`
- Centralized policies use function to filter rows - O(1) overhead
- Scales to many concurrent users without per-user policy creation

**See also:**

- `db/temp_user_manager.py` - Unified user manager for all agent types
- `db/migrations/versions/20251215000000_add_prompt_optimizer_rls.py` - RLS setup migration

## Architecture: MCP I/O Models vs DB Persistence Models

### Problem

Database persistence models should NOT use MCP I/O protocol types directly. Using MCP types (like
`CriticSubmitPayload`, `ReportedIssue`, `GraderOutput`) in database schemas couples database
migrations to protocol changes.

### Solution: Two Parallel Model Hierarchies

**MCP I/O Models** (in `critic/models.py`, `grader/models.py`):

- Purpose: Define the API contract for MCP tool inputs/outputs
- Use NewType wrappers for type safety, rich types, validation logic

**DB Persistence Models** (in `db/snapshots.py`):

- Purpose: Define the database storage format
- Use primitives (`str` instead of NewType, `list` instead of `set`)
- Frozen models, stable schema

**Conversion Functions** (in `grader/persistence.py`):

- Bridge between MCP and DB models
- Convert TO DB when writing
- Use DB model directly when reading

### Layer Isolation Test

The test `tests/db/test_layer_isolation.py` enforces that the database layer (`db/`) does not
import from MCP I/O layers (`critic.models`, `grader.models`).

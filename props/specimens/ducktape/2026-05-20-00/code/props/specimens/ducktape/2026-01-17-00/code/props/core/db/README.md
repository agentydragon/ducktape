# Properties Evaluation Database

PostgreSQL-based storage for properties evaluation results.

## Database Separation: Production vs Test

We maintain **TWO separate databases** to ensure tests never affect production data:

### Production Database: `eval_results`

- **Purpose**: Real evaluation results, persistent storage
- **DO NOT DROP/RECREATE**: Contains valuable data
- **Connection**: Uses standard `PG*` environment variables (set by devenv)

### Test Database: `eval_results_test`

- **Purpose**: Integration tests only
- **FREELY DROP/RECREATE**: Tests use fixtures to reset state
- **Connection**: Uses individual environment variables for test database configuration

## Setup

1. **Start PostgreSQL container**:

   ```bash
   cd props
   docker compose up -d
   ```

   This starts the PostgreSQL container in the background.

2. **Initialize database**:

   ```bash
   # Create database, schema, RLS policies, and sync specimens
   props db recreate --yes
   ```

   This automatically:
   - Creates the `eval_results` database
   - Runs Alembic migrations to create schema
   - Sets up the `agent_base` role and RLS policies
   - Syncs specimen data from the specimens repository

   For incremental updates (without dropping tables):

   ```bash
   props sync
   ```

## Database Users

### postgres (admin)

- **Full access**: Create/drop tables, write data, read all data
- **Purpose**: Migrations, data loading, test setup
- **Bypasses RLS**: Can see all splits (train/valid/test)
- **Connection**: Via PGUSER/PGPASSWORD environment variables

### Temporary Agent Users (per-task)

- **Username pattern**: `agent_{agent_run_id}` (unified for all agent types)
- **Role membership**: Inherit from `agent_base` role
- **Permissions**: SELECT on reference tables, INSERT/UPDATE/DELETE on agent-specific tables
- **RLS-restricted**: Access filtered by `current_agent_run_id()` and `current_agent_type()`
- **Purpose**: Enforce data isolation (e.g., TRAIN-only for prompt optimizer, own-run for critics)
- **Lifecycle**: Created on-demand, automatically cleaned up on task completion
- **Implementation**: See `TempUserManager` in `db/temp_user_manager.py`

Type-specific access is controlled entirely by RLS policies based on `agent_runs.type_config`,
not by different roles or username patterns.

## Running Tests

```bash
# Run database tests via Bazel
bazel test //props/core/db/...
```

**Important**: Tests use fixtures that **only affect eval_results_test**. Production data is never touched.

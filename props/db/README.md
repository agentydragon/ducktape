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
   # Sync all data from filesystem to database
   props db sync

   # Use staged files instead of HEAD (for development)
   props db sync --use-staged

   # Validate sync without committing changes
   props db sync --dry-run
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
- **Access Control**: Row-Level Security (RLS) policies filter data based on `current_agent_run_id()` and `current_agent_type()` session variables
  - Type-specific access (e.g., TRAIN-only for prompt optimizer, own-run for critics) is controlled entirely by RLS policies based on `agent_runs.type_config`, not by different roles or username patterns
- **Purpose**: Enforce data isolation between agent runs
- **Lifecycle**: Created on-demand by `TempUserManager`, automatically cleaned up on task completion
- **Implementation**: See `TempUserManager` in `db/temp_user_manager.py`

## Running Tests

```bash
# Run all database tests via Bazel
bazel test //props/db/...

# Run a specific test file
bazel test //props/db:test_models

# Run tests with verbose output
bazel test //props/db/... --test_output=all
```

**Important**: Tests use fixtures that **only affect eval_results_test**. Production data is never touched.

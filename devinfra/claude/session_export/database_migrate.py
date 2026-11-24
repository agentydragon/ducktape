"""Alembic runner for the database the sync writes."""

from pathlib import Path

from util.db_migrations import MigrationRunner

# Alembic loads migrations/env.py, which imports the store models.
# gazelle:include_dep :store
RUNNER = MigrationRunner(
    migrations_dir=Path(__file__).parent / "migrations",
    lock_key=0x4353_5359,  # "CSSY"
)

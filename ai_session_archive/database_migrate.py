"""Alembic runner for the archive database."""

from pathlib import Path

from util.db_migrations import MigrationRunner

# Alembic loads migrations/env.py, which imports the database models.
# gazelle:include_dep :database
RUNNER = MigrationRunner(
    migrations_dir=Path(__file__).parent / "migrations",
    lock_key=0x4149_4152,  # "AIAR"
)

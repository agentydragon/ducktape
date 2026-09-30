"""Alembic runner for the database the sync writes."""

from pathlib import Path

from devinfra.claude.session_export.store import Base
from util.db_migrations import MigrationRunner

RUNNER = MigrationRunner(
    metadata=Base.metadata,
    migrations_dir=Path(__file__).parent / "migrations",
    lock_key=0x4353_5359,  # "CSSY"
)

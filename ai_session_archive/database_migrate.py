"""Alembic runner for the archive database."""

from pathlib import Path

from ai_session_archive.database import Base
from util.db_migrations import MigrationRunner

RUNNER = MigrationRunner(
    metadata=Base.metadata,
    migrations_dir=Path(__file__).parent / "migrations",
    lock_key=0x4149_4152,  # "AIAR"
    version_table="alembic_version_ai_session_archive",
)

"""Legacy Alembic runner retained for Python test fixtures."""

from __future__ import annotations

from pathlib import Path

from agentplane.action_service.db import Base
from util.db_migrations import MigrationRunner

RUNNER = MigrationRunner(
    metadata=Base.metadata,
    migrations_dir=Path(__file__).parent / "migrations",
    lock_key=0x4143_544E,  # "ACTN"
)

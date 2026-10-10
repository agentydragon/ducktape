"""The Sandbox Service's command admission schema, applied by its own migration lock."""

from pathlib import Path

from agentplane.sandbox_service.commands.db import Base
from util.db_migrations import MigrationRunner

RUNNER = MigrationRunner(
    metadata=Base.metadata,
    migrations_dir=Path(__file__).parent / "migrations",
    lock_key=0x5343_4D44,  # "SCMD"
)

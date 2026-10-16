"""The retained Runner Gateway Session table schema, applied by its own migration lock."""

from pathlib import Path

from agentplane.runner_gateway.commands.db import metadata
from util.db_migrations import MigrationRunner

RUNNER = MigrationRunner(
    metadata=metadata,
    migrations_dir=Path(__file__).parent / "migrations",
    lock_key=0x5343_4D44,  # "SCMD"
)

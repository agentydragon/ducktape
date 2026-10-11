"""Session history schema, with the History Service's thread fold beside the raw log."""

from pathlib import Path

from agentplane.history_service.fold import models as fold_models
from agentplane.history_service.storage.db import Base
from util.db_migrations import MigrationRunner, merged_metadata

RUNNER = MigrationRunner(
    metadata=merged_metadata(Base.metadata, fold_models.Base.metadata),
    migrations_dir=Path(__file__).parent / "migrations",
    lock_key=0x53415243,
)

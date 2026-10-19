"""Image-coupled Alembic runner for the egress history-owned database.

CLEANUP(added 2026-10-17): Delete this module, `decision_tables.py` and `migrations/` with the Alembic
handoff test fixtures in `history_migrations` and `history`, once the testing and staging history
databases carry `egress_goose_version`.
"""

from __future__ import annotations

from pathlib import Path

from agentplane.egress.decision_tables import Base
from util.db_migrations import MigrationRunner

RUNNER = MigrationRunner(
    metadata=Base.metadata,
    migrations_dir=Path(__file__).parent / "migrations",
    lock_key=0x4547_5244,  # "EGRD"
)

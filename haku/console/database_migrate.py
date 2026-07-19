"""Alembic migration runner for haku-console's database."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, text

from haku.console.database_schema import metadata

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Arbitrary fixed key, unique to this lock's purpose (no meaning beyond that). With more
# than one haku-console replica, every pod runs migrations at startup — pg_advisory_xact_lock
# serializes them onto the connection's transaction (auto-released on commit/rollback) so two
# pods starting at once don't race to apply the same migration.
_MIGRATION_LOCK_KEY = 0x4B41_4B55  # "KAKU" in hex, close enough to "haku" to be memorable


def run_migrations_for_connection(conn: Any, revision: str = "head") -> None:
    conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _MIGRATION_LOCK_KEY})
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.attributes["connection"] = conn
    cfg.attributes["target_metadata"] = metadata
    alembic_command.upgrade(cfg, revision)


def apply_migrations(database_url: str, revision: str = "head") -> None:
    """Upgrade the haku-console database to head. The explicit startup step (app.main) and the tests
    call this once against the shared database — migrations are an ownership of the process, not a
    side effect of constructing a ledger/store."""
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            run_migrations_for_connection(conn, revision)
    finally:
        engine.dispose()

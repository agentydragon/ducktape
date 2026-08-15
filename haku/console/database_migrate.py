"""Alembic migration runner for haku-console's database."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from haku.console.database_schema import metadata
from haku.state_index.schema import Base as StateIndexBase

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
    if revision == "head":
        # Alembic revisions are immutable once deployed. If a revision is accidentally edited after
        # a database has already recorded it, upgrade-to-head is a no-op even though the ORM may now
        # require columns that are absent. Compile and execute a zero-row read for every owned table
        # so that schema-incompatible processes fail during startup instead of serving as Ready.
        #
        # Both metadatas, because the index declares its own `Base` and its tables are just as much
        # this database's as the console's own: on 2026-08-15 a rename edited into an already-applied
        # 0037 shipped exactly this way, and the guard did not see it because it only knew about one
        # of the two. `.tables` rather than `.sorted_tables`: creation order is meaningless for a
        # zero-row read, and sorting warns about the mutually dependent foreign keys in the Agent
        # graph, which are deliberate.
        for owned in (metadata, StateIndexBase.metadata):
            for table in owned.tables.values():
                conn.execute(select(table).limit(0))


def sync_database_url(database_url: str) -> str:
    """Render an application database URL for synchronous Alembic/psycopg access."""
    return make_url(database_url).set(drivername="postgresql+psycopg").render_as_string(hide_password=False)


def apply_migrations(database_url: str, revision: str = "head") -> None:
    """Upgrade the haku-console database to head. The explicit startup step (app.main) and the tests
    call this once against the shared database — migrations are an ownership of the process, not a
    side effect of constructing a ledger/store."""
    engine = create_engine(sync_database_url(database_url))
    try:
        with engine.begin() as conn:
            run_migrations_for_connection(conn, revision)
    finally:
        engine.dispose()

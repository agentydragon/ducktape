"""Applying one database's Alembic history from the image that owns it."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import MetaData, create_engine, text

from util.db_url import render_postgresql_url

# SQLAlchemy loads psycopg from the URL scheme; Gazelle cannot infer the runtime dependency.
# gazelle:include_dep @pypi//psycopg


def merged_metadata(*sources: MetaData) -> MetaData:
    """One history's tables when they are declared on several declarative bases."""
    merged = MetaData()
    for source in sources:
        for table in source.tables.values():
            table.to_metadata(merged)
    return merged


@dataclass(frozen=True)
class MigrationRunner:
    """One history, its tables, and the lock two replicas starting at once contend for.

    `version_table` names this history's stamp. It matters only where two histories share a
    database -- the integration app's tests point the app's runner and the Action Service's at one
    server, and under the default `alembic_version` each would read the other's revision and fail to
    locate it.
    """

    metadata: MetaData
    migrations_dir: Path
    lock_key: int
    version_table: str | None = None

    def sync_url(self, database_url: str) -> str:
        return render_postgresql_url(database_url, "postgresql+psycopg")

    def run_for_connection(self, connection: Any, revision: str = "head") -> None:
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": self.lock_key})
        config = AlembicConfig()
        config.set_main_option("script_location", str(self.migrations_dir))
        config.attributes["connection"] = connection
        config.attributes["target_metadata"] = self.metadata
        # Startup advances the history this image owns, while migration tests assert its exact schema.
        # Keeping model equality out of runtime permits a rollout to leave unused columns behind.
        alembic_command.upgrade(config, revision)

    def apply(self, database_url: str, revision: str = "head") -> None:
        engine = create_engine(self.sync_url(database_url))
        try:
            with engine.begin() as connection:
                self.run_for_connection(connection, revision)
        finally:
            engine.dispose()

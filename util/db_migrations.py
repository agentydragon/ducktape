"""Applying one database's Alembic history from the image that owns it."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import MetaData, create_engine, select, text
from sqlalchemy.engine import make_url

# SQLAlchemy loads psycopg from the URL scheme; Gazelle cannot infer the runtime dependency.
# gazelle:include_dep @pypi//psycopg


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
        return make_url(database_url).set(drivername="postgresql+psycopg").render_as_string(hide_password=False)

    def run_for_connection(self, connection: Any, revision: str = "head") -> None:
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": self.lock_key})
        config = AlembicConfig()
        config.set_main_option("script_location", str(self.migrations_dir))
        config.attributes["connection"] = connection
        config.attributes["target_metadata"] = self.metadata
        alembic_command.upgrade(config, revision)
        if revision == "head":
            self.verify_schema(connection)

    def verify_schema(self, connection: Any) -> None:
        """Every table the metadata declares is readable, so a migration that did not run is loud."""
        for table in self.metadata.tables.values():
            connection.execute(select(table).limit(0))

    def apply(self, database_url: str, revision: str = "head") -> None:
        engine = create_engine(self.sync_url(database_url))
        try:
            with engine.begin() as connection:
                self.run_for_connection(connection, revision)
        finally:
            engine.dispose()

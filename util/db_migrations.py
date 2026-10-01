"""Applying one database's Alembic history from the image that owns it."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alembic import command as alembic_command
from alembic.autogenerate import compare_metadata
from alembic.config import Config as AlembicConfig
from alembic.migration import MigrationContext
from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.schema import SchemaItem

# SQLAlchemy loads psycopg from the URL scheme; Gazelle cannot infer the runtime dependency.
# gazelle:include_dep @pypi//psycopg


def _declared_table(
    _obj: SchemaItem, _name: str | None, type_: str, reflected: bool, compare_to: SchemaItem | None
) -> bool:
    """Alembic's `include_object` hook: a table in the database that the metadata does not declare is another
    history's, or a version table, and not this history's to judge."""
    return not (type_ == "table" and reflected and compare_to is None)


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
        """Raise unless the schema is the one the metadata describes.

        A database stamped at head can still differ: it applied a migration that was edited afterwards,
        or was altered by hand. Nothing else notices, and the app then writes to the wrong schema.
        """
        context = MigrationContext.configure(connection, opts={"include_object": _declared_table})
        if drift := compare_metadata(context, self.metadata):
            raise RuntimeError(f"The migrated schema differs from the models: {drift=}")

    def apply(self, database_url: str, revision: str = "head") -> None:
        engine = create_engine(self.sync_url(database_url))
        try:
            with engine.begin() as connection:
                self.run_for_connection(connection, revision)
        finally:
            engine.dispose()

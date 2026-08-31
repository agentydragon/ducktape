"""Alembic migration runner for haku-console's database."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from haku.console.database_schema import metadata
from haku.recall_index.schema import Base as RecallIndexBase

# SQLAlchemy loads the psycopg dialect at runtime via the `postgresql+psycopg://`
# URL scheme; nothing imports it directly, so Gazelle cannot see the dependency.
# gazelle:include_dep @pypi//psycopg

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Arbitrary fixed key, unique to this lock's purpose (no meaning beyond that). The release Job
# normally has one Pod, but the transaction-scoped advisory lock also makes a deliberate retry or
# a transient overlapping execution safe.
_MIGRATION_LOCK_KEY = 0x4B41_4B55  # "KAKU" in hex, close enough to "haku" to be memorable


class MigrationSettings(BaseSettings):
    """The migration Job's deliberately narrow process contract."""

    model_config = SettingsConfigDict(env_prefix="HAKU_CONSOLE__")

    database_url: SecretStr


def run_migrations_for_connection(conn: Any, revision: str = "head") -> None:
    conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _MIGRATION_LOCK_KEY})
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.attributes["connection"] = conn
    cfg.attributes["target_metadata"] = metadata
    alembic_command.upgrade(cfg, revision)
    if revision == "head":
        verify_schema_for_connection(conn)


def verify_schema_for_connection(conn: Any) -> None:
    """Fail if this image's ORM mappings cannot read the deployed schema.

    Alembic revisions are immutable once deployed. If one is accidentally edited after a database
    recorded it, upgrade-to-head is a no-op even though the ORM may require absent columns. Compile
    and execute a zero-row read for every table the Console owns, so a mismatched process never
    serves as Ready. `.tables` rather than `.sorted_tables`: creation order is irrelevant for these
    reads and sorting warns about deliberate mutually dependent Agent foreign keys.
    """
    for owned in (metadata, RecallIndexBase.metadata):
        for table in owned.tables.values():
            conn.execute(select(table).limit(0))


def sync_database_url(database_url: str) -> str:
    """Render an application database URL for synchronous Alembic/psycopg access."""
    return make_url(database_url).set(drivername="postgresql+psycopg").render_as_string(hide_password=False)


def apply_migrations(database_url: str, revision: str = "head") -> None:
    """Upgrade the haku-console database to ``revision`` under the release Job's lock."""
    engine = create_engine(sync_database_url(database_url))
    try:
        with engine.begin() as conn:
            run_migrations_for_connection(conn, revision)
    finally:
        engine.dispose()


def verify_schema(database_url: str) -> None:
    """Run the application startup's read-only compatibility check without applying DDL."""
    engine = create_engine(sync_database_url(database_url))
    try:
        with engine.connect() as conn:
            verify_schema_for_connection(conn)
    finally:
        engine.dispose()


def main() -> None:
    """Run the migration-only process entrypoint from ``HAKU_CONSOLE__DATABASE_URL``.

    This intentionally avoids constructing ``Settings``: migrations need only the database URL and
    must not require the Console's OIDC, connector, routine, or Kubernetes credentials.
    """
    apply_migrations(MigrationSettings().database_url.get_secret_value())


if __name__ == "__main__":
    main()

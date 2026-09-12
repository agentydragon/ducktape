"""Image-coupled Alembic runner for the integration app's database."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from x.agentplane.app.trajectory import Base

# SQLAlchemy loads psycopg from the URL scheme; Gazelle cannot infer the runtime dependency.
# gazelle:include_dep @pypi//psycopg

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_LOCK_KEY = 0x5452_414A  # "TRAJ"
# Base is declared in operator_sessions.py; trajectory.py's own Thread/Event tables and
# operator_sessions.py's BrowserSession all share it, and importing it here (via trajectory.py,
# which already imports operator_sessions.py) registers every table onto one metadata.
_METADATA = Base.metadata

# This history's own stamp. The Action Service owns a second Alembic history, and the app's
# integration tests point both runners at one database; the default `alembic_version` would have
# each read the other's stamp and fail to locate the revision.
VERSION_TABLE = "alembic_version_app"


class MigrationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_")

    database_url: str


def sync_database_url(database_url: str) -> str:
    return make_url(database_url).set(drivername="postgresql+psycopg").render_as_string(hide_password=False)


def run_migrations_for_connection(connection: Any, revision: str = "head") -> None:
    connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _MIGRATION_LOCK_KEY})
    config = AlembicConfig()
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    config.attributes["connection"] = connection
    config.attributes["target_metadata"] = _METADATA
    alembic_command.upgrade(config, revision)
    if revision == "head":
        verify_schema_for_connection(connection)


def verify_schema_for_connection(connection: Any) -> None:
    for table in _METADATA.tables.values():
        connection.execute(select(table).limit(0))


def apply_migrations(database_url: str, revision: str = "head") -> None:
    engine = create_engine(sync_database_url(database_url))
    try:
        with engine.begin() as connection:
            run_migrations_for_connection(connection, revision)
    finally:
        engine.dispose()


def main() -> None:
    apply_migrations(MigrationSettings().database_url)


if __name__ == "__main__":
    main()

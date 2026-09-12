"""The migration history against a real PostgreSQL: it reaches the models, and re-running it is a no-op."""

import pytest_bazel
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection

from x.agentplane.app.database_migrate import VERSION_TABLE, apply_migrations, sync_database_url
from x.agentplane.app.trajectory import Base

# The migrated database is read back over psycopg, which SQLAlchemy loads from the URL scheme.
# gazelle:include_dep @pypi//psycopg


def _drift(connection: Connection) -> list[object]:
    """What Alembic would have to emit to turn the live schema into the models."""
    context = MigrationContext.configure(connection, opts={"version_table": VERSION_TABLE})
    return list(compare_metadata(context, Base.metadata))


def test_the_migrated_schema_is_the_one_the_models_describe(db_url: str) -> None:
    """Nothing creates a table at runtime, so a model column without a migration reaches no database."""
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.connect() as connection:
            assert _drift(connection) == []
    finally:
        engine.dispose()


def test_a_second_run_over_a_migrated_database_changes_nothing(db_url: str) -> None:
    """Every replica's init container runs this against the same database, on every rollout."""
    apply_migrations(db_url)
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.connect() as connection:
            assert _drift(connection) == []
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()

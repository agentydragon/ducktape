"""The migration history against a real PostgreSQL: it reaches the models, and re-running it is a no-op."""

import os
import subprocess
import sys

import pytest_bazel
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Connection

from agentplane.app.database_migrate import RUNNER

# The migrated database is read back over psycopg, which SQLAlchemy loads from the URL scheme.
# gazelle:include_dep @pypi//psycopg


def _drift(connection: Connection) -> list[object]:
    """What Alembic would have to emit to turn the live schema into the models."""
    context = MigrationContext.configure(connection, opts={"version_table": RUNNER.version_table})
    return list(compare_metadata(context, RUNNER.metadata))


def test_the_migrated_schema_is_the_one_the_models_describe(db_url: str) -> None:
    """Nothing creates a table at runtime, so a model column without a migration reaches no database."""
    engine = create_engine(RUNNER.sync_url(db_url))
    try:
        with engine.connect() as connection:
            assert _drift(connection) == []
    finally:
        engine.dispose()


def test_a_second_run_over_a_migrated_database_changes_nothing(db_url: str) -> None:
    """Every replica's init container runs this against the same database, on every rollout."""
    RUNNER.apply(db_url)
    engine = create_engine(RUNNER.sync_url(db_url))
    try:
        with engine.connect() as connection:
            assert _drift(connection) == []
    finally:
        engine.dispose()


def test_the_runner_registers_every_migrated_table(db_url: str) -> None:
    """`database_migrate.py` registers tables by importing their modules. This process's fixtures import
    them all anyway, so the runner is imported in a fresh interpreter."""
    registered = subprocess.run(
        [sys.executable, "-c", "from agentplane.app.database_migrate import RUNNER; print(*RUNNER.metadata.tables)"],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    engine = create_engine(RUNNER.sync_url(db_url))
    try:
        migrated = set(inspect(engine).get_table_names()) - {RUNNER.version_table}
    finally:
        engine.dispose()
    assert set(registered) == migrated


if __name__ == "__main__":
    pytest_bazel.main()

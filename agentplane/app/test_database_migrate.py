"""The migration history against real PostgreSQL: tests compare the exact declared schema and reruns are no-ops."""

import os
import subprocess
import sys
from typing import Any, cast

import pytest_bazel
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, create_engine, inspect, text
from sqlalchemy.schema import SchemaItem

from agentplane.app.database_migrate import RUNNER

# The migrated database is read back over psycopg, which SQLAlchemy loads from the URL scheme.
# gazelle:include_dep @pypi//psycopg


def _execute(db_url: str, statement: str) -> None:
    engine = create_engine(RUNNER.sync_url(db_url))
    try:
        with engine.begin() as connection:
            connection.execute(text(statement))
    finally:
        engine.dispose()


def _include_owned_tables(
    _obj: SchemaItem, _name: str | None, type_: str, reflected: bool, compare_to: SchemaItem | None
) -> bool:
    """Only compare tables owned by this history; shared databases can hold other histories."""
    return not (type_ == "table" and reflected and compare_to is None)


def _schema_drift(connection: Connection) -> list[Any]:
    context = MigrationContext.configure(connection, opts={"include_object": _include_owned_tables})
    return cast(list[Any], compare_metadata(context, RUNNER.metadata))


def test_migration_test_detects_a_database_that_differs_from_the_models(db_url: str) -> None:
    """The migration test retains exact model/schema comparison even though startup allows leftovers."""
    _execute(db_url, "ALTER TABLE thread ALTER COLUMN name TYPE varchar(80)")
    RUNNER.apply(db_url)
    engine = create_engine(RUNNER.sync_url(db_url))
    try:
        with engine.connect() as connection:
            assert _schema_drift(connection)
    finally:
        engine.dispose()


def test_migration_test_detects_an_index_only_the_database_has(db_url: str) -> None:
    _execute(db_url, "CREATE INDEX index_only_in_test_database ON thread (created_at)")
    RUNNER.apply(db_url)
    engine = create_engine(RUNNER.sync_url(db_url))
    try:
        with engine.connect() as connection:
            assert any(
                diff[0] == "remove_index" and diff[1].name == "index_only_in_test_database"
                for diff in _schema_drift(connection)
            )
    finally:
        engine.dispose()


def test_runtime_migration_accepts_an_unused_extra_nullable_column(db_url: str) -> None:
    _execute(db_url, "ALTER TABLE thread ADD COLUMN retained_unused_field text")
    RUNNER.apply(db_url)
    engine = create_engine(RUNNER.sync_url(db_url))
    try:
        with engine.connect() as connection:
            assert "retained_unused_field" in {column["name"] for column in inspect(connection).get_columns("thread")}
            assert _schema_drift(connection)
    finally:
        engine.dispose()


def test_a_fresh_migration_matches_the_declared_schema_exactly(db_url: str) -> None:
    engine = create_engine(RUNNER.sync_url(db_url))
    try:
        with engine.connect() as connection:
            assert _schema_drift(connection) == []
    finally:
        engine.dispose()


def test_a_second_run_over_a_migrated_database_changes_nothing(db_url: str) -> None:
    """Every replica's init container runs this against the same database, on every rollout."""
    RUNNER.apply(db_url)


def test_tables_the_history_does_not_own_are_not_drift(db_url: str) -> None:
    """Histories can share a database, each with a version table of its own."""
    _execute(db_url, "CREATE TABLE another_history_test_table (id integer PRIMARY KEY)")
    RUNNER.apply(db_url)


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

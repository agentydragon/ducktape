"""The versioned archive history creates the exact schema and is safe to rerun."""

# gazelle:include_dep //util/testing:postgres_fixtures
from collections.abc import Iterator
from typing import Any, cast

import pytest
import pytest_bazel
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, create_engine
from sqlalchemy.schema import SchemaItem

from util.testing.postgres import DatabaseNames, create_temporary_database
from x.ai_session_archive.database import Base, make_engine
from x.ai_session_archive.database_migrate import RUNNER

pytest_plugins = ["util.testing.postgres_fixtures"]


@pytest.fixture
def fresh_database(postgres_admin_url: str, database_names: DatabaseNames) -> Iterator[str]:
    with create_temporary_database(postgres_admin_url, database_names()) as database_url:
        yield database_url


def _include_owned_tables(
    _obj: SchemaItem, _name: str | None, type_: str, reflected: bool, compare_to: SchemaItem | None
) -> bool:
    """Ignore tables owned by other histories in a shared database."""
    return not (type_ == "table" and reflected and compare_to is None)


def _schema_drift(connection: Connection) -> list[Any]:
    context = MigrationContext.configure(connection, opts={"include_object": _include_owned_tables})
    return cast(list[Any], compare_metadata(context, Base.metadata))


def test_fresh_database_matches_models_and_migration_is_idempotent(fresh_database: str) -> None:
    RUNNER.apply(fresh_database)
    RUNNER.apply(fresh_database)

    engine = create_engine(RUNNER.sync_url(fresh_database))
    try:
        with engine.connect() as connection:
            assert _schema_drift(connection) == []
    finally:
        engine.dispose()


def test_make_engine_normalizes_cnpg_uri_to_installed_driver() -> None:
    engine = make_engine("postgresql://archive_user:archive_password@postgres.example:5432/archive")
    try:
        assert engine.url.drivername == "postgresql+psycopg"
        assert engine.url.username == "archive_user"
        assert engine.url.password == "archive_password"
        assert engine.url.host == "postgres.example"
        assert engine.url.database == "archive"
    finally:
        engine.dispose()


def test_make_engine_preserves_sqlite_urls() -> None:
    engine = make_engine("sqlite:///:memory:")
    try:
        assert engine.url.drivername == "sqlite"
    finally:
        engine.dispose()


def test_make_engine_preserves_explicit_postgresql_driver() -> None:
    engine = make_engine("postgresql+psycopg://archive_user:archive_password@postgres.example:5432/archive")
    try:
        assert engine.url.drivername == "postgresql+psycopg"
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()

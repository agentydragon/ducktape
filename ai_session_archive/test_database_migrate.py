"""The versioned archive history creates the exact schema and is safe to rerun."""

# gazelle:include_dep //testing:postgres_fixtures
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_bazel
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, create_engine
from sqlalchemy.schema import SchemaItem

from ai_session_archive.database import Base, make_engine
from ai_session_archive.database_migrate import RUNNER

pytest_plugins = ["testing.postgres_fixtures"]


def _include_owned_tables(
    _obj: SchemaItem, _name: str | None, type_: str, reflected: bool, compare_to: SchemaItem | None
) -> bool:
    """Ignore tables owned by other histories in a shared database."""
    return not (type_ == "table" and reflected and compare_to is None)


def _schema_drift(connection: Connection) -> list[Any]:
    context = MigrationContext.configure(connection, opts={"include_object": _include_owned_tables})
    return cast(list[Any], compare_metadata(context, Base.metadata))


def test_fresh_database_matches_models_and_migration_is_idempotent(empty_postgres_url: str, tmp_path: Path) -> None:
    history = tmp_path / "100% complete"
    history.symlink_to(RUNNER.migrations_dir.resolve(), target_is_directory=True)
    replace(RUNNER, migrations_dir=history).apply(empty_postgres_url)
    RUNNER.apply(empty_postgres_url)

    engine = create_engine(RUNNER.sync_url(empty_postgres_url))
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


@pytest.mark.parametrize(
    ("url", "drivername"),
    [
        ("sqlite:///:memory:", "sqlite"),
        ("postgresql+psycopg://archive_user:archive_password@postgres.example:5432/archive", "postgresql+psycopg"),
    ],
)
def test_make_engine_preserves_the_driver_of_a_url_that_names_one(url: str, drivername: str) -> None:
    engine = make_engine(url)
    try:
        assert engine.url.drivername == drivername
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()

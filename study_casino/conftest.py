"""Shared pytest fixtures for the Study Casino test suite."""

from __future__ import annotations

from collections.abc import Generator, Iterator

import pytest
from sqlalchemy import create_engine

from study_casino.store import _run_alembic_migrations
from util.testing.postgres import (
    DatabaseClones,
    DatabaseNames,
    PostgresDatabaseTemplate,
    create_database_sync,
    force_drop_database_sync,
)
from util.testing.postgres_fixtures import (
    database_clones,
    database_names,
    postgres_admin_url,
    postgres_container,
    postgres_image,
)


@pytest.fixture(scope="session")
def casino_database_template(postgres_admin_url: str) -> Iterator[PostgresDatabaseTemplate]:
    """Migrate one template; runtime stores retain their normal startup schema check on each clone."""
    with PostgresDatabaseTemplate(postgres_admin_url, "casino_template") as template:
        engine = create_engine(template.url)
        try:
            _run_alembic_migrations(engine)
        finally:
            engine.dispose()
        yield template


@pytest.fixture
def db_url(casino_database_template: PostgresDatabaseTemplate, database_clones: DatabaseClones) -> str:
    """A fresh writable clone for ordinary store and app tests."""
    return database_clones.clone(casino_database_template, drivername="postgresql+psycopg")


@pytest.fixture
def empty_db_url(postgres_admin_url: str, database_names: DatabaseNames) -> Generator[str]:
    """An empty database for the migration test that deliberately starts at revision 0004."""
    db_name = database_names()
    url = create_database_sync(postgres_admin_url, db_name)
    try:
        yield url
    finally:
        force_drop_database_sync(postgres_admin_url, db_name)

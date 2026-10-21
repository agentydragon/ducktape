"""The egress history database."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from util.testing.postgres import DatabaseNames, create_temporary_database
from util.testing.postgres_fixtures import database_names, postgres_admin_url, postgres_container, postgres_image


@pytest.fixture
def history_db_url(postgres_admin_url: str, database_names: DatabaseNames) -> Iterator[str]:
    """An empty database that the Go replica's migrator owns."""
    with create_temporary_database(postgres_admin_url, database_names()) as url:
        yield url

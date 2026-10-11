"""Postgres database fixture wiring for the Spend Service tests."""

from __future__ import annotations

import pytest

from finance.plaid.db.testing import db_url, plaid_database_template
from util.db_url import render_postgresql_url
from util.testing.postgres_fixtures import (
    database_clones,
    database_names,
    postgres_admin_url,
    postgres_container,
    postgres_image,
)


@pytest.fixture
def postgres_url(db_url: str) -> str:
    """Return the cloned Plaid database URL in the form asyncpg accepts directly."""
    return render_postgresql_url(db_url, "postgresql")

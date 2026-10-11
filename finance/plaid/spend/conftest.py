"""Postgres database fixture wiring for the Spend Service tests."""

from __future__ import annotations

import pytest
from sqlalchemy.engine import make_url

from finance.plaid.db.testing import db_url, plaid_database_template
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
    return make_url(db_url).set(drivername="postgresql").render_as_string(hide_password=False)

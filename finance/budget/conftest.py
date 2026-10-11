"""Reuse the Plaid schema and migrated-template test fixtures."""

from finance.plaid.db.testing import db_url, plaid_database_template
from util.testing.postgres_fixtures import (
    database_clones,
    database_names,
    postgres_admin_url,
    postgres_container,
    postgres_image,
)

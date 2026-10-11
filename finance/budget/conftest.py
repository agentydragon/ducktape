"""Reuse the Plaid schema and migrated-template test fixtures."""

from finance.plaid.db.testing import plaid_database_template, plaid_database_url
from util.testing.postgres_fixtures import database_clones, database_names, postgres_admin_url, postgres_container

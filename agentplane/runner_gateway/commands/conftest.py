"""Isolated, migrated PostgreSQL fixture for the command admission store."""

from agentplane.runner_gateway.commands.database_migrate import RUNNER
from util.testing.postgres_fixtures import (
    database_clones,
    database_names,
    engine,
    migrated_database_fixtures,
    postgres_admin_url,
    postgres_container,
    postgres_image,
)

# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//psycopg


database_template, db_url = migrated_database_fixtures(RUNNER, drivername="postgresql+asyncpg")

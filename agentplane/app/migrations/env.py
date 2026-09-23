"""Alembic environment for the integration app's schema."""

from alembic import context

from agentplane.app.database_migrate import RUNNER

connection = context.config.attributes["connection"]
target_metadata = context.config.attributes.get("target_metadata", RUNNER.metadata)
context.configure(connection=connection, target_metadata=target_metadata, version_table=RUNNER.version_table)
with context.begin_transaction():
    context.run_migrations()

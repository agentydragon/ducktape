"""Alembic environment for the AI session archive."""

from alembic import context

from x.ai_session_archive.database_migrate import RUNNER

connection = context.config.attributes["connection"]
target_metadata = context.config.attributes.get("target_metadata", RUNNER.metadata)
context.configure(connection=connection, target_metadata=target_metadata, version_table=RUNNER.version_table)
with context.begin_transaction():
    context.run_migrations()

"""Alembic environment for the session mirror."""

from alembic import context

from devinfra.claude.session_export.store import Base

connection = context.config.attributes["connection"]
target_metadata = context.config.attributes.get("target_metadata", Base.metadata)
context.configure(connection=connection, target_metadata=target_metadata)
with context.begin_transaction():
    context.run_migrations()

"""Alembic environment for the session mirror."""

from alembic import context

from devinfra.claude.session_export.store import Base

context.configure(connection=context.config.attributes["connection"], target_metadata=Base.metadata)
with context.begin_transaction():
    context.run_migrations()

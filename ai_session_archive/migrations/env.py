"""Alembic environment for the AI session archive."""

from alembic import context

from ai_session_archive.database import Base

# The archive already uses this stamp table in its shared database.
context.configure(
    connection=context.config.attributes["connection"],
    target_metadata=Base.metadata,
    version_table="alembic_version_ai_session_archive",
)
with context.begin_transaction():
    context.run_migrations()

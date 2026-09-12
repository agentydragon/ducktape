"""Alembic environment for the integration app's schema."""

from alembic import context

from x.agentplane.app.database_migrate import VERSION_TABLE
from x.agentplane.app.trajectory import Base

connection = context.config.attributes["connection"]
target_metadata = context.config.attributes.get("target_metadata", Base.metadata)
context.configure(connection=connection, target_metadata=target_metadata, version_table=VERSION_TABLE)
with context.begin_transaction():
    context.run_migrations()

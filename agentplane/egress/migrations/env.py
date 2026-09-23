"""Alembic environment for the egress history schema."""

from alembic import context

from agentplane.egress.decision_store import Base

connection = context.config.attributes["connection"]
target_metadata = context.config.attributes.get("target_metadata", Base.metadata)
context.configure(connection=connection, target_metadata=target_metadata, version_table="egress_alembic_version")
with context.begin_transaction():
    context.run_migrations()

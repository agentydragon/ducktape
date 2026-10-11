"""Command admission Alembic environment."""

from alembic import context

from agentplane.runner_gateway.commands.db import Base

context.configure(connection=context.config.attributes["connection"], target_metadata=Base.metadata)
with context.begin_transaction():
    context.run_migrations()

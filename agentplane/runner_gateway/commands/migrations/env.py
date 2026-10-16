"""Command admission Alembic environment."""

from alembic import context

from agentplane.runner_gateway.commands.db import metadata

context.configure(connection=context.config.attributes["connection"], target_metadata=metadata)
with context.begin_transaction():
    context.run_migrations()

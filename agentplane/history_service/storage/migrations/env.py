"""Session history Alembic environment."""

from alembic import context

from agentplane.history_service.storage.database_migrate import RUNNER

context.configure(
    connection=context.config.attributes["connection"],
    target_metadata=context.config.attributes.get("target_metadata", RUNNER.metadata),
)
with context.begin_transaction():
    context.run_migrations()

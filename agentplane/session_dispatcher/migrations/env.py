"""Session Dispatcher Alembic environment."""

from alembic import context

from agentplane.session_dispatcher.db import Base

context.configure(connection=context.config.attributes["connection"], target_metadata=Base.metadata)
with context.begin_transaction():
    context.run_migrations()

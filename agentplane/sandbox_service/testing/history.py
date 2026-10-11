"""Test-only History + Commands schema setup.

The History and Commands production Alembic runners use the same default ``alembic_version`` table,
so a combined test database must not apply both runners. Build the test schema with SQLAlchemy
metadata once in a PostgreSQL template instead; the template does not create migration stamps.
"""

from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.history_service.storage.db import Base
from agentplane.runner_gateway.commands import db as commands_db
from agentplane.thread_fold.models import Base as FoldBase


async def initialize_service_database(engine: AsyncEngine) -> None:
    """Create History, Commands, and fold test schemas on an empty template database."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        # Production keeps admission in its own database; one test database serves both here.
        await connection.run_sync(commands_db.Base.metadata.create_all)
        await connection.run_sync(FoldBase.metadata.create_all)

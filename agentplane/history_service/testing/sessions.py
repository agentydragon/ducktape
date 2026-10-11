"""History rows for tests: a pushed Session's writer, and the rows the retired follower left."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from agentplane.history_service.storage.db import SessionHistory
from agentplane.history_service.storage.store import Writer

WRITER = Writer(namespace="test-sandbox-namespace", name="test-sandbox-runner", uid="test-runner-account-uid")


async def record_follower_session(
    engine: AsyncEngine, session_id: UUID, *, sandbox_namespace: str, sandbox_name: str, runner_session_id: str
) -> None:
    """A row the retired follower created: it names its Sandbox and runner session instead of a writer."""
    async with AsyncSession(engine) as session, session.begin():
        session.add(
            SessionHistory(
                id=session_id,
                sandbox_namespace=sandbox_namespace,
                sandbox_name=sandbox_name,
                runner_session_id=runner_session_id,
                last_cursor=0,
            )
        )

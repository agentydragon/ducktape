"""Register a Session with a chosen public ID and runner locator and no Open reservation, as Sessions
registered before `CreateSession` existed are."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.runner_gateway.commands.db import Session


async def register_session(
    engine: AsyncEngine,
    session_id: UUID,
    *,
    sandbox_namespace: str,
    sandbox_name: str,
    sandbox_uid: UUID | None,
    runner_session_id: str,
) -> None:
    async with async_sessionmaker(engine, expire_on_commit=False).begin() as session:
        session.add(
            Session(
                id=session_id,
                sandbox_namespace=sandbox_namespace,
                sandbox_name=sandbox_name,
                sandbox_uid=sandbox_uid,
                runner_session_id=runner_session_id,
            )
        )

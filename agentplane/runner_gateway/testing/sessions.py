"""Register a Session with a chosen ID and no Open reservation."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.runner_gateway.commands.db import Session
from agentplane.sandboxes.testing.kubernetes import SANDBOX, SANDBOX_UID
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE


async def register_session(
    engine: AsyncEngine, session_id: UUID, *, sandbox_namespace: str, sandbox_name: str, sandbox_uid: UUID | None
) -> None:
    async with async_sessionmaker(engine, expire_on_commit=False).begin() as session:
        session.add(
            Session(
                id=session_id, sandbox_namespace=sandbox_namespace, sandbox_name=sandbox_name, sandbox_uid=sandbox_uid
            )
        )


async def register_on_fake_sandbox(engine: AsyncEngine, session_id: UUID) -> None:
    """Register a Session on the fake cluster's Sandbox."""
    await register_session(
        engine, session_id, sandbox_namespace=SANDBOX_NAMESPACE, sandbox_name=SANDBOX, sandbox_uid=UUID(SANDBOX_UID)
    )

"""Explicit retained app identity rows for compatibility tests."""

from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.app.threads.models import EventLog


async def seed_retained_session(engine: AsyncEngine, *, sandbox: str = "sb-1", public_id: UUID | None = None) -> UUID:
    thread = public_id if public_id is not None else uuid4()
    async with async_sessionmaker(engine).begin() as session:
        session.add(EventLog(id=thread, sandbox=sandbox))
    return thread

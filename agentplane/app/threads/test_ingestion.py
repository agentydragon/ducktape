"""Current app projection lease ownership, without a retired raw writer."""

import asyncio
from datetime import timedelta
from uuid import UUID

import pytest
import pytest_bazel
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from agentplane.app.testing.retained_history import seed_retained_session
from agentplane.app.threads.events.projection_lease import ProjectionLease, ProjectionLeaseLostError, fence
from agentplane.app.threads.ingestion import Ingestion
from agentplane.app.threads.models import SandboxProjectionLease


@pytest.fixture
def ingestion(engine: AsyncEngine) -> Ingestion:
    return Ingestion(engine)


async def check_fence(engine: AsyncEngine, lease: ProjectionLease, thread: UUID) -> None:
    async with async_sessionmaker(engine).begin() as session:
        await fence(session, lease, thread)


async def test_fence_rechecks_expiry_after_waiting_for_the_lease_row(
    engine: AsyncEngine, lease: ProjectionLease, db_url: str
) -> None:
    thread = await seed_retained_session(engine)
    engine = create_async_engine(db_url)
    write: asyncio.Task[None] | None = None
    try:
        async with engine.begin() as connection:
            await connection.execute(
                select(SandboxProjectionLease).where(SandboxProjectionLease.sandbox == lease.sandbox).with_for_update()
            )
            write = asyncio.create_task(check_fence(engine, lease, thread))
            async with asyncio.timeout(5):
                while True:
                    await connection.execute(text("SELECT pg_stat_clear_snapshot()"))
                    waiting = await connection.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                            "AND wait_event_type = 'Lock' AND query LIKE 'SELECT sandbox_ingestion.%'"
                        )
                    )
                    if waiting:
                        break
            # Expire after record's transaction began. Transaction-start now() would accept the
            # write; clock_timestamp() checked after the lock must reject it.
            await connection.execute(
                update(SandboxProjectionLease)
                .where(SandboxProjectionLease.sandbox == lease.sandbox)
                .values(expires_at=func.clock_timestamp())
            )
        with pytest.raises(ProjectionLeaseLostError):
            await write
    finally:
        if write is not None and not write.done():
            write.cancel()
            await asyncio.gather(write, return_exceptions=True)
        await engine.dispose()


async def test_concurrent_replicas_choose_one_owner(engine: AsyncEngine) -> None:
    first, second = await asyncio.gather(
        Ingestion(engine).acquire("sb-1", timedelta(minutes=1)), Ingestion(engine).acquire("sb-1", timedelta(minutes=1))
    )
    assert (first is None) != (second is None)


async def test_only_current_lease_can_fence_or_renew(
    engine: AsyncEngine, ingestion: Ingestion, lease: ProjectionLease
) -> None:
    thread = await seed_retained_session(engine)
    replica = Ingestion(engine)
    assert await replica.acquire("sb-1", timedelta(minutes=1)) is None
    assert await ingestion.renew(lease, timedelta(minutes=2))
    async with engine.begin() as connection:
        await connection.execute(
            update(SandboxProjectionLease)
            .where(SandboxProjectionLease.sandbox == "sb-1")
            .values(expires_at=func.clock_timestamp() - timedelta(seconds=1))
        )
    assert not await ingestion.renew(lease, timedelta(minutes=1))
    successor = await replica.acquire("sb-1", timedelta(minutes=1))
    assert successor is not None
    assert successor.token != lease.token
    await ingestion.release(lease)
    assert await replica.renew(successor, timedelta(minutes=1))
    with pytest.raises(ProjectionLeaseLostError):
        await check_fence(engine, lease, thread)
    await check_fence(engine, successor, thread)
    other = await seed_retained_session(engine, sandbox="sb-2")
    with pytest.raises(ProjectionLeaseLostError):
        await check_fence(engine, successor, other)
    await replica.release(successor)
    assert await ingestion.acquire("sb-1", timedelta(minutes=1)) is not None


if __name__ == "__main__":
    pytest_bazel.main()

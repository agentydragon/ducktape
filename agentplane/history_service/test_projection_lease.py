"""Per-Session fold leases over a migrated history database: one owner, and a fence that rechecks it."""

import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from agentplane.history_service.fold import projection_lease
from agentplane.history_service.fold.models import SessionProjectionLease
from agentplane.history_service.fold.projection_lease import ProjectionLease, ProjectionLeaseLostError

# gazelle:include_dep //agentplane/history_service/storage:conftest

pytest_plugins = ("agentplane.history_service.storage.conftest",)


async def acquire(engine: AsyncEngine, session_id: UUID) -> ProjectionLease | None:
    async with async_sessionmaker(engine).begin() as session:
        return await projection_lease.acquire(session, session_id, timedelta(minutes=1))


async def renew(engine: AsyncEngine, lease: ProjectionLease) -> bool:
    async with async_sessionmaker(engine).begin() as session:
        return await projection_lease.renew(session, lease, timedelta(minutes=1))


async def release(engine: AsyncEngine, lease: ProjectionLease) -> None:
    async with async_sessionmaker(engine).begin() as session:
        await projection_lease.release(session, lease)


async def fence(engine: AsyncEngine, lease: ProjectionLease, session_id: UUID) -> None:
    async with async_sessionmaker(engine).begin() as session:
        await projection_lease.fence(session, lease, session_id)


async def test_fence_rechecks_expiry_after_waiting_for_the_lease_row(engine: AsyncEngine, db_url: str) -> None:
    session_id = uuid4()
    lease = await acquire(engine, session_id)
    assert lease is not None
    other = create_async_engine(db_url)
    write: asyncio.Task[None] | None = None
    try:
        async with other.begin() as connection:
            await connection.execute(
                select(SessionProjectionLease).where(SessionProjectionLease.session_id == session_id).with_for_update()
            )
            write = asyncio.create_task(fence(engine, lease, session_id))
            async with asyncio.timeout(5):
                while True:
                    await connection.execute(text("SELECT pg_stat_clear_snapshot()"))
                    waiting = await connection.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                            "AND wait_event_type = 'Lock' AND query LIKE 'SELECT session_projection_lease.%'"
                        )
                    )
                    if waiting:
                        break
            # Expire after the fenced transaction began. Transaction-start now() would accept the
            # write; clock_timestamp() checked after the lock must reject it.
            await connection.execute(
                update(SessionProjectionLease)
                .where(SessionProjectionLease.session_id == session_id)
                .values(expires_at=func.clock_timestamp())
            )
        with pytest.raises(ProjectionLeaseLostError):
            await write
    finally:
        if write is not None and not write.done():
            write.cancel()
            await asyncio.gather(write, return_exceptions=True)
        await other.dispose()


async def test_concurrent_replicas_choose_one_owner(engine: AsyncEngine) -> None:
    session_id = uuid4()
    first, second = await asyncio.gather(acquire(engine, session_id), acquire(engine, session_id))
    assert (first is None) != (second is None)


async def test_only_current_lease_can_fence_or_renew(engine: AsyncEngine) -> None:
    session_id = uuid4()
    lease = await acquire(engine, session_id)
    assert lease is not None
    assert await acquire(engine, session_id) is None
    assert await renew(engine, lease)
    async with engine.begin() as connection:
        await connection.execute(
            update(SessionProjectionLease)
            .where(SessionProjectionLease.session_id == session_id)
            .values(expires_at=func.clock_timestamp() - timedelta(seconds=1))
        )
    assert not await renew(engine, lease)
    successor = await acquire(engine, session_id)
    assert successor is not None
    assert successor.token != lease.token
    await release(engine, lease)
    assert await renew(engine, successor)
    with pytest.raises(ProjectionLeaseLostError):
        await fence(engine, lease, session_id)
    await fence(engine, successor, session_id)
    with pytest.raises(ProjectionLeaseLostError):
        await fence(engine, successor, uuid4())
    await release(engine, successor)
    assert await acquire(engine, session_id) is not None


if __name__ == "__main__":
    pytest_bazel.main()

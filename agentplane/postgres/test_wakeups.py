"""Commit-scoped NOTIFY reaching every replica's readers of that channel, and reconnects waking all."""

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_bazel
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.postgres.wakeups import WakeupListener, notify
from util.testing.sqlalchemy_engines import postgres_async_engine

FIRST, SECOND = "wakeups_test_first", "wakeups_test_second"


@pytest.fixture
async def engine(postgres_admin_url: str) -> AsyncIterator[AsyncEngine]:
    async with postgres_async_engine(postgres_admin_url) as engine:
        yield engine


async def test_commits_wake_their_channel_on_every_replica(postgres_admin_url: str, engine: AsyncEngine) -> None:
    url = make_url(postgres_admin_url)
    replicas = [
        WakeupListener(url, channels=(FIRST, SECOND), application_name=f"wakeups-test-{uuid4().hex}") for _ in "ab"
    ]
    first_a, first_b, second_a = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async with replicas[0].listener.listen(), replicas[1].listener.listen():
        with (
            replicas[0].changes[FIRST].subscribe(first_a),
            replicas[1].changes[FIRST].subscribe(first_b),
            replicas[0].changes[SECOND].subscribe(second_a),
        ):
            async with async_sessionmaker(engine).begin() as session:
                await notify(session, FIRST)
                await session.rollback()
            async with async_sessionmaker(engine).begin() as session:
                await notify(session, FIRST)
                assert not first_a.is_set()
            async with asyncio.timeout(10):
                await first_a.wait()
                await first_b.wait()
            assert not second_a.is_set()


async def test_reconnect_wakes_every_channel(postgres_admin_url: str, engine: AsyncEngine) -> None:
    name = f"wakeups-test-{uuid4().hex}"
    wakeups = WakeupListener(make_url(postgres_admin_url), channels=(FIRST, SECOND), application_name=name)
    first, second = asyncio.Event(), asyncio.Event()
    async with wakeups.listener.listen():
        with wakeups.changes[FIRST].subscribe(first), wakeups.changes[SECOND].subscribe(second):
            async with engine.begin() as connection:
                await connection.execute(
                    text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = :name"),
                    {"name": name},
                )
            async with asyncio.timeout(10):
                await first.wait()
                await second.wait()


if __name__ == "__main__":
    pytest_bazel.main()

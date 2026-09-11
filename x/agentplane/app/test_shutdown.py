"""The drain ends a stream where it waits, and leaves an exhausted one alone."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest_bazel

from x.agentplane.app.shutdown import Drain


async def test_the_drain_ends_a_stream_at_its_wait_and_closes_its_source() -> None:
    drain = Drain()
    first_seen = asyncio.Event()
    closed = asyncio.Event()
    items: list[int] = []

    async def source() -> AsyncIterator[int]:
        try:
            yield 1
            await asyncio.Event().wait()
            yield 2
        finally:
            closed.set()

    async def consume() -> None:
        async for item in drain.until(source()):
            items.append(item)
            first_seen.set()

    consumer = asyncio.create_task(consume())
    await first_seen.wait()
    assert not closed.is_set()
    drain.begin()
    await consumer

    assert (items, closed.is_set()) == ([1], True)


async def test_a_stream_that_ends_on_its_own_is_passed_through_whole() -> None:
    async def source() -> AsyncIterator[int]:
        yield 1
        yield 2

    assert [item async for item in Drain().until(source())] == [1, 2]


if __name__ == "__main__":
    pytest_bazel.main()

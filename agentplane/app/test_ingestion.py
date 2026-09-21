"""Batch flushes preserve the stream reader and bound queued events."""

import asyncio
from collections import deque
from contextlib import aclosing

import pytest_bazel

from agentplane.app.ingestion import event_batches
from agentplane.protocol import event_log_pb2
from agentplane.runner.client import StreamClosedError

# gazelle:include_dep @pypi//protobuf


async def test_full_batches_and_eof_preserve_order() -> None:
    source = deque(event_log_pb2.EventEntry(cursor=i) for i in range(1, 302))

    async def read() -> event_log_pb2.EventEntry:
        if not source:
            raise StreamClosedError
        return source.popleft()

    batches = [batch async for batch in event_batches(read, limit=128, delay_s=60)]
    assert [len(batch) for batch in batches] == [128, 128, 45]
    assert [entry.cursor for batch in batches for entry in batch] == list(range(1, 302))


async def test_deadline_flush_does_not_cancel_or_replace_pending_read() -> None:
    gate = asyncio.Event()
    waiting = asyncio.Event()
    reads = 0
    cancellations = 0

    async def read() -> event_log_pb2.EventEntry:
        nonlocal reads, cancellations
        reads += 1
        if reads == 1:
            return event_log_pb2.EventEntry(cursor=1)
        if reads == 2:
            waiting.set()
            try:
                await gate.wait()
            except asyncio.CancelledError:
                cancellations += 1
                raise
            return event_log_pb2.EventEntry(cursor=2)
        raise StreamClosedError

    async with aclosing(event_batches(read, delay_s=0.001)) as batches:
        first = await asyncio.wait_for(anext(batches), timeout=2)
        await waiting.wait()
        assert [entry.cursor for entry in first] == [1]
        assert reads == 2
        assert cancellations == 0
        gate.set()
        remaining = [entry.cursor async for batch in batches for entry in batch]
        assert remaining == [2]
    assert cancellations == 0


async def test_slow_consumer_has_only_one_read_ahead_and_close_cancels_it() -> None:
    count = 0
    waiting = asyncio.Event()
    cancelled = asyncio.Event()

    async def read() -> event_log_pb2.EventEntry:
        nonlocal count
        count += 1
        if count <= 4:
            return event_log_pb2.EventEntry(cursor=count)
        waiting.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    async with aclosing(event_batches(read, limit=4, delay_s=60)) as batches:
        page = await anext(batches)
        await waiting.wait()
        assert len(page) == 4
        assert count == 5
    assert cancelled.is_set()


if __name__ == "__main__":
    pytest_bazel.main()

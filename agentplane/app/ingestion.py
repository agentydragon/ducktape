"""Bounded event batching with one persistent transport read across flush deadlines."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

from agentplane.protocol import event_log_pb2
from agentplane.runner.client import StreamClosedError

# gazelle:include_dep @pypi//protobuf


async def event_batches(
    read: Callable[[], Awaitable[event_log_pb2.EventEntry]], *, limit: int = 128, delay_s: float = 0.025
) -> AsyncIterator[list[event_log_pb2.EventEntry]]:
    """Flush by count, elapsed batch delay, or EOF; never cancel a read to flush a batch."""
    if limit < 1 or delay_s <= 0:
        raise ValueError("batch size and delay must be positive")
    loop = asyncio.get_running_loop()
    pending = asyncio.ensure_future(read())
    try:
        while True:
            try:
                batch = [await pending]
            except StreamClosedError:
                return
            deadline = loop.time() + delay_s
            pending = asyncio.ensure_future(read())
            while len(batch) < limit:
                done, _ = await asyncio.wait({pending}, timeout=max(0, deadline - loop.time()))
                if not done:
                    break
                try:
                    batch.append(pending.result())
                except StreamClosedError:
                    yield batch
                    return
                pending = asyncio.ensure_future(read())
            yield batch
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)

"""Terminal app state must not truncate a service-backed stream during catch-up."""

import asyncio
from contextlib import aclosing
from typing import cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_bazel

from agentplane.app.changes import Changes
from agentplane.app.threads.events.event_log import EventLogStore, FeedEnd, FeedSnapshot, RunnerSession
from agentplane.app.threads.events.stream import follow
from agentplane.protocol import event_log_pb2
from agentplane.runner import protocol_pb2

# gazelle:include_dep @pypi//protobuf


async def test_terminal_stream_waits_without_spinning_for_service_suffix() -> None:
    logs = AsyncMock(spec=EventLogStore)
    logs.runner_session.return_value = RunnerSession("sb", "s-1")
    logs.feed_state.return_value = FeedSnapshot(protocol_pb2.Attached(last_cursor=1), FeedEnd())
    logs.last_cursor.return_value = 1
    logs.events.side_effect = [[], [event_log_pb2.EventEntry(cursor=1)], []]
    with patch("agentplane.app.threads.events.stream.KEEPALIVE_S", 0.01):
        async with aclosing(follow(cast(EventLogStore, logs), Changes(), uuid4(), after_cursor=0)) as frames:
            async with asyncio.timeout(1):
                assert b"event: attached" in await anext(frames)
                assert await anext(frames) == b": keepalive\n\n"
                assert logs.events.await_count == 1
                assert b"id: 1" in await anext(frames)
                assert b"event: end" in await anext(frames)
                with pytest.raises(StopAsyncIteration):
                    await anext(frames)


if __name__ == "__main__":
    pytest_bazel.main()

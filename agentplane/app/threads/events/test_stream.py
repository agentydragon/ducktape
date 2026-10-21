"""Terminal app state must not truncate a service-backed stream during catch-up."""

import asyncio
from contextlib import aclosing
from typing import cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_bazel

from agentplane.app.threads.events.event_log import EventLogStore
from agentplane.app.threads.events.stream import follow
from agentplane.app.threads.summaries import FeedEnd, FeedSnapshot
from agentplane.changes import ChangeSignal
from agentplane.proto import command_pb2, event_log_pb2, event_pb2, runner_pb2

# gazelle:include_dep @pypi//protobuf


async def test_terminal_stream_waits_without_spinning_for_service_suffix() -> None:
    logs = AsyncMock(spec=EventLogStore)
    logs.exists.return_value = True
    logs.feed_state.return_value = FeedSnapshot(runner_pb2.Attached(last_cursor=1), FeedEnd())
    logs.last_cursor.return_value = 1
    logs.events.side_effect = [[], [event_log_pb2.EventEntry(cursor=1)], []]
    with patch("agentplane.app.threads.events.stream.KEEPALIVE_S", 0.01):
        async with aclosing(follow(cast(EventLogStore, logs), ChangeSignal(), uuid4(), after_cursor=0)) as frames:
            async with asyncio.timeout(1):
                assert b"event: attached" in await anext(frames)
                assert await anext(frames) == b": keepalive\n\n"
                assert logs.events.await_count == 1
                assert b"id: 1" in await anext(frames)
                assert b"event: end" in await anext(frames)
                with pytest.raises(StopAsyncIteration):
                    await anext(frames)


async def test_stream_does_not_end_on_a_terminal_snapshot_behind_streamed_entries() -> None:
    """A runner attached before its harness started can record an end that later entries supersede.
    The archive can stream those entries before the fold clears that end."""
    logs = AsyncMock(spec=EventLogStore)
    logs.exists.return_value = True
    stale = FeedSnapshot(runner_pb2.Attached(last_cursor=1), FeedEnd())
    current = FeedSnapshot(runner_pb2.Attached(last_cursor=2), FeedEnd())
    logs.feed_state.side_effect = [stale, stale, current, current]
    logs.last_cursor.side_effect = [1, 2]
    logs.events.side_effect = [[event_log_pb2.EventEntry(cursor=1), event_log_pb2.EventEntry(cursor=2)], [], []]
    with patch("agentplane.app.threads.events.stream.KEEPALIVE_S", 0.01):
        async with aclosing(follow(cast(EventLogStore, logs), ChangeSignal(), uuid4(), after_cursor=0)) as frames:
            async with asyncio.timeout(1):
                assert b"event: attached" in await anext(frames)
                assert b"id: 1" in await anext(frames)
                assert b"id: 2" in await anext(frames)
                assert await anext(frames) == b": keepalive\n\n"
                assert b"event: end" in await anext(frames)


async def test_stream_does_not_end_before_the_awaited_command_is_admitted() -> None:
    """The Dispatcher answers a submission before History archives its admission, and until then a
    Session whose harness exited reads as ended."""
    archive = [event_log_pb2.EventEntry(cursor=1, event=event_pb2.Event(harness_exited=event_pb2.HarnessExited()))]
    logs = AsyncMock(spec=EventLogStore)
    logs.exists.return_value = True
    logs.feed_state.side_effect = lambda _: FeedSnapshot(runner_pb2.Attached(last_cursor=len(archive)), FeedEnd())
    logs.last_cursor.side_effect = lambda _: len(archive)
    logs.events.side_effect = lambda _, *, after_cursor, limit: archive[after_cursor:]
    with patch("agentplane.app.threads.events.stream.KEEPALIVE_S", 0.01):
        async with aclosing(
            follow(cast(EventLogStore, logs), ChangeSignal(), uuid4(), after_cursor=0, awaiting_command_id="test-input")
        ) as frames:
            async with asyncio.timeout(1):
                assert b"event: attached" in await anext(frames)
                assert b"id: 1" in await anext(frames)
                assert await anext(frames) == b": keepalive\n\n"
                archive.append(
                    event_log_pb2.EventEntry(
                        cursor=2,
                        event=event_pb2.Event(
                            command_admitted=event_pb2.CommandAdmitted(
                                command=command_pb2.Command(command_id="test-input")
                            )
                        ),
                    )
                )
                assert b"id: 2" in await anext(frames)
                assert b"event: end" in await anext(frames)


if __name__ == "__main__":
    pytest_bazel.main()

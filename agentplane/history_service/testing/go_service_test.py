"""Readiness waits distinguish recorded Sessions from announced snapshots."""

import asyncio
from unittest.mock import AsyncMock

import grpc
import pytest

from agentplane.grpc.calls import ServiceError
from agentplane.history_service.api.history import history_service_pb2
from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.testing.go_service import wait_for_history_snapshot
from agentplane.proto import session_pb2

# gazelle:include_dep @pypi//protobuf


@pytest.fixture
def history() -> HistoryServiceClient:
    return HistoryServiceClient("unused", request_timeout_s=1, channel_credentials=grpc.ssl_channel_credentials())


async def test_snapshot_waits_past_recording_until_announcement(
    history: HistoryServiceClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    announced = history_service_pb2.ReadSessionEventsResponse(
        last_cursor=7,
        feed_state=history_service_pb2.SessionFeedState(
            attached=session_pb2.Attached(
                session_id="test-session", last_cursor=3, spec=session_pb2.SessionSpec(instructions="test task")
            )
        ),
    )
    pages = iter(
        [
            history_service_pb2.ReadSessionEventsResponse(),
            history_service_pb2.ReadSessionEventsResponse(feed_state=history_service_pb2.SessionFeedState()),
            announced,
        ]
    )
    reading_announcement = asyncio.Event()
    release_announcement = asyncio.Event()

    async def read(session_id: str, *, after_cursor: int, limit: int) -> history_service_pb2.ReadSessionEventsResponse:
        assert (session_id, after_cursor, limit) == ("test-session", 0, 1)
        page = next(pages)
        if page is announced:
            reading_announcement.set()
            await release_announcement.wait()
        return page

    monkeypatch.setattr(history, "read_session_events", read)
    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        snapshot = tasks.create_task(wait_for_history_snapshot(history, "test-session", timeout_s=5))
        await reading_announcement.wait()
        assert not snapshot.done()
        release_announcement.set()
    assert snapshot.result().session_id == "test-session"
    assert snapshot.result().spec.instructions == "test task"
    assert snapshot.result().last_cursor == 7
    assert announced.feed_state.attached.last_cursor == 3


@pytest.mark.parametrize("code", [grpc.StatusCode.NOT_FOUND, grpc.StatusCode.PERMISSION_DENIED])
async def test_snapshot_wait_propagates_read_errors(
    history: HistoryServiceClient, monkeypatch: pytest.MonkeyPatch, code: grpc.StatusCode
) -> None:
    error = ServiceError(code, "test read rejected")
    read = AsyncMock(side_effect=error)
    monkeypatch.setattr(history, "read_session_events", read)
    with pytest.raises(ServiceError) as rejected:
        await wait_for_history_snapshot(history, "test-session", timeout_s=5)
    assert rejected.value is error
    assert read.await_count == 1


@pytest.mark.parametrize("block_read", [False, True])
async def test_snapshot_wait_deadline_covers_missing_announcement_and_blocked_reads(
    history: HistoryServiceClient, monkeypatch: pytest.MonkeyPatch, block_read: bool
) -> None:
    cancelled = asyncio.Event()
    never_announced = asyncio.Event()

    async def read(session_id: str, *, after_cursor: int, limit: int) -> history_service_pb2.ReadSessionEventsResponse:
        if block_read:
            try:
                await never_announced.wait()
            finally:
                cancelled.set()
        return history_service_pb2.ReadSessionEventsResponse()

    monkeypatch.setattr(history, "read_session_events", read)
    with pytest.raises(TimeoutError):
        await wait_for_history_snapshot(history, "test-session", timeout_s=0.01)
    assert cancelled.is_set() == block_read

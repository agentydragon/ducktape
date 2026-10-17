"""A Thread's dispatch stream, against a Dispatcher that holds rows and says when they change."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import override
from uuid import UUID, uuid4

import grpc
import pytest
import pytest_bazel

from agentplane.app.threads.dispatch_stream import DispatchesSnapshot, dispatch_frames
from agentplane.app.threads.dispatcher_client import SessionDispatcherClient
from agentplane.grpc.calls import ServiceError, dial
from agentplane.grpc.serving import serving
from agentplane.proto import session_dispatcher_pb2, session_dispatcher_pb2_grpc

# Generated protobuf typing imports google.protobuf through transitive stubs.
# gazelle:include_dep @pypi//protobuf

INTERVAL_S = 0.05
THREAD = uuid4()


class FakeDispatcher(session_dispatcher_pb2_grpc.SessionDispatcherServicer):
    def __init__(self) -> None:
        self.rows: dict[str, session_dispatcher_pb2.Dispatch] = {}
        self.changed = asyncio.Event()
        self.watch_failure: grpc.StatusCode | None = None

    def hold(self, command_id: str, *, session: UUID = THREAD, state: session_dispatcher_pb2.DeliveryState) -> None:
        self.rows[command_id] = session_dispatcher_pb2.Dispatch(
            command_id=command_id, session_id=str(session), state=state
        )

    @override
    async def ListDispatches(self, request, _context):
        return session_dispatcher_pb2.ListDispatchesResponse(
            dispatches=[row for row in self.rows.values() if row.session_id == request.filter.session_id]
        )

    @override
    async def WatchDispatches(self, _request, context):
        if self.watch_failure is not None:
            await context.abort(self.watch_failure, "dispatcher unavailable")
        while True:
            await self.changed.wait()
            self.changed.clear()
            yield session_dispatcher_pb2.WatchDispatchesResponse()


@asynccontextmanager
async def serve(tmp_path: Path, dispatcher: FakeDispatcher) -> AsyncIterator[SessionDispatcherClient]:
    token = tmp_path / "token"
    await asyncio.to_thread(token.write_text, "test-token")
    server = grpc.aio.server()
    session_dispatcher_pb2_grpc.add_SessionDispatcherServicer_to_server(dispatcher, server)
    async with serving(server, "127.0.0.1:0", grace_s=0) as port, dial(f"127.0.0.1:{port}") as channel:
        yield SessionDispatcherClient(channel, token_file=token, request_timeout_s=5)


def parse(chunk: bytes) -> tuple[str, str]:
    """The frame's event name and its JSON data."""
    event, data = chunk.decode().strip().split("\n")
    return event.removeprefix("event: "), data.removeprefix("data: ")


async def drain(frames: AsyncIterator[bytes]) -> None:
    async with asyncio.timeout(10):
        async for _ in frames:
            pass


async def test_snapshot_lists_each_asked_command_the_dispatcher_holds_for_this_thread(tmp_path: Path) -> None:
    dispatcher = FakeDispatcher()
    dispatcher.hold("queued", state=session_dispatcher_pb2.DELIVERY_STATE_QUEUED_IN_DISPATCHER)
    dispatcher.hold("other-thread", session=uuid4(), state=session_dispatcher_pb2.DELIVERY_STATE_QUEUED_IN_DISPATCHER)
    async with serve(tmp_path, dispatcher) as client:
        frames = dispatch_frames(client, THREAD, ["queued", "other-thread", "deleted"], interval_s=INTERVAL_S)
        async with asyncio.timeout(10):
            event, data = parse(await anext(frames))
        await frames.aclose()
    snapshot = DispatchesSnapshot.model_validate_json(data)
    assert event == "snapshot"
    assert snapshot.absent == ["other-thread", "deleted"]
    assert [row["commandId"] for row in snapshot.dispatches] == ["queued"]


async def test_a_change_of_the_dispatcher_pushes_the_new_state(tmp_path: Path) -> None:
    dispatcher = FakeDispatcher()
    dispatcher.hold("sent", state=session_dispatcher_pb2.DELIVERY_STATE_QUEUED_IN_DISPATCHER)
    async with serve(tmp_path, dispatcher) as client:
        frames = dispatch_frames(client, THREAD, ["sent"], interval_s=INTERVAL_S)
        async with asyncio.timeout(10):
            await anext(frames)
            dispatcher.hold("sent", state=session_dispatcher_pb2.DELIVERY_STATE_FAILED)
            dispatcher.changed.set()
            states: list[object] = []
            while "DELIVERY_STATE_FAILED" not in states:
                event, data = parse(await anext(frames))
                if event == "snapshot":
                    states = [row["state"] for row in DispatchesSnapshot.model_validate_json(data).dispatches]
        await frames.aclose()


async def test_a_quiet_stream_sends_heartbeats(tmp_path: Path) -> None:
    dispatcher = FakeDispatcher()
    async with serve(tmp_path, dispatcher) as client:
        frames = dispatch_frames(client, THREAD, ["sent"], interval_s=INTERVAL_S)
        async with asyncio.timeout(10):
            await anext(frames)
            assert parse(await anext(frames)) == ("health", "{}")
        await frames.aclose()


async def test_the_stream_ends_with_the_error_that_ended_the_watch(tmp_path: Path) -> None:
    dispatcher = FakeDispatcher()
    dispatcher.watch_failure = grpc.StatusCode.UNAVAILABLE
    async with serve(tmp_path, dispatcher) as client:
        with pytest.raises(ServiceError) as ended:
            await drain(dispatch_frames(client, THREAD, ["sent"], interval_s=INTERVAL_S))
    assert ended.value.code == grpc.StatusCode.UNAVAILABLE


if __name__ == "__main__":
    pytest_bazel.main()

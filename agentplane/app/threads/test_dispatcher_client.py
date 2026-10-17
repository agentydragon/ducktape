"""Transport behavior of the generated Dispatcher client."""

import asyncio
from pathlib import Path
from typing import override
from uuid import uuid4

import grpc
import pytest_bazel

from agentplane.app.threads.dispatcher_client import SessionDispatcherClient
from agentplane.grpc.calls import dial
from agentplane.grpc.serving import serving
from agentplane.proto import session_dispatcher_pb2, session_dispatcher_pb2_grpc

# Generated protobuf typing imports google.protobuf through transitive stubs.
# gazelle:include_dep @pypi//protobuf


class RecordingDispatcher(session_dispatcher_pb2_grpc.SessionDispatcherServicer):
    def __init__(self) -> None:
        self.tokens: list[str] = []
        self.deadlines: list[float] = []

    @override
    async def GetDispatch(self, request, context):
        self.tokens.append(dict(context.invocation_metadata())["authorization"])
        self.deadlines.append(context.time_remaining())
        return session_dispatcher_pb2.GetDispatchResponse(
            dispatch=session_dispatcher_pb2.Dispatch(command_id=request.command_id)
        )


async def test_generated_stub_reads_rotated_bearer_for_each_call(tmp_path: Path) -> None:
    token = tmp_path / "token"
    service = RecordingDispatcher()
    server = grpc.aio.server()
    session_dispatcher_pb2_grpc.add_SessionDispatcherServicer_to_server(service, server)
    async with serving(server, "127.0.0.1:0", grace_s=0) as port, dial(f"127.0.0.1:{port}") as channel:
        client = SessionDispatcherClient(channel, token_file=token, request_timeout_s=7)
        for value in ("first", "second"):
            await asyncio.to_thread(token.write_text, value)
            response = await client.unary(
                client.stub.GetDispatch, session_dispatcher_pb2.GetDispatchRequest(command_id=value)
            )
            assert response.dispatch.command_id == value
    assert service.tokens == ["Bearer first", "Bearer second"]
    assert len(service.deadlines) == 2
    assert all(0 < deadline < 7.5 for deadline in service.deadlines)


class LeasedDispatcher(session_dispatcher_pb2_grpc.SessionDispatcherServicer):
    """Ends its first watch after two notices, as the Dispatcher ends a watch on its lease."""

    def __init__(self) -> None:
        self.watched_sessions: list[str] = []

    @override
    async def WatchDispatches(self, request, _context):
        self.watched_sessions.append(request.filter.session_id)
        yield session_dispatcher_pb2.WatchDispatchesResponse()
        yield session_dispatcher_pb2.WatchDispatchesResponse()
        if len(self.watched_sessions) > 1:
            await asyncio.Event().wait()


async def test_watch_renews_the_stream_when_the_dispatcher_ends_it_on_its_lease(tmp_path: Path) -> None:
    token = tmp_path / "token"
    await asyncio.to_thread(token.write_text, "test-token")
    service = LeasedDispatcher()
    server = grpc.aio.server()
    session_dispatcher_pb2_grpc.add_SessionDispatcherServicer_to_server(service, server)
    session = uuid4()
    async with serving(server, "127.0.0.1:0", grace_s=0) as port, dial(f"127.0.0.1:{port}") as channel:
        client = SessionDispatcherClient(channel, token_file=token, request_timeout_s=7)
        notices = 0
        async with asyncio.timeout(10):
            async for _ in client.watch_dispatches(session):
                notices += 1
                if notices == 3:
                    break
    assert service.watched_sessions == [str(session), str(session)]


if __name__ == "__main__":
    pytest_bazel.main()

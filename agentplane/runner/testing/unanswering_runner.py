"""A runner that takes an Attach and its Open but never answers, as a wedged runner looks to a client."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import grpc

from agentplane.proto import runner_pb2
from agentplane.runner.testing.runner_server import UnimplementedRunner, serve_runner

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio


class UnansweringRunner(UnimplementedRunner):
    def __init__(self) -> None:
        self.cancelled: asyncio.Queue[str] = asyncio.Queue()

    async def Attach(  # noqa: N802  # gRPC names servicer methods after the RPC
        self, requests: AsyncIterator[runner_pb2.ClientMessage], context: grpc.aio.ServicerContext
    ) -> AsyncIterator[runner_pb2.ServerMessage]:
        del context
        first = await anext(requests)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Only the client ending the call gets here: the session whose Open went unanswered.
            self.cancelled.put_nowait(first.open.session_id)
            raise
        yield runner_pb2.ServerMessage()  # Never reached; makes this the streaming handler gRPC calls.

    async def ListSessions(  # noqa: N802  # gRPC names servicer methods after the RPC
        self, request: runner_pb2.ListSessionsRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.ListSessionsResponse:
        """None, so an app ingester that discovers sessions here opens no feeds of its own."""
        del request, context
        return runner_pb2.ListSessionsResponse()

    @asynccontextmanager
    async def serve(self) -> AsyncIterator[int]:
        async with serve_runner(self) as port:
            yield port

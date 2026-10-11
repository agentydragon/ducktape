"""A runner that takes an Attach and its Open but never answers, as a wedged runner looks to a client."""

import asyncio
from collections.abc import AsyncIterator
from typing import override

import grpc

from agentplane.proto import runner_pb2, runner_pb2_grpc

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio


class UnansweringRunner(runner_pb2_grpc.RunnerServicer):
    def __init__(self) -> None:
        self.cancelled: asyncio.Queue[str] = asyncio.Queue()

    @override
    async def Attach(
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

    @override
    async def ListSessions(
        self, request: runner_pb2.ListSessionsRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.ListSessionsResponse:
        """None, so an app ingester that discovers sessions here opens no feeds of its own."""
        del request, context
        return runner_pb2.ListSessionsResponse()

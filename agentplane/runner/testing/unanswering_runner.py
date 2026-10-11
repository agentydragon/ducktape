"""A runner that takes an Attach and its Open but never answers, as a wedged runner looks to a client."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import grpc

from agentplane.proto import runner_pb2
from agentplane.runner.testing.runner_server import serve_runner

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio


class UnansweringRunner:
    def __init__(self) -> None:
        self.cancelled: asyncio.Queue[str] = asyncio.Queue()

    async def attach(
        self, requests: AsyncIterator[runner_pb2.ClientMessage], context: grpc.aio.ServicerContext
    ) -> None:
        del context
        first = await anext(requests)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Only the client ending the call gets here: the session whose Open went unanswered.
            self.cancelled.put_nowait(first.open.session_id)
            raise

    async def list_sessions(
        self, request: runner_pb2.ListSessionsRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.ListSessionsResponse:
        """None, so an app ingester that discovers sessions here opens no feeds of its own."""
        del request, context
        return runner_pb2.ListSessionsResponse()

    @asynccontextmanager
    async def serve(self) -> AsyncIterator[int]:
        async with serve_runner(attach=self.attach, list_sessions=self.list_sessions) as port:
            yield port

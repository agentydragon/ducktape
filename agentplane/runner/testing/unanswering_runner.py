"""A runner that takes an Attach and its Open but never answers, as a wedged runner looks to a client."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import grpc

from agentplane.runner import protocol_pb2

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio


class UnansweringRunner:
    def __init__(self) -> None:
        self.cancelled: asyncio.Queue[str] = asyncio.Queue()

    async def attach(
        self, requests: AsyncIterator[protocol_pb2.ClientMessage], context: grpc.aio.ServicerContext
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
        self, request: protocol_pb2.ListSessionsRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.ListSessionsResponse:
        """None, so an app ingester that discovers sessions here opens no feeds of its own."""
        del request, context
        return protocol_pb2.ListSessionsResponse()

    @asynccontextmanager
    async def serve(self) -> AsyncIterator[int]:
        server = grpc.aio.server()
        server.add_generic_rpc_handlers(
            [
                grpc.method_handlers_generic_handler(
                    "ducktape.agentplane.runner.v1.Runner",
                    {
                        "Attach": grpc.stream_stream_rpc_method_handler(
                            self.attach,
                            request_deserializer=protocol_pb2.ClientMessage.FromString,
                            response_serializer=protocol_pb2.ServerMessage.SerializeToString,
                        ),
                        "ListSessions": grpc.unary_unary_rpc_method_handler(
                            self.list_sessions,
                            request_deserializer=protocol_pb2.ListSessionsRequest.FromString,
                            response_serializer=protocol_pb2.ListSessionsResponse.SerializeToString,
                        ),
                    },
                )
            ]
        )
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        try:
            yield port
        finally:
            await server.stop(0)

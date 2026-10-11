"""Serve a scripted runner's handlers as the Runner service on a loopback ephemeral port."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import grpc

from agentplane.proto import runner_pb2

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio

type Attach = Callable[
    [AsyncIterator[runner_pb2.ClientMessage], grpc.aio.ServicerContext],
    AsyncIterator[runner_pb2.ServerMessage] | Awaitable[None],
]
type ListSessions = Callable[
    [runner_pb2.ListSessionsRequest, grpc.aio.ServicerContext], Awaitable[runner_pb2.ListSessionsResponse]
]
type Seal = Callable[[runner_pb2.SealRequest, grpc.aio.ServicerContext], Awaitable[runner_pb2.SealResponse]]


@asynccontextmanager
async def serve_runner(*, attach: Attach, list_sessions: ListSessions, seal: Seal | None = None) -> AsyncIterator[int]:
    """Yields the port. Without `seal` the runner predates the Seal RPC: gRPC answers UNIMPLEMENTED."""
    handlers: dict[str, grpc.RpcMethodHandler[Any, Any]] = {
        "Attach": grpc.stream_stream_rpc_method_handler(
            attach,
            request_deserializer=runner_pb2.ClientMessage.FromString,
            response_serializer=runner_pb2.ServerMessage.SerializeToString,
        ),
        "ListSessions": grpc.unary_unary_rpc_method_handler(
            list_sessions,
            request_deserializer=runner_pb2.ListSessionsRequest.FromString,
            response_serializer=runner_pb2.ListSessionsResponse.SerializeToString,
        ),
    }
    if seal is not None:
        handlers["Seal"] = grpc.unary_unary_rpc_method_handler(
            seal,
            request_deserializer=runner_pb2.SealRequest.FromString,
            response_serializer=runner_pb2.SealResponse.SerializeToString,
        )
    server = grpc.aio.server()
    server.add_generic_rpc_handlers([grpc.method_handlers_generic_handler("agentplane.proto.runner.Runner", handlers)])
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield port
    finally:
        await server.stop(0)

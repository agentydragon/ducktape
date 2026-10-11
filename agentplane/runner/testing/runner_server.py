"""Serve a fake Runner servicer on a loopback ephemeral port."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import override

import grpc

from agentplane.proto import runner_pb2, runner_pb2_grpc

# gazelle:include_dep @pypi//protobuf


class UnimplementedRunner(runner_pb2_grpc.RunnerServicer):
    """Answers every RPC UNIMPLEMENTED, as a runner that predates it would. The generated stub marks
    each RPC abstract, so a fake subclasses this and overrides only the RPCs its test drives."""

    @override
    async def Attach(
        self, requests: AsyncIterator[runner_pb2.ClientMessage], context: grpc.aio.ServicerContext
    ) -> AsyncIterator[runner_pb2.ServerMessage]:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake runner does not serve Attach")
        yield  # Unreachable; makes this the streaming handler gRPC calls.

    @override
    async def Initialize(
        self, request: runner_pb2.InitializeRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[runner_pb2.InitializationEvent]:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake runner does not serve Initialize")
        yield  # Unreachable; makes this the streaming handler gRPC calls.

    @override
    async def ListSessions(
        self, request: runner_pb2.ListSessionsRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.ListSessionsResponse:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake runner does not serve ListSessions")

    @override
    async def Seal(self, request: runner_pb2.SealRequest, context: grpc.aio.ServicerContext) -> runner_pb2.SealResponse:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "the fake runner does not serve Seal")


@asynccontextmanager
async def serve_runner(servicer: runner_pb2_grpc.RunnerServicer) -> AsyncIterator[int]:
    """Yields the port."""
    server = grpc.aio.server()
    runner_pb2_grpc.add_RunnerServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield port
    finally:
        await server.stop(0)

"""Serve a fake Runner servicer on a loopback ephemeral port."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import grpc

from agentplane.proto import runner_pb2_grpc

# gazelle:include_dep @pypi//protobuf


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

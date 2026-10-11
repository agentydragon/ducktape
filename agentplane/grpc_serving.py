"""The one way an agentplane process serves a `grpc.aio.Server`."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import grpc
from grpc_health.v1 import health, health_pb2_grpc


@asynccontextmanager
async def serving(server: grpc.aio.Server, address: str, *, grace_s: float) -> AsyncIterator[int]:
    """Binds `address`, starts the server and yields the bound port, the ephemeral one for a `:0`
    address. On exit it stops the server, giving in-flight RPCs `grace_s` to finish."""
    port = server.add_insecure_port(address)
    await server.start()
    try:
        yield port
    finally:
        await server.stop(grace=grace_s)


def add_health(server: grpc.aio.Server) -> None:
    """Serves `grpc.health.v1.Health` on `server`, answering SERVING for the whole server (service
    `""`) for as long as it accepts RPCs. Kubernetes gRPC probes call it."""
    health_pb2_grpc.add_HealthServicer_to_server(health.aio.HealthServicer(), server)

"""The one way an agentplane process serves a `grpc.aio.Server`."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import grpc


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

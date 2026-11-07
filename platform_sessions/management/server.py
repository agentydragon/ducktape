"""Start a bearer-protected management listener on loopback only."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import grpc

from platform_sessions.management.service import SessionManagement
from platform_sessions.management.v1.management_pb2_grpc import add_SessionManagementServicer_to_server
from platform_sessions.provider.control_auth import ControlBearerInterceptor
from platform_sessions.provider.control_client import ProviderControlClient

_MANAGEMENT_PREFIX = "/platform_sessions.management.v1.SessionManagement/"


@asynccontextmanager
async def serve_management(clients: dict[str, ProviderControlClient], token_file: Path) -> AsyncIterator[str]:
    """Start an internal loopback listener and close it when the gateway stops.

    Only the same-pod HTTP facade may connect without TLS; a separate bearer is required on every
    call. Public requests must be authorized by the gateway before they reach the facade.
    """
    server = grpc.aio.server(interceptors=[ControlBearerInterceptor(token_file, service_prefix=_MANAGEMENT_PREFIX)])
    add_SessionManagementServicer_to_server(SessionManagement(clients), server)
    port = server.add_insecure_port("127.0.0.1:0")
    if not port:
        raise OSError("Could not bind management listener")
    await server.start()
    try:
        yield f"127.0.0.1:{port}"
    finally:
        await server.stop(grace=5)

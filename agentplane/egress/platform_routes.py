"""Platform routes of the egress sidecar: a loopback gRPC listener per Agentplane service its Pod
calls, such as the Runner Gateway or the event ingress. Each forwards every call to its service with
this Pod's projected token for that service's audience as `authorization`, replacing whatever the
workload sent, so neither the central proxy nor an egress policy is in the path. The token file is
mounted into the sidecar only and re-read on every call.

Calls pass through as bytes: the sidecar knows no service's protos, and registers every method as a
bidirectional stream, which carries unary and streaming calls alike.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack
from pathlib import Path
from types import TracebackType
from typing import Self

import grpc
from pydantic import BaseModel, ConfigDict, Field

from agentplane.egress.token_file import read_token_file

logger = logging.getLogger(__name__)

# Never forwarded from the workload: the route sets the token, and the rest is the transport's own.
_REPLACED_METADATA = frozenset({"authorization"})


class PlatformRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    listen_port: int = Field(description="Loopback port the workload dials for this service.")
    upstream: str = Field(min_length=1, description="host:port of the service, dialled in plaintext in-cluster.")
    token_file: Path = Field(
        description="Projected token file with the service's audience, mounted into the sidecar only."
    )


class _Forward(grpc.GenericRpcHandler):
    def __init__(self, channel: grpc.aio.Channel, token_file: Path) -> None:
        self._channel = channel
        self._token_file = token_file

    def service(self, handler_call_details: grpc.HandlerCallDetails) -> grpc.RpcMethodHandler:
        method = handler_call_details.method

        async def forward(requests: AsyncIterator[bytes], context: grpc.aio.ServicerContext) -> AsyncIterator[bytes]:
            token = read_token_file(self._token_file)
            if token is None:
                await context.abort(grpc.StatusCode.UNAVAILABLE, f"egress sidecar has no token at {self._token_file}")
            metadata = [
                (key, value)
                for key, value in context.invocation_metadata() or ()
                if key not in _REPLACED_METADATA and not key.startswith((":", "grpc-"))
            ]
            # Without serializers the call passes requests and responses through as bytes.
            upstream: grpc.aio.StreamStreamMultiCallable[bytes, bytes] = self._channel.stream_stream(method)
            call = upstream(
                requests, metadata=(*metadata, ("authorization", f"Bearer {token}")), timeout=context.time_remaining()
            )
            try:
                async for response in call:
                    yield response
            except grpc.aio.AioRpcError as error:
                await context.abort(error.code(), error.details() or "", error.trailing_metadata() or ())
            finally:
                call.cancel()

        return grpc.stream_stream_rpc_method_handler(forward)


class PlatformRoutes:
    """Async context manager: entering starts one loopback server per route, exiting stops them."""

    def __init__(self, routes: Mapping[str, PlatformRoute], *, listen_host: str = "127.0.0.1") -> None:
        self._routes = routes
        self._listen_host = listen_host
        self._stack = AsyncExitStack()
        self._ports: dict[str, int] = {}

    async def __aenter__(self) -> Self:
        async with AsyncExitStack() as stack:
            for name, route in self._routes.items():
                channel = await stack.enter_async_context(grpc.aio.insecure_channel(route.upstream))
                server = grpc.aio.server()
                server.add_generic_rpc_handlers([_Forward(channel, route.token_file)])
                self._ports[name] = server.add_insecure_port(f"{self._listen_host}:{route.listen_port}")
                await server.start()
                stack.push_async_callback(server.stop, None)
                logger.info("route %s: %s:%d -> %s", name, self._listen_host, self._ports[name], route.upstream)
            self._stack = stack.pop_all()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self._stack.aclose()

    def port(self, name: str) -> int:
        """The bound port of a route, for `listen_port=0`."""
        return self._ports[name]

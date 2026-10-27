"""The transport lifecycle shared by native gRPC streams relayed through HTTP."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

import grpc
from google.protobuf.message import Message

from agentplane.grpc.calls import raise_status


@dataclass
class OpenWatch[Response: Message]:
    """A watch whose first frame was received before the HTTP response headers were sent."""

    call: grpc.aio.UnaryStreamCall[Any, Response]
    first: Response


async def unary[Response: Message](
    call: Callable[..., Awaitable[Response]],
    request: Message,
    *,
    metadata: tuple[tuple[str, str], ...],
    timeout_s: float,
) -> Response:
    """Run one deadline-bounded RPC and translate its status to the app's service errors."""
    try:
        return await call(request, metadata=metadata, timeout=timeout_s)
    except grpc.aio.AioRpcError as error:
        raise_status(error)


async def open_watch[Request: Message, Response: Message](
    call: Callable[..., grpc.aio.UnaryStreamCall[Request, Response]],
    request: Request,
    *,
    metadata: tuple[tuple[str, str], ...],
    timeout_s: float,
) -> OpenWatch[Response]:
    """Bound the handshake so authentication and early failures remain HTTP errors."""
    stream = call(request, metadata=metadata)
    try:
        async with asyncio.timeout(timeout_s):
            first = await stream.read()
    except grpc.aio.AioRpcError as error:
        stream.cancel()
        raise_status(error)
    except BaseException:
        # A cancelled HTTP request may interrupt the opening frame before the response starts.
        # Do not leave its gRPC call alive when the stream has not yet transferred to `messages`.
        stream.cancel()
        raise
    if first is grpc.aio.EOF:
        stream.cancel()
        raise ConnectionError("gRPC watch ended before its first frame")
    return OpenWatch(stream, cast(Response, first))


async def messages[Response: Message](watch: OpenWatch[Response]) -> AsyncIterator[Response]:
    """Yield frames until EOF or cancellation, always closing the call on consumer exit."""
    try:
        yield watch.first
        while True:
            try:
                response = await watch.call.read()
            except grpc.aio.AioRpcError as error:
                raise_status(error)
            if response is grpc.aio.EOF:
                return
            yield cast(Response, response)
    finally:
        watch.call.cancel()


class OperatorGrpcClient:
    """Common bearer, deadline and stream handling for operator-facing generated stubs."""

    def __init__(self, *, request_timeout_s: float) -> None:
        self._request_timeout_s = request_timeout_s

    async def unary[Response: Message](
        self,
        call: Callable[..., Awaitable[Response]],
        request: Message,
        *,
        bearer_token: str,
        extra_metadata: tuple[tuple[str, str], ...] = (),
    ) -> Response:
        return await unary(
            call,
            request,
            metadata=(("authorization", f"Bearer {bearer_token}"), *extra_metadata),
            timeout_s=self._request_timeout_s,
        )

    async def open_watch[Request: Message, Response: Message](
        self, call: Callable[..., grpc.aio.UnaryStreamCall[Request, Response]], request: Request, *, bearer_token: str
    ) -> OpenWatch[Response]:
        return await open_watch(
            call, request, metadata=(("authorization", f"Bearer {bearer_token}"),), timeout_s=self._request_timeout_s
        )

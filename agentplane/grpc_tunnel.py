"""Unary gRPC calls carried as `Call` and `Reply` messages on a stream the callee dialed, so the side
that dials out serves an ordinary gRPC service.

The callee makes each call on a server of its own, reached over a private Unix socket, so the service
keeps its generated servicer, deadlines and status codes. The caller reaches it through a channel that
never dials, so its generated stubs work unchanged. The stream carrying the messages owns
authentication, liveness and ordering.
"""

import asyncio
import itertools
import logging
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from types import TracebackType
from typing import Self, cast, override

import grpc
from google.protobuf import descriptor_pool, message_factory
from google.protobuf.message import Message

from agentplane.grpc_serving import serving
from agentplane.proto import grpc_tunnel_pb2

# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)

type Send = Callable[[grpc_tunnel_pb2.Call], Awaitable[None]]

_CODES = {code.value[0]: code for code in grpc.StatusCode}


def _status(code: grpc.StatusCode, details: str) -> grpc.aio.AioRpcError:
    return grpc.aio.AioRpcError(code, grpc.aio.Metadata(), grpc.aio.Metadata(), details)


def _response_type(method: str) -> type[Message]:
    # The stub that made the call imported the module defining its method.
    service, name = method.removeprefix("/").split("/")
    output = descriptor_pool.Default().FindMethodByName(f"{service}.{name}").output_type
    return message_factory.GetMessageClass(output)


class TunnelCaller(grpc.aio.UnaryUnaryClientInterceptor):
    """The calling side of one carrying stream: each unary call on `channel` goes out as a `Call` through
    `send`, and returns once `answer` receives its `Reply`. Leaving the block fails every unanswered call
    with `UNAVAILABLE`, since whether it ran is unknown."""

    def __init__(self, send: Send) -> None:
        self._send = send
        self._call_ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[grpc_tunnel_pb2.Reply]] = {}
        self._calls: set[asyncio.Task[object]] = set()
        self._ended = False
        # Never dialed: this interceptor answers every call made on it.
        self.channel = grpc.aio.insecure_channel("grpc-tunnel.invalid", interceptors=[self])

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self._ended = True
        for waiting in self._pending.values():
            if not waiting.done():
                waiting.set_exception(
                    _status(grpc.StatusCode.UNAVAILABLE, "the tunnel ended before the callee answered")
                )
        # Closing the channel cancels the calls still on it: let them raise their status first.
        if self._calls:
            await asyncio.wait(self._calls)
        await self.channel.close()

    def answer(self, reply: grpc_tunnel_pb2.Reply) -> None:
        waiting = self._pending.get(reply.call_id)
        if waiting is None or waiting.done():
            # Its caller's deadline passed first; the call's outcome is the callee's to keep.
            logger.info("Dropped the reply to call %d, which nothing awaits", reply.call_id)
            return
        waiting.set_result(reply)

    @override
    async def intercept_unary_unary[Request, Response](
        self,
        continuation: Callable[
            [grpc.aio.ClientCallDetails, Request], Awaitable[grpc.aio.UnaryUnaryCall[Request, Response]]
        ],
        client_call_details: grpc.aio.ClientCallDetails,
        request: Request,
    ) -> Response | grpc.aio.UnaryUnaryCall[Request, Response]:
        # Generated stubs call with a message and expect their method's output message back.
        assert isinstance(request, Message), request
        method = client_call_details.method
        method = method.decode() if isinstance(method, bytes) else method
        response = await self.forward(method, request.SerializeToString(), deadline_s=client_call_details.timeout)
        return cast(Response, _response_type(method).FromString(response))

    async def forward(self, method: str, request: bytes, *, deadline_s: float | None) -> bytes:
        """Call `method` with the serialized `request` and return the serialized response, or raise the
        call's status as an `AioRpcError`. The callee sees the remaining `deadline_s`."""
        if self._ended:
            raise _status(grpc.StatusCode.UNAVAILABLE, "the tunnel has ended")
        call = grpc_tunnel_pb2.Call(call_id=next(self._call_ids), method=method, request=request)
        if deadline_s is not None:
            call.timeout.FromTimedelta(timedelta(seconds=deadline_s))
        waiting: asyncio.Future[grpc_tunnel_pb2.Reply] = asyncio.get_running_loop().create_future()
        self._pending[call.call_id] = waiting
        current = asyncio.current_task()
        assert current is not None
        self._calls.add(current)
        try:
            try:
                await self._send(call)
            except (grpc.RpcError, asyncio.InvalidStateError) as error:
                raise _status(grpc.StatusCode.UNAVAILABLE, "the tunnel ended before the call was sent") from error
            try:
                async with asyncio.timeout(deadline_s):
                    reply = await waiting
            except TimeoutError as error:
                raise _status(grpc.StatusCode.DEADLINE_EXCEEDED, "the callee did not answer in time") from error
        finally:
            del self._pending[call.call_id]
            self._calls.discard(current)
        if reply.code != grpc.StatusCode.OK.value[0]:
            raise _status(_CODES.get(reply.code, grpc.StatusCode.UNKNOWN), reply.details)
        return reply.response


@asynccontextmanager
async def local_server(add_services: Callable[[grpc.aio.Server], None]) -> AsyncIterator[grpc.aio.Channel]:
    """The callee's own server, with the services `add_services` registers, on a Unix socket in a
    private directory; yields a channel to it for `relay`."""
    with tempfile.TemporaryDirectory(prefix="grpc-tunnel-") as directory:
        target = f"unix:{directory}/grpc.sock"
        server = grpc.aio.server()
        add_services(server)
        async with serving(server, target, grace_s=0), grpc.aio.insecure_channel(target) as channel:
            yield channel


async def relay(call: grpc_tunnel_pb2.Call, server: grpc.aio.Channel) -> grpc_tunnel_pb2.Reply:
    """Make `call` on the callee's `server` and report how it ended."""
    timeout = call.timeout.ToTimedelta().total_seconds() if call.HasField("timeout") else None
    try:
        # Without serializers the call passes the request and response through as bytes.
        passthrough: grpc.aio.UnaryUnaryMultiCallable[bytes, bytes] = server.unary_unary(call.method)
        response = await passthrough(call.request, timeout=timeout)
    except grpc.aio.AioRpcError as error:
        return grpc_tunnel_pb2.Reply(call_id=call.call_id, code=error.code().value[0], details=error.details() or "")
    return grpc_tunnel_pb2.Reply(call_id=call.call_id, response=response)

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

from agentplane.grpc.serving import serving
from agentplane.proto import grpc_tunnel_pb2

logger = logging.getLogger(__name__)

_TUNNEL_ENDED_BEFORE_CALLEE_ANSWER = "the tunnel ended before the callee answered"
_TUNNEL_FAILED_WHILE_SENDING = "the tunnel failed while sending the call; the callee outcome is unknown"

type Send = Callable[[grpc_tunnel_pb2.Call], Awaitable[None]]

_CODES = {code.value[0]: code for code in grpc.StatusCode}


def _status(
    code: grpc.StatusCode, details: str, trailing_metadata: grpc.aio.Metadata | None = None
) -> grpc.aio.AioRpcError:
    return grpc.aio.AioRpcError(code, grpc.aio.Metadata(), trailing_metadata or grpc.aio.Metadata(), details)


def _metadata(reply: grpc_tunnel_pb2.Reply) -> grpc.aio.Metadata:
    return grpc.aio.Metadata(
        *(
            (item.key, item.text if item.WhichOneof("value") == "text" else item.binary)
            for item in reply.trailing_metadata
        )
    )


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
        self._methods: dict[int, str] = {}
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
        if self._pending:
            logger.warning(
                "Runner control tunnel ended with unanswered calls: %s; context_exception=%r",
                {call_id: self._methods[call_id] for call_id in self._pending},
                exc,
            )
        for waiting in self._pending.values():
            if not waiting.done():
                details = _TUNNEL_ENDED_BEFORE_CALLEE_ANSWER
                waiting.set_exception(_status(grpc.StatusCode.UNAVAILABLE, details))
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
        logger.debug("Received reply for tunnel call %d (%s)", reply.call_id, self._methods.get(reply.call_id))
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
            details = "the tunnel has ended"
            raise _status(grpc.StatusCode.UNAVAILABLE, details)
        call = grpc_tunnel_pb2.Call(call_id=next(self._call_ids), method=method, request=request)
        if deadline_s is not None:
            call.timeout.FromTimedelta(timedelta(seconds=deadline_s))
        logger.debug("Forwarding tunnel call %d (%s); deadline_s=%s", call.call_id, method, deadline_s)
        waiting: asyncio.Future[grpc_tunnel_pb2.Reply] = asyncio.get_running_loop().create_future()
        self._pending[call.call_id] = waiting
        self._methods[call.call_id] = method
        current = asyncio.current_task()
        assert current is not None
        self._calls.add(current)
        try:
            try:
                await self._send(call)
            except grpc.aio.AioRpcError as error:
                details = error.details() or ""
                raise _status(error.code(), details, error.trailing_metadata()) from error
            except (grpc.RpcError, asyncio.InvalidStateError) as error:
                logger.warning("Runner control tunnel send failed", exc_info=error)
                raise _status(grpc.StatusCode.UNAVAILABLE, _TUNNEL_FAILED_WHILE_SENDING) from error
            try:
                async with asyncio.timeout(deadline_s):
                    reply = await waiting
            except TimeoutError as error:
                raise _status(grpc.StatusCode.DEADLINE_EXCEEDED, "the callee did not answer in time") from error
        finally:
            del self._pending[call.call_id]
            del self._methods[call.call_id]
            self._calls.discard(current)
        if reply.code != grpc.StatusCode.OK.value[0]:
            logger.info(
                "Tunnel call %d (%s) received status %s: %s",
                call.call_id,
                method,
                _CODES.get(reply.code, grpc.StatusCode.UNKNOWN).name,
                reply.details,
            )
            raise _status(_CODES.get(reply.code, grpc.StatusCode.UNKNOWN), reply.details, _metadata(reply))
        logger.debug("Tunnel call %d (%s) completed", call.call_id, method)
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
        reply = grpc_tunnel_pb2.Reply(call_id=call.call_id, code=error.code().value[0], details=error.details() or "")
        for key, value in error.trailing_metadata() or ():
            item = reply.trailing_metadata.add(key=key)
            if isinstance(value, bytes):
                item.binary = value
            else:
                item.text = value
        return reply
    return grpc_tunnel_pb2.Reply(call_id=call.call_id, response=response)

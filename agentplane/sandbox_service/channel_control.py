"""Lifecycle requests over the runner channel, sent by the replica holding a Sandbox's connection.

The holder writes a `ControlRequest` on `Connect`, and the runner answers each one once with a
`ControlResult` on the same stream. A request is bounded by its caller's deadline; a connection that
ends drops its unanswered requests, and a reconnect replays none of them. Contract:
`agentplane/docs/runner_channel.md` § Lifecycle operations.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from uuid import UUID, uuid4

import grpc

from agentplane.runner.errors import RunnerError
from agentplane.runner_channel import protocol_pb2 as channel_pb2
from agentplane.sandbox_service.connection_records import ConnectionLeases
from agentplane.sandbox_service.destinations import DestinationUnavailableError

# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)

type Send = Callable[[channel_pb2.ServiceFrame], Awaitable[None]]


class ConnectionNotHeldError(DestinationUnavailableError):
    """This replica holds no current runner connection for the Sandbox. The caller resolves the
    Sandbox's control Service again and retries."""


class ControlLostError(DestinationUnavailableError):
    """The connection ended before the runner answered; whether the operation ran is unknown."""


class ControlRefusedError(RunnerError):
    """The runner answered the request with a refusal."""

    def __init__(self, refusal: channel_pb2.ControlRefused) -> None:
        super().__init__(f"{channel_pb2.ControlRefused.Reason.Name(refusal.reason)}: {refusal.detail}")
        self.refusal = refusal


class ControlProtocolError(ConnectionError):
    """The runner answered a request with another verb's result."""


class ControlConnection:
    """One `Connect` stream that agreed `lifecycle`, with its requests awaiting an answer."""

    def __init__(self, *, epoch: int, send: Send) -> None:
        self.epoch = epoch
        self.send = send
        self.pending: dict[str, asyncio.Future[channel_pb2.ControlResult]] = {}

    def answer(self, result: channel_pb2.ControlResult) -> None:
        waiting = self.pending.get(result.request_id)
        if waiting is None or waiting.done():
            # Its caller's deadline passed first; the operation's outcome is the runner's to keep.
            logger.info("Dropped the answer to control request %s, which nothing awaits", result.request_id)
            return
        waiting.set_result(result)

    def end(self) -> None:
        for waiting in self.pending.values():
            if not waiting.done():
                waiting.set_exception(ControlLostError("the runner connection ended before it answered"))


class ChannelControl:
    """This replica's runner connections that carry lifecycle requests, by the UID of each one's Sandbox."""

    def __init__(self, leases: ConnectionLeases) -> None:
        self.leases = leases
        self._connections: dict[UUID, ControlConnection] = {}

    @contextmanager
    def connect(self, sandbox_uid: UUID, *, epoch: int, send: Send) -> Iterator[ControlConnection]:
        """Carry requests on this connection while it lasts. The newest local connection wins."""
        connection = ControlConnection(epoch=epoch, send=send)
        self._connections[sandbox_uid] = connection
        try:
            yield connection
        finally:
            if self._connections.get(sandbox_uid) is connection:
                del self._connections[sandbox_uid]
            connection.end()

    def holder(self, sandbox_uid: UUID) -> ControlConnection | None:
        """The connection this replica holds for the Sandbox, unless the Lease cache shows a newer one.

        A cache that has not seen this connection's accept yet does not count against it: the runner
        acts only on frames from the connection that received its latest `Welcome`.
        """
        connection = self._connections.get(sandbox_uid)
        if connection is None or (self.leases.read_epoch(sandbox_uid) or 0) > connection.epoch:
            return None
        return connection

    async def request(self, sandbox_uid: UUID, request: channel_pb2.ControlRequest) -> channel_pb2.ControlResult:
        """Send one request on the Sandbox's connection and return the runner's answer to it.

        Raises `ControlRefusedError` on a refusal. The caller bounds the wait.
        """
        connection = self.holder(sandbox_uid)
        if connection is None:
            raise ConnectionNotHeldError("this replica holds no current runner connection for the Sandbox")
        sent = channel_pb2.ControlRequest(request_id=str(uuid4()))
        sent.MergeFrom(request)
        waiting: asyncio.Future[channel_pb2.ControlResult] = asyncio.get_running_loop().create_future()
        connection.pending[sent.request_id] = waiting
        try:
            try:
                await connection.send(channel_pb2.ServiceFrame(control=sent))
            except (grpc.RpcError, asyncio.InvalidStateError) as error:
                raise ControlLostError("the runner connection ended before the request was sent") from error
            result = await waiting
        finally:
            del connection.pending[sent.request_id]
        match result.WhichOneof("outcome"):
            case "refused":
                raise ControlRefusedError(result.refused)
            case outcome if outcome != sent.WhichOneof("verb"):
                raise ControlProtocolError(f"a {sent.WhichOneof('verb')} request was answered with {outcome}")
        return result

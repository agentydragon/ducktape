"""Lifecycle requests and command deliveries over the runner control stream, sent by the replica
holding a Sandbox's connection.

The holder writes a `ControlRequest` or `DeliverCommand` on `Connect`, and the runner answers each
on the same stream: a `ControlResult` by request ID, a `CommandReceipt` by Session and command ID.
Each is bounded by its caller's deadline; a connection that ends drops its unanswered requests, and
a reconnect replays none of them. Contract: `agentplane/docs/runner_control.md` § Lifecycle
operations and § Command delivery.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID, uuid4

import grpc

from agentplane.proto import command_pb2, event_log_pb2, runner_control_pb2
from agentplane.runner.errors import RunnerError
from agentplane.runner_gateway.connection_records import ConnectionLeases
from agentplane.runner_gateway.destinations import DestinationUnavailableError

# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)

type Send = Callable[[runner_control_pb2.ServiceFrame], Awaitable[None]]


class ConnectionNotHeldError(DestinationUnavailableError):
    """This replica holds no current runner connection for the Sandbox. The caller resolves the
    Sandbox's control Service again and retries."""


class ControlLostError(DestinationUnavailableError):
    """The connection ended before the runner answered; whether the operation ran is unknown."""


class ControlRefusedError(RunnerError):
    """The runner answered the request with a refusal."""

    def __init__(self, refusal: runner_control_pb2.ControlRefused) -> None:
        super().__init__(f"{runner_control_pb2.ControlRefused.Reason.Name(refusal.reason)}: {refusal.detail}")
        self.refusal = refusal


class ControlProtocolError(ConnectionError):
    """The runner answered a request with another verb's result, or a delivery with another command."""


class CommandRefusedError(Exception):
    """The runner did not admit the command; a stopped harness may run again, and a retry delivers
    it again."""

    def __init__(self, refusal: runner_control_pb2.CommandRefused) -> None:
        super().__init__(runner_control_pb2.CommandRefused.Reason.Name(refusal.reason))
        self.refusal = refusal


@dataclass(frozen=True)
class CommandKey:
    """How a receipt names its command: the runner's Session ID and the command's own ID."""

    runner_session_id: str
    command_id: str


class ControlConnection:
    """One `Connect` stream, with its requests and deliveries awaiting an answer."""

    def __init__(self, *, epoch: int, send: Send) -> None:
        self.epoch = epoch
        self.send = send
        self.pending: dict[str, asyncio.Future[runner_control_pb2.ControlResult]] = {}
        # Concurrent deliveries of one command share its receipt; the runner deduplicates them.
        self.deliveries: dict[CommandKey, set[asyncio.Future[runner_control_pb2.CommandReceipt]]] = {}

    def answer(self, result: runner_control_pb2.ControlResult) -> None:
        waiting = self.pending.get(result.request_id)
        if waiting is None or waiting.done():
            # Its caller's deadline passed first; the operation's outcome is the runner's to keep.
            logger.info("Dropped the answer to control request %s, which nothing awaits", result.request_id)
            return
        waiting.set_result(result)

    def receive(self, receipt: runner_control_pb2.CommandReceipt) -> None:
        waiting = [
            future
            for future in self.deliveries.get(CommandKey(receipt.session_id, receipt.command_id), ())
            if not future.done()
        ]
        if not waiting:
            # Its caller's deadline passed first, or another delivery's receipt already answered it.
            logger.info("Dropped the receipt for command %s, which nothing awaits", receipt.command_id)
        for future in waiting:
            future.set_result(receipt)

    def end(self) -> None:
        lost = ControlLostError("the runner connection ended before it answered")
        for request in self.pending.values():
            if not request.done():
                request.set_exception(lost)
        for delivery in (future for futures in self.deliveries.values() for future in futures):
            if not delivery.done():
                delivery.set_exception(lost)


class LifecycleControl:
    """This replica's runner connections, which carry lifecycle requests and command deliveries, by
    the UID of each one's Sandbox."""

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

    def connection(self, sandbox_uid: UUID) -> ControlConnection:
        connection = self.holder(sandbox_uid)
        if connection is None:
            raise ConnectionNotHeldError("this replica holds no current runner connection for the Sandbox")
        return connection

    def holder(self, sandbox_uid: UUID) -> ControlConnection | None:
        """The connection this replica holds for the Sandbox, unless the Lease cache shows a newer one.

        A cache that has not seen this connection's accept yet does not count against it: the runner
        acts only on frames from the connection that received its latest `Welcome`.
        """
        connection = self._connections.get(sandbox_uid)
        if connection is None or (self.leases.read_epoch(sandbox_uid) or 0) > connection.epoch:
            return None
        return connection

    async def request(
        self, sandbox_uid: UUID, request: runner_control_pb2.ControlRequest
    ) -> runner_control_pb2.ControlResult:
        """Send one request on the Sandbox's connection and return the runner's answer to it.

        Raises `ControlRefusedError` on a refusal. The caller bounds the wait.
        """
        connection = self.connection(sandbox_uid)
        sent = runner_control_pb2.ControlRequest(request_id=str(uuid4()))
        sent.MergeFrom(request)
        waiting: asyncio.Future[runner_control_pb2.ControlResult] = asyncio.get_running_loop().create_future()
        connection.pending[sent.request_id] = waiting
        try:
            try:
                await connection.send(runner_control_pb2.ServiceFrame(control=sent))
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

    async def deliver(
        self, sandbox_uid: UUID, runner_session_id: str, command: command_pb2.Command
    ) -> event_log_pb2.EventEntry:
        """Send the command on the Sandbox's connection and return the runner's admission entry.

        The runner deduplicates by command ID, so a retry after a lost answer is safe and returns the
        original entry. Raises `CommandRefusedError` on a refusal. The caller bounds the wait.
        """
        connection = self.connection(sandbox_uid)
        key = CommandKey(runner_session_id, command.command_id)
        waiting: asyncio.Future[runner_control_pb2.CommandReceipt] = asyncio.get_running_loop().create_future()
        connection.deliveries.setdefault(key, set()).add(waiting)
        try:
            try:
                await connection.send(
                    runner_control_pb2.ServiceFrame(
                        deliver=runner_control_pb2.DeliverCommand(session_id=runner_session_id, command=command)
                    )
                )
            except (grpc.RpcError, asyncio.InvalidStateError) as error:
                raise ControlLostError("the runner connection ended before the command was sent") from error
            receipt = await waiting
        finally:
            waiters = connection.deliveries[key]
            waiters.discard(waiting)
            if not waiters:
                del connection.deliveries[key]
        match receipt.WhichOneof("outcome"):
            case "refused":
                raise CommandRefusedError(receipt.refused)
            case "admitted" if receipt.admitted.event.command_admitted.command == command:
                return receipt.admitted
        raise ControlProtocolError(f"command {command.command_id!r} was answered with a different admission")

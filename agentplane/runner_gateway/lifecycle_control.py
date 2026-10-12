"""This replica's runner connections, and the `Runner` service each runner serves over its own.

The holder of a Sandbox's connection calls the runner's `Runner` service through it. Each call is
bounded by its caller's deadline; a connection that ends fails its unanswered calls with
`UNAVAILABLE`, and a reconnect replays none of them. Contract: `agentplane/docs/runner_control.md`
§ Lifecycle operations and § Command delivery.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from agentplane.grpc_tunnel import Send, TunnelCaller
from agentplane.proto import runner_control_pb2_grpc
from agentplane.runner_gateway.connection_records import ConnectionLeases
from agentplane.runner_gateway.destinations import DestinationUnavailableError

# The `Runner` stub names its messages; mypy reads stubs only from direct deps.
# gazelle:include_dep //agentplane/proto:runner_control_pb2
# gazelle:include_dep @pypi//grpcio
# gazelle:include_dep @pypi//protobuf


class ConnectionNotHeldError(DestinationUnavailableError):
    """This replica holds no current runner connection for the Sandbox. The caller resolves the
    Sandbox's control Service again and retries."""


@dataclass(frozen=True)
class RunnerConnection:
    """One `Connect` stream: its epoch, and the runner's service through it."""

    epoch: int
    tunnel: TunnelCaller
    runner: runner_control_pb2_grpc.RunnerAsyncStub


class LifecycleControl:
    """This replica's runner connections, by the UID of each one's Sandbox."""

    def __init__(self, leases: ConnectionLeases) -> None:
        self.leases = leases
        self._connections: dict[UUID, RunnerConnection] = {}

    @asynccontextmanager
    async def connect(self, sandbox_uid: UUID, *, epoch: int, send: Send) -> AsyncIterator[RunnerConnection]:
        """Call the runner through this connection while it lasts. The newest local connection wins."""
        async with TunnelCaller(send) as tunnel:
            connection = RunnerConnection(
                epoch=epoch, tunnel=tunnel, runner=runner_control_pb2_grpc.RunnerStub(tunnel.channel)
            )
            self._connections[sandbox_uid] = connection
            try:
                yield connection
            finally:
                if self._connections.get(sandbox_uid) is connection:
                    del self._connections[sandbox_uid]

    def holder(self, sandbox_uid: UUID) -> RunnerConnection | None:
        """The connection this replica holds for the Sandbox, unless the Lease cache shows a newer one.

        A cache that has not seen this connection's accept yet does not count against it: the runner
        acts only on frames from the connection that received its latest `Welcome`.
        """
        connection = self._connections.get(sandbox_uid)
        if connection is None or (self.leases.read_epoch(sandbox_uid) or 0) > connection.epoch:
            return None
        return connection

    def runner(self, sandbox_uid: UUID) -> runner_control_pb2_grpc.RunnerAsyncStub:
        """The Sandbox's `Runner` service, through the connection this replica holds."""
        connection = self.holder(sandbox_uid)
        if connection is None:
            raise ConnectionNotHeldError("this replica holds no current runner connection for the Sandbox")
        return connection.runner

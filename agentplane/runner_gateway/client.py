"""gRPC client for the Runner proxy reached through each Sandbox's control Service."""

from collections.abc import Awaitable, Callable
from pathlib import Path

import grpc
from google.protobuf.message import Message

from agentplane.env.runtime.control import environment_runtime_pb2_grpc
from agentplane.grpc.calls import call_unary, dial
from agentplane.proto import session_pb2
from agentplane.proto.identity_pb2 import SandboxDestination
from agentplane.runner_gateway.api import (
    SANDBOX_HEADER,
    SANDBOX_NAMESPACE_HEADER,
    SANDBOX_OWNER_NAME_HEADER,
    SANDBOX_OWNER_NAMESPACE_HEADER,
    SANDBOX_UID_HEADER,
)

# gazelle:include_dep @pypi//grpcio


class SandboxControlClient:
    """Calls only the replica holding a Sandbox's runner connection answers, through the Sandbox's
    control Service, each on a connection dialed for that call alone: a replica that no longer holds
    it refuses with UNAVAILABLE, and the caller's retry resolves the control Service again."""

    def __init__(
        self,
        *,
        control_target: str,
        token_file: Path,
        request_timeout_s: float,
        channel_credentials: grpc.ChannelCredentials | None = None,
        server_name: str | None = None,
    ) -> None:
        # A template of `{sandbox}` and `{namespace}`: `{sandbox}-control.{namespace}.svc:8080`.
        self.control_target = control_target
        self.token_file = token_file
        self.request_timeout_s = request_timeout_s
        self.channel_credentials = channel_credentials
        self.server_name = server_name

    def runner(self, destination: SandboxDestination) -> SandboxRunner:
        return SandboxRunner(self, destination)

    async def runner_unary[T](
        self,
        destination: SandboxDestination,
        call: Callable[[environment_runtime_pb2_grpc.EnvironmentRuntimeAsyncStub], Callable[..., Awaitable[T]]],
        request: Message,
        *,
        timeout_s: float | None = None,
    ) -> T:
        """Call a Runner RPC through its control Service, fenced to the expected Sandbox identity."""
        async with dial(
            self.control_target.format(sandbox=destination.target.name, namespace=destination.target.namespace),
            channel_credentials=self.channel_credentials,
            server_name=self.server_name,
        ) as channel:
            runner: environment_runtime_pb2_grpc.EnvironmentRuntimeAsyncStub = (
                environment_runtime_pb2_grpc.EnvironmentRuntimeStub(channel)
            )
            return await call_unary(
                call(runner),
                request,
                token_file=self.token_file,
                timeout_s=timeout_s or self.request_timeout_s,
                extra_metadata=(
                    (SANDBOX_HEADER, destination.target.name),
                    (SANDBOX_NAMESPACE_HEADER, destination.target.namespace),
                    (SANDBOX_UID_HEADER, destination.target.uid),
                    (SANDBOX_OWNER_NAMESPACE_HEADER, destination.owner.namespace),
                    (SANDBOX_OWNER_NAME_HEADER, destination.owner.name),
                ),
            )

    async def list_sessions(self, destination: SandboxDestination) -> list[session_pb2.SessionSummary]:
        """Read live runner inventory through its proxy, fenced to the expected Sandbox identity."""
        response = await self.runner_unary(
            destination, lambda stub: stub.ListSessions, session_pb2.ListSessionsRequest()
        )
        return list(response.sessions)


class SandboxRunner:
    """One Sandbox's runner, reached through the replica holding its connection."""

    def __init__(self, control: SandboxControlClient, destination: SandboxDestination) -> None:
        self.control = control
        self.destination = destination

    async def list_sessions(self) -> list[session_pb2.SessionSummary]:
        return await self.control.list_sessions(self.destination)

    async def status(self) -> session_pb2.EnvironmentRuntimeStatus:
        return await self.control.runner_unary(
            self.destination, lambda stub: stub.GetStatus, session_pb2.GetStatusRequest()
        )

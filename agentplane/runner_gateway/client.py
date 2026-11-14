"""gRPC client for the Runner proxy reached through the stable Runner Gateway Service."""

from collections.abc import Awaitable, Callable
from pathlib import Path

import grpc
from google.protobuf.message import Message

from agentplane.env.runtime.control import environment_runtime_pb2_grpc
from agentplane.grpc.calls import call_unary, dial
from agentplane.proto import session_pb2
from agentplane.proto.identity_pb2 import SandboxDestination
from agentplane.runner_gateway.api import (
    INCARNATION_UID_HEADER,
    SANDBOX_HEADER,
    SANDBOX_NAMESPACE_HEADER,
    SANDBOX_OWNER_NAME_HEADER,
    SANDBOX_OWNER_NAMESPACE_HEADER,
    SANDBOX_UID_HEADER,
)

# gazelle:include_dep @pypi//grpcio


class SandboxControlClient:
    """Calls the Runner Gateway Service on a fresh connection for each RPC; the Gateway replica
    receiving the call uses the Sandbox identity to select its local runner connection."""

    def __init__(
        self,
        *,
        control_target: str,
        token_file: Path,
        request_timeout_s: float,
        channel_credentials: grpc.ChannelCredentials | None = None,
        server_name: str | None = None,
    ) -> None:
        # A template of `{namespace}`: `runner-gateway.{namespace}.svc:8080`.
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
        expected_incarnation_uid: str | None = None,
    ) -> T:
        """Call a Runner RPC through the shared Gateway Service, fenced to the Sandbox identity."""
        incarnation_metadata = (
            ((INCARNATION_UID_HEADER, expected_incarnation_uid),) if expected_incarnation_uid is not None else ()
        )
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
                    *incarnation_metadata,
                ),
            )

    async def list_sessions(
        self, destination: SandboxDestination, *, expected_incarnation_uid: str | None = None
    ) -> list[session_pb2.SessionSummary]:
        """Read live runner inventory through the Gateway proxy, fenced to the expected Sandbox identity."""
        response = await self.runner_unary(
            destination,
            lambda stub: stub.ListSessions,
            session_pb2.ListSessionsRequest(),
            expected_incarnation_uid=expected_incarnation_uid,
        )
        return list(response.sessions)


class SandboxRunner:
    """One Sandbox's runner, reached through the shared Runner Gateway Service."""

    def __init__(self, control: SandboxControlClient, destination: SandboxDestination) -> None:
        self.control = control
        self.destination = destination

    async def list_sessions(self, *, expected_incarnation_uid: str | None = None) -> list[session_pb2.SessionSummary]:
        return await self.control.list_sessions(self.destination, expected_incarnation_uid=expected_incarnation_uid)

    async def status(self) -> session_pb2.EnvironmentRuntimeStatus:
        return await self.control.runner_unary(
            self.destination, lambda stub: stub.GetStatus, session_pb2.GetStatusRequest()
        )

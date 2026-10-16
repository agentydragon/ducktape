"""gRPC clients for Gateway Session placement and the per-Sandbox Runner control proxy."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import UUID

import grpc
from google.protobuf.message import Message

from agentplane.grpc.calls import bearer, call_unary, dial
from agentplane.proto import (
    runner_control_pb2,
    runner_control_pb2_grpc,
    runner_gateway_pb2,
    runner_gateway_pb2_grpc,
    runner_pb2,
)
from agentplane.proto.sandboxes_pb2 import SandboxDestination
from agentplane.runner_gateway import wire
from agentplane.runner_gateway.runner_proxy import (
    SANDBOX_HEADER,
    SANDBOX_OWNER_NAME_HEADER,
    SANDBOX_OWNER_NAMESPACE_HEADER,
    SANDBOX_UID_HEADER,
)

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio


class SandboxControlClient:
    """Calls only the replica holding a Sandbox's runner connection answers, through the Sandbox's
    control Service, each on a connection dialed for that call alone: a replica that no longer holds
    it refuses with UNAVAILABLE, and the caller's retry resolves the control Service again."""

    def __init__(
        self, *, control_target: str, token_file: Path, request_timeout_s: float, lifecycle_timeout_s: float = 310
    ) -> None:
        # A template of `{sandbox}` and `{namespace}`: `{sandbox}-control.{namespace}.svc:8080`.
        self.control_target = control_target
        self.token_file = token_file
        self.request_timeout_s = request_timeout_s
        self.lifecycle_timeout_s = lifecycle_timeout_s

    async def held_unary[T](
        self,
        destination: SandboxDestination,
        call: Callable[[runner_gateway_pb2_grpc.RunnerGatewayAsyncStub], Callable[..., Awaitable[T]]],
        request: Message,
        *,
        timeout_s: float | None = None,
    ) -> T:
        async with dial(
            self.control_target.format(sandbox=destination.sandbox, namespace=destination.owner.namespace)
        ) as channel:
            return await call_unary(
                call(runner_gateway_pb2_grpc.RunnerGatewayStub(channel)),
                request,
                token_file=self.token_file,
                timeout_s=timeout_s or self.request_timeout_s,
            )

    def runner(self, destination: SandboxDestination) -> SandboxRunner:
        return SandboxRunner(self, destination)

    async def runner_unary[T](
        self,
        destination: SandboxDestination,
        call: Callable[[runner_control_pb2_grpc.RunnerAsyncStub], Callable[..., Awaitable[T]]],
        request: Message,
        *,
        timeout_s: float | None = None,
    ) -> T:
        """Call a Runner RPC through its control Service, fenced to the expected Sandbox identity."""
        async with dial(
            self.control_target.format(sandbox=destination.sandbox, namespace=destination.owner.namespace)
        ) as channel:
            runner: runner_control_pb2_grpc.RunnerAsyncStub = runner_control_pb2_grpc.RunnerStub(channel)
            return await call_unary(
                call(runner),
                request,
                token_file=self.token_file,
                timeout_s=timeout_s or self.request_timeout_s,
                extra_metadata=(
                    (SANDBOX_HEADER, destination.sandbox),
                    (SANDBOX_UID_HEADER, destination.sandbox_uid),
                    (SANDBOX_OWNER_NAMESPACE_HEADER, destination.owner.namespace),
                    (SANDBOX_OWNER_NAME_HEADER, destination.owner.name),
                ),
            )

    async def list_sessions(self, destination: SandboxDestination) -> list[runner_pb2.SessionSummary]:
        """Read live runner inventory through its proxy, fenced to the expected Sandbox identity."""
        response = await self.runner_unary(
            destination, lambda stub: stub.ListSessions, runner_pb2.ListSessionsRequest()
        )
        return list(response.sessions)


class RunnerGatewayClient:
    def __init__(
        self, channel: grpc.aio.Channel, *, token_file: Path, request_timeout_s: float, control: SandboxControlClient
    ) -> None:
        self.stub: runner_gateway_pb2_grpc.RunnerGatewayAsyncStub = runner_gateway_pb2_grpc.RunnerGatewayStub(channel)
        self.token_file = token_file
        self.request_timeout_s = request_timeout_s
        self.control = control

    async def metadata(self) -> tuple[tuple[str, str], ...]:
        return await bearer(self.token_file)

    async def unary[T](
        self, call: Callable[..., Awaitable[T]], request: Message, *, timeout_s: float | None = None
    ) -> T:
        return await call_unary(
            call, request, token_file=self.token_file, timeout_s=timeout_s or self.request_timeout_s
        )

    async def session_sandboxes(self, session_ids: set[str]) -> list[runner_gateway_pb2.SessionSandbox]:
        """Where each Session the gateway registered among `session_ids` runs."""
        request = runner_gateway_pb2.BatchGetSessionSandboxesRequest(session_ids=sorted(session_ids))
        return list((await self.unary(self.stub.BatchGetSessionSandboxes, request)).sessions)

    def runner(self, destination: SandboxDestination) -> SandboxRunner:
        return self.control.runner(destination)


class SandboxRunner:
    """One Sandbox's runner, reached through the replica holding its connection."""

    def __init__(self, control: SandboxControlClient, destination: SandboxDestination) -> None:
        self.control = control
        self.destination = destination

    async def list_sessions(self) -> list[runner_pb2.SessionSummary]:
        return await self.control.list_sessions(self.destination)

    async def create(
        self, *, session_id: UUID, spec: dict[str, object], setup_script: str | None = None
    ) -> runner_gateway_pb2.CreateSessionResponse:
        """Open the Session the caller minted `session_id` for; a retry passes the same ID and inputs."""
        return await self.control.held_unary(
            self.destination,
            lambda stub: stub.CreateSession,
            wire.create_proto(self.destination, session_id, spec, setup_script),
            timeout_s=self.control.lifecycle_timeout_s,
        )

    async def resume(self, session_id: str) -> runner_pb2.Attached:
        """Resume a stopped harness on the Runner holding this Sandbox's connection."""
        response = await self.control.runner_unary(
            self.destination,
            lambda stub: stub.ResumeSession,
            runner_control_pb2.ResumeSessionRequest(session_id=session_id),
            timeout_s=self.control.lifecycle_timeout_s,
        )
        return response.attached

"""gRPC clients for the Runner Gateway: Session routing through its Service, and the per-Sandbox
calls, commands included, that only the replica holding a runner's connection answers."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import UUID

import grpc
from google.protobuf.message import Message

from agentplane.grpc_calls import bearer, call_unary, dial
from agentplane.proto import (
    command_pb2,
    event_log_pb2,
    runner_gateway_pb2,
    runner_gateway_pb2_grpc,
    runner_pb2,
    sandbox_service_pb2,
)
from agentplane.proto.sandbox_service_pb2 import SandboxDestination
from agentplane.runner_gateway import wire

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio


class SandboxControlClient:
    """Calls only the replica holding a Sandbox's runner connection answers, through the Sandbox's
    control Service, each on a connection dialed for that call alone: a replica that no longer holds
    it refuses with UNAVAILABLE, and the caller's retry resolves the control Service again."""

    def __init__(
        self, *, control_target: str, token_file: Path, request_timeout_s: float, lifecycle_timeout_s: float
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


class RunnerGatewayClient:
    def __init__(
        self,
        channel: grpc.aio.Channel,
        *,
        token_file: Path,
        command_admission_timeout_s: float,
        request_timeout_s: float,
        control: SandboxControlClient,
    ) -> None:
        self.stub: runner_gateway_pb2_grpc.RunnerGatewayAsyncStub = runner_gateway_pb2_grpc.RunnerGatewayStub(channel)
        self.token_file = token_file
        self.request_timeout_s = request_timeout_s
        self.command_admission_timeout_s = command_admission_timeout_s
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

    async def find_sessions(
        self, request: runner_gateway_pb2.FindSessionsRequest
    ) -> list[runner_gateway_pb2.SessionSandbox]:
        """Every Session in a Sandbox whose name `request` matches, deleted Sandboxes included."""
        return list((await self.unary(self.stub.FindSessions, request)).sessions)

    async def command(
        self, destination: SandboxDestination, session_id: str, command: command_pb2.Command
    ) -> event_log_pb2.EventEntry:
        """Submit to a public Session running in `destination` and return the runner's admission entry.

        A retry with the same command returns the same entry: the runner deduplicates by command ID.
        """
        response = await self.control.held_unary(
            destination,
            lambda stub: stub.SubmitCommand,
            runner_gateway_pb2.SubmitCommandRequest(
                destination=sandbox_service_pb2.SessionDestination(sandbox=destination, session_id=session_id),
                command=command,
            ),
            timeout_s=self.command_admission_timeout_s,
        )
        receipt = response.admission
        if not receipt.event.HasField("command_admitted") or receipt.event.command_admitted.command != command:
            raise ConnectionError("Runner Gateway did not return the exact command admission")
        return receipt

    def runner(self, destination: SandboxDestination) -> SandboxRunner:
        return self.control.runner(destination)


class SandboxRunner:
    """One Sandbox's runner, reached through the replica holding its connection."""

    def __init__(self, control: SandboxControlClient, destination: SandboxDestination) -> None:
        self.control = control
        self.destination = destination

    async def list_sessions(self) -> list[runner_pb2.SessionSummary]:
        result = await self.control.held_unary(
            self.destination,
            lambda stub: stub.ListSessions,
            runner_gateway_pb2.ListSessionsRequest(destination=self.destination),
        )
        return list(result.sessions)

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

    async def seal(self) -> runner_gateway_pb2.SealSandboxResponse:
        """Teardown's seal, which only the Sandbox Service may ask for."""
        return await self.control.held_unary(
            self.destination,
            lambda stub: stub.SealSandbox,
            runner_gateway_pb2.SealSandboxRequest(destination=self.destination),
            timeout_s=self.control.lifecycle_timeout_s,
        )

    async def resume(self, session_id: str) -> runner_pb2.Attached:
        response = await self.control.held_unary(
            self.destination,
            lambda stub: stub.ResumeSession,
            runner_gateway_pb2.ResumeSessionRequest(
                destination=sandbox_service_pb2.SessionDestination(sandbox=self.destination, session_id=session_id)
            ),
            timeout_s=self.control.lifecycle_timeout_s,
        )
        return response.attached

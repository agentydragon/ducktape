"""gRPC client for Sandbox Service, with no Kubernetes or direct-runner fallback."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Never

import grpc
from google.protobuf.empty_pb2 import Empty
from google.protobuf.message import Message

from agentplane.grpc_options import grpc_channel_option_kvps
from agentplane.protocol import command_pb2, event_log_pb2
from agentplane.runner import protocol_pb2 as runner_pb2
from agentplane.runner.errors import RunnerError, StreamClosedError
from agentplane.sandbox_service import protocol_pb2, protocol_pb2_grpc, wire
from agentplane.sandbox_service.models import SandboxNotFoundError
from agentplane.sandbox_service.protocol_pb2 import CreateSandboxRequest, Sandbox, SandboxDestination

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio


class ServiceError(ConnectionError):
    def __init__(self, code: grpc.StatusCode) -> None:
        super().__init__(f"Sandbox Service returned {code.name}; mutation outcome may be uncertain")
        self.code = code


class ReconnectRequiredError(ConnectionError):
    """Planned follow renewal, not an interruption or native session end."""


def _raise(error: grpc.aio.AioRpcError) -> Never:
    # Do not include upstream details: they may contain credentials or native output.
    if error.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
        raise TimeoutError("Sandbox Service outcome uncertain; reconcile using unchanged identifiers") from error
    if error.code() == grpc.StatusCode.FAILED_PRECONDITION:
        raise RunnerError("Sandbox Service refused the requested session or resource state") from error
    raise ServiceError(error.code()) from error


class Attachment:
    def __init__(self, call: grpc.aio.UnaryStreamCall, attached: runner_pb2.Attached) -> None:
        self.attached = attached
        self._call = call
        self._ended = False

    async def next_entry(self) -> event_log_pb2.EventEntry:
        if self._ended:
            raise StreamClosedError
        try:
            message = await self._call.read()
        except grpc.aio.AioRpcError as error:
            if error.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
                raise TimeoutError("Sandbox Service follow deadline expired") from error
            _raise(error)
        if message is grpc.aio.EOF:
            raise ConnectionError("Sandbox Service follow ended without native closure evidence")
        assert isinstance(message, protocol_pb2.FollowSessionResponse)
        if message.HasField("entry"):
            return message.entry
        if message.HasField("reconnect_required"):
            self._call.cancel()
            raise ReconnectRequiredError
        if message.HasField("ended"):
            self._ended = True
            self._call.cancel()
            raise StreamClosedError
        self._call.cancel()
        raise ConnectionError("Sandbox Service sent an unexpected follow observation")

    def cancel(self) -> None:
        self._call.cancel()


class SandboxServiceClient:
    def __init__(
        self,
        target: str,
        *,
        namespace: str,
        token_file: Path,
        command_admission_timeout_s: float | None,
        request_timeout_s: float,
        lifecycle_timeout_s: float,
        follow_timeout_s: float,
        channel_options: Mapping[str, int | str] | None = None,
    ) -> None:
        if min(request_timeout_s, lifecycle_timeout_s, follow_timeout_s) <= 0 or (
            command_admission_timeout_s is not None and command_admission_timeout_s <= 0
        ):
            raise ValueError("timeouts must be positive")
        self.target = target
        self.namespace = namespace
        self.token_file = token_file
        self.request_timeout_s = request_timeout_s
        self.command_admission_timeout_s = command_admission_timeout_s
        self.lifecycle_timeout_s = lifecycle_timeout_s
        self.follow_timeout_s = follow_timeout_s
        self._channel_options = channel_options
        self._channel: grpc.aio.Channel | None = None
        self._stub: protocol_pb2_grpc.SandboxServiceAsyncStub | None = None

    @property
    def stub(self) -> protocol_pb2_grpc.SandboxServiceAsyncStub:
        if self._stub is None:
            # No retry service config: uncertain mutations need domain-specific reconciliation.
            self._channel = grpc.aio.insecure_channel(
                self.target,
                options=grpc_channel_option_kvps(self._channel_options, required={"grpc.enable_retries": 0}),
            )
            self._stub = protocol_pb2_grpc.SandboxServiceStub(self._channel)
        return self._stub

    async def metadata(self) -> tuple[tuple[str, str], ...]:
        # Projected tokens rotate. Read for every RPC, including follow reconnects.
        token = (await asyncio.to_thread(self.token_file.read_text)).strip()
        return (("authorization", f"Bearer {token}"),)

    async def unary[T](
        self, call: Callable[..., Awaitable[T]], request: Message, *, timeout_s: float | None = None
    ) -> T:
        try:
            return await call(request, metadata=await self.metadata(), timeout=timeout_s or self.request_timeout_s)
        except grpc.aio.AioRpcError as error:
            _raise(error)

    async def read_session_events(
        self, session_id: str, *, after_cursor: int = 0, limit: int = 128
    ) -> protocol_pb2.ReadSessionEventsResponse:
        if not session_id or after_cursor < 0 or not 1 <= limit <= 1000:
            raise ValueError("invalid session history page")
        return await self.unary(
            self.stub.ReadSessionEvents,
            protocol_pb2.ReadSessionEventsRequest(session_id=session_id, after_cursor=after_cursor, limit=limit),
        )

    async def list_sandboxes(self) -> list[Sandbox]:
        result = await self.unary(self.stub.ListSandboxes, Empty())
        return list(result.sandboxes)

    async def list_templates(self) -> list[str]:
        result = await self.unary(self.stub.ListTemplates, Empty())
        return list(result.templates)

    async def get(self, name: str) -> Sandbox:
        try:
            result = await self.unary(self.stub.GetSandbox, protocol_pb2.GetSandboxRequest(name=name))
        except ServiceError as error:
            if error.code == grpc.StatusCode.NOT_FOUND:
                raise SandboxNotFoundError(name) from error
            raise
        return result

    async def create(self, spec: CreateSandboxRequest) -> Sandbox:
        return await self.unary(self.stub.CreateSandbox, spec, timeout_s=self.lifecycle_timeout_s)

    async def _lifecycle(self, call: Callable[..., Awaitable[Empty]], name: str) -> None:
        view = await self.get(name)
        destination = SandboxDestination(owner=view.service_account, sandbox=view.name, sandbox_uid=view.uid)
        await self.unary(call, protocol_pb2.SandboxRequest(destination=destination))

    async def suspend(self, name: str) -> None:
        await self._lifecycle(self.stub.SuspendSandbox, name)

    async def resume(self, name: str) -> None:
        await self._lifecycle(self.stub.ResumeSandbox, name)

    async def delete(self, name: str) -> None:
        await self._lifecycle(self.stub.DeleteSandbox, name)

    async def grant_egress(self, sandbox: Sandbox, policies: list[str]) -> str:
        destination = SandboxDestination(owner=sandbox.service_account, sandbox=sandbox.name, sandbox_uid=sandbox.uid)
        result = await self.unary(
            self.stub.GrantEgress, protocol_pb2.GrantEgressRequest(destination=destination, egress_policies=policies)
        )
        return result.binding_name

    async def revoke_egress(self, name: str) -> None:
        await self.unary(self.stub.RevokeEgress, protocol_pb2.RevokeEgressRequest(binding_name=name))

    def runner(self, destination: SandboxDestination) -> Runner:
        return Runner(self, destination)

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None


class Runner:
    def __init__(self, service: SandboxServiceClient, destination: SandboxDestination) -> None:
        self.service = service
        self.destination = destination

    async def list_sessions(self) -> list[runner_pb2.SessionSummary]:
        result = await self.service.unary(
            self.service.stub.ListSessions, protocol_pb2.SandboxRequest(destination=self.destination)
        )
        return list(result.sessions)

    async def create(
        self, *, idempotency_key: str, spec: dict[str, object], setup_script: str | None = None
    ) -> protocol_pb2.CreateSessionResponse:
        # The key is for an explicit Open attempt, not a runner/session identifier.
        launch = wire.open_proto(protocol_pb2.SessionDestination(sandbox=self.destination), spec, setup_script)
        request = protocol_pb2.CreateSessionRequest(
            sandbox=self.destination,
            idempotency_key=idempotency_key,
            spec=launch.spec,
            override_mask=launch.override_mask,
        )
        if setup_script is not None:
            request.setup_script = setup_script
        return await self.service.unary(
            self.service.stub.CreateSession, request, timeout_s=self.service.lifecycle_timeout_s
        )

    async def lookup(self, *, idempotency_key: str) -> protocol_pb2.LookupSessionResponse:
        return await self.service.unary(
            self.service.stub.LookupSession,
            protocol_pb2.LookupSessionRequest(sandbox=self.destination, idempotency_key=idempotency_key),
        )

    async def open(
        self, session_id: str, spec: dict[str, object], setup_script: str | None = None
    ) -> runner_pb2.Attached:
        return await self.service.unary(
            self.service.stub.OpenSession,
            wire.open_proto(
                protocol_pb2.SessionDestination(sandbox=self.destination, session_id=session_id), spec, setup_script
            ),
            timeout_s=self.service.lifecycle_timeout_s,
        )

    async def resume(self, session_id: str) -> runner_pb2.Attached:
        return await self.service.unary(
            self.service.stub.ResumeSession,
            protocol_pb2.SessionRequest(
                destination=protocol_pb2.SessionDestination(sandbox=self.destination, session_id=session_id)
            ),
            timeout_s=self.service.lifecycle_timeout_s,
        )

    async def command(
        self, session_id: str, command: command_pb2.Command, *, after_cursor: int
    ) -> event_log_pb2.EventEntry:
        if self.service.command_admission_timeout_s is None:
            raise ValueError("SubmitCommand requires an explicit client deadline")
        receipt = await self.service.unary(
            self.service.stub.SubmitCommand,
            protocol_pb2.SubmitCommandRequest(
                destination=protocol_pb2.SessionDestination(sandbox=self.destination, session_id=session_id),
                command=command,
                follow=event_log_pb2.Follow(after_cursor=after_cursor),
            ),
            timeout_s=self.service.command_admission_timeout_s,
        )
        if not receipt.event.HasField("command_admitted") or receipt.event.command_admitted.command != command:
            raise ConnectionError("Sandbox Service did not return the exact command admission")
        return receipt

    async def attach(self, session_id: str, *, after_cursor: int = 0) -> Attachment:
        call = self.service.stub.FollowSession(
            protocol_pb2.FollowSessionRequest(
                destination=protocol_pb2.SessionDestination(sandbox=self.destination, session_id=session_id),
                follow=event_log_pb2.Follow(after_cursor=after_cursor),
            ),
            metadata=await self.service.metadata(),
            timeout=self.service.follow_timeout_s,
        )
        try:
            async with asyncio.timeout(self.service.request_timeout_s):
                message = await call.read()
            if not isinstance(message, protocol_pb2.FollowSessionResponse) or not message.HasField("attached"):
                raise ConnectionError("Sandbox Service did not provide an Attached snapshot")
            return Attachment(call, message.attached)
        except BaseException as error:
            call.cancel()
            if isinstance(error, grpc.aio.AioRpcError):
                _raise(error)
            raise

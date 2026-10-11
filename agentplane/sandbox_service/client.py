"""gRPC client for Sandbox Service, with no Kubernetes or direct-runner fallback."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path
from types import TracebackType
from typing import Never, Self
from uuid import UUID

import grpc
from google.protobuf.empty_pb2 import Empty
from google.protobuf.message import Message

from agentplane.grpc_options import grpc_channel_option_kvps
from agentplane.proto import (
    command_pb2,
    event_log_pb2,
    runner_gateway_pb2_grpc,
    runner_pb2,
    sandbox_service_pb2,
    sandbox_service_pb2_grpc,
)
from agentplane.proto.sandbox_service_pb2 import CreateSandboxRequest, Sandbox, SandboxDestination
from agentplane.runner.errors import RunnerError, StreamClosedError
from agentplane.sandbox_service import wire
from agentplane.sandbox_service.models import SandboxNotFoundError

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio

logger = logging.getLogger(__name__)


class ServiceError(ConnectionError):
    def __init__(self, code: grpc.StatusCode) -> None:
        super().__init__(f"Sandbox Service returned {code.name}; mutation outcome may be uncertain")
        self.code = code


class ReconnectRequiredError(ConnectionError):
    """Planned follow renewal, not an interruption or native session end."""


class SealedError(StreamClosedError):
    """The runner sealed this Sandbox incarnation of the Session; its journal ends at `cursor`."""

    def __init__(self, cursor: int) -> None:
        super().__init__(f"Session incarnation sealed at cursor {cursor}")
        self.cursor = cursor


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
        assert isinstance(message, sandbox_service_pb2.FollowSessionResponse)
        if message.HasField("entry"):
            return message.entry
        if message.HasField("reconnect_required"):
            self._call.cancel()
            raise ReconnectRequiredError
        if message.HasField("ended"):
            self._ended = True
            self._call.cancel()
            raise StreamClosedError
        if message.HasField("sealed"):
            self._ended = True
            self._call.cancel()
            raise SealedError(message.sealed.cursor)
        self._call.cancel()
        raise ConnectionError("Sandbox Service sent an unexpected follow observation")

    def cancel(self) -> None:
        self._call.cancel()


class SandboxServiceClient:
    def __init__(
        self,
        target: str,
        *,
        gateway_target: str,
        # The Sandbox namespace, for callers that check destinations against it.
        namespace: str | None,
        token_file: Path,
        command_admission_timeout_s: float | None,
        request_timeout_s: float,
        lifecycle_timeout_s: float,
        follow_timeout_s: float,
        control_target: str | None = None,
        channel_options: Mapping[str, int | str] | None = None,
    ) -> None:
        if min(request_timeout_s, lifecycle_timeout_s, follow_timeout_s) <= 0 or (
            command_admission_timeout_s is not None and command_admission_timeout_s <= 0
        ):
            raise ValueError("timeouts must be positive")
        # The Sandbox Service, and the Runner Gateway serving Sessions.
        self.target = target
        self.gateway_target = gateway_target
        self.namespace = namespace
        self.token_file = token_file
        self.request_timeout_s = request_timeout_s
        self.command_admission_timeout_s = command_admission_timeout_s
        self.lifecycle_timeout_s = lifecycle_timeout_s
        self.follow_timeout_s = follow_timeout_s
        # A Sandbox's control Service, which names the replica holding its runner connection, as a
        # template of `{sandbox}` and `{namespace}`: `{sandbox}-control.{namespace}.svc:8080`. None
        # sends every gateway call to `gateway_target`.
        self.control_target = control_target
        self._channel_options = channel_options
        self._channel: grpc.aio.Channel | None = None
        self._stub: sandbox_service_pb2_grpc.SandboxServiceAsyncStub | None = None
        self._gateway_channel: grpc.aio.Channel | None = None
        self._gateway_stub: runner_gateway_pb2_grpc.RunnerGatewayAsyncStub | None = None

    def dial(self, target: str) -> grpc.aio.Channel:
        # No retry service config: uncertain mutations need domain-specific reconciliation.
        # A local subchannel pool makes a channel after `redial()` dial anew; from the process-wide
        # pool it would inherit the closed channel's connection.
        return grpc.aio.insecure_channel(
            target,
            options=grpc_channel_option_kvps(
                self._channel_options, required={"grpc.enable_retries": 0, "grpc.use_local_subchannel_pool": 1}
            ),
        )

    @property
    def stub(self) -> sandbox_service_pb2_grpc.SandboxServiceAsyncStub:
        if self._stub is None:
            self._channel = self.dial(self.target)
            self._stub = sandbox_service_pb2_grpc.SandboxServiceStub(self._channel)
        return self._stub

    @property
    def gateway(self) -> runner_gateway_pb2_grpc.RunnerGatewayAsyncStub:
        if self._gateway_stub is None:
            self._gateway_channel = self.dial(self.gateway_target)
            self._gateway_stub = runner_gateway_pb2_grpc.RunnerGatewayStub(self._gateway_channel)
        return self._gateway_stub

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

    async def held_unary[T](
        self,
        destination: SandboxDestination,
        call: Callable[[runner_gateway_pb2_grpc.RunnerGatewayAsyncStub], Callable[..., Awaitable[T]]],
        request: Message,
        *,
        timeout_s: float | None = None,
    ) -> T:
        """Call the replica holding the Sandbox's runner connection, through its control Service, on a
        connection dialed for this call alone: a replica that no longer holds it refuses with
        UNAVAILABLE, and the caller's retry resolves the control Service again."""
        if self.control_target is not None:
            async with self.dial(
                self.control_target.format(sandbox=destination.sandbox, namespace=destination.owner.namespace)
            ) as channel:
                try:
                    return await self.unary(
                        call(runner_gateway_pb2_grpc.RunnerGatewayStub(channel)), request, timeout_s=timeout_s
                    )
                except ServiceError as error:
                    if error.code != grpc.StatusCode.UNAVAILABLE:
                        raise
                    # CLEANUP(added 2026-10-11): Call through the control Service alone once no live
                    # Sandbox's runner lacks the control stream. Until then a Sandbox whose runner never
                    # connected has no control Service. Any replica serves its inbound route and refuses a
                    # control-routed Sandbox before acting unless it holds the connection, so this retry
                    # never takes such a Sandbox's operation another way.
                    logger.debug("Sandbox %s: control Service unavailable, calling the gateway", destination.sandbox)
        return await self.unary(call(self.gateway), request, timeout_s=timeout_s)

    async def list_sandboxes(self) -> list[Sandbox]:
        result = await self.unary(self.stub.ListSandboxes, Empty())
        return list(result.sandboxes)

    async def list_templates(self) -> list[str]:
        result = await self.unary(self.stub.ListTemplates, Empty())
        return list(result.templates)

    async def get(self, name: str) -> Sandbox:
        try:
            result = await self.unary(self.stub.GetSandbox, sandbox_service_pb2.GetSandboxRequest(name=name))
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
        await self.unary(call, sandbox_service_pb2.SandboxRequest(destination=destination))

    async def suspend(self, name: str) -> None:
        await self._lifecycle(self.stub.SuspendSandbox, name)

    async def resume(self, name: str) -> None:
        await self._lifecycle(self.stub.ResumeSandbox, name)

    async def delete(self, name: str) -> None:
        await self._lifecycle(self.stub.DeleteSandbox, name)

    async def grant_egress(self, sandbox: Sandbox, policies: list[str]) -> str:
        destination = SandboxDestination(owner=sandbox.service_account, sandbox=sandbox.name, sandbox_uid=sandbox.uid)
        result = await self.unary(
            self.stub.GrantEgress,
            sandbox_service_pb2.GrantEgressRequest(destination=destination, egress_policies=policies),
        )
        return result.binding_name

    async def revoke_egress(self, name: str) -> None:
        await self.unary(self.stub.RevokeEgress, sandbox_service_pb2.RevokeEgressRequest(binding_name=name))

    async def watch_sessions(self, *, after_position: int) -> AsyncIterator[sandbox_service_pb2.SessionChange]:
        """Yield changes after the position until planned renewal, which returns; anything else raises."""
        call = self.gateway.WatchSessions(
            sandbox_service_pb2.WatchSessionsRequest(after_position=after_position),
            metadata=await self.metadata(),
            timeout=self.follow_timeout_s,
        )
        try:
            while True:
                try:
                    message = await call.read()
                except grpc.aio.AioRpcError as error:
                    _raise(error)
                if message is grpc.aio.EOF:
                    raise ConnectionError("Sandbox Service ended the Session feed without planned renewal")
                assert isinstance(message, sandbox_service_pb2.WatchSessionsResponse)
                if message.HasField("reconnect_required"):
                    return
                yield message.change
        finally:
            call.cancel()

    def runner(self, destination: SandboxDestination) -> Runner:
        return Runner(self, destination)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.redial()

    async def redial(self) -> None:
        """Close the open channels; the next call dials fresh ones."""
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None
        if self._gateway_channel is not None:
            await self._gateway_channel.close()
            self._gateway_channel = None
            self._gateway_stub = None


class Runner:
    def __init__(self, service: SandboxServiceClient, destination: SandboxDestination) -> None:
        self.service = service
        self.destination = destination

    async def list_sessions(self) -> list[runner_pb2.SessionSummary]:
        result = await self.service.held_unary(
            self.destination,
            lambda stub: stub.ListSessions,
            sandbox_service_pb2.SandboxRequest(destination=self.destination),
        )
        return list(result.sessions)

    async def create(
        self, *, session_id: UUID, spec: dict[str, object], setup_script: str | None = None
    ) -> sandbox_service_pb2.CreateSessionResponse:
        """Open the Session the caller minted `session_id` for; a retry passes the same ID and inputs.
        The response's Session ID is authoritative: a Sandbox Service that predates client-minted IDs
        mints its own."""
        return await self.service.held_unary(
            self.destination,
            lambda stub: stub.CreateSession,
            wire.create_proto(self.destination, session_id, spec, setup_script),
            timeout_s=self.service.lifecycle_timeout_s,
        )

    async def lookup(self, *, idempotency_key: str) -> sandbox_service_pb2.LookupSessionResponse:
        return await self.service.unary(
            self.service.gateway.LookupSession,
            sandbox_service_pb2.LookupSessionRequest(sandbox=self.destination, idempotency_key=idempotency_key),
        )

    async def place_hold(self, session_id: str, *, holder: str) -> sandbox_service_pb2.Hold:
        destination = sandbox_service_pb2.SessionDestination(sandbox=self.destination, session_id=session_id)
        return await self.service.unary(
            self.service.stub.PlaceHold, sandbox_service_pb2.HoldRequest(destination=destination, holder=holder)
        )

    async def confirm_hold(self, session_id: str, *, holder: str, through_cursor: int) -> sandbox_service_pb2.Hold:
        destination = sandbox_service_pb2.SessionDestination(sandbox=self.destination, session_id=session_id)
        return await self.service.unary(
            self.service.stub.ConfirmHold,
            sandbox_service_pb2.ConfirmHoldRequest(
                destination=destination, holder=holder, through_cursor=through_cursor
            ),
        )

    async def seal(self) -> runner_pb2.SealResponse:
        """Teardown's seal, which only the Sandbox Service may ask for, each Session named as retention
        holders name it."""
        return await self.service.held_unary(
            self.destination,
            lambda stub: stub.SealSandbox,
            sandbox_service_pb2.SandboxRequest(destination=self.destination),
            timeout_s=self.service.lifecycle_timeout_s,
        )

    async def resume(self, session_id: str) -> runner_pb2.Attached:
        return await self.service.held_unary(
            self.destination,
            lambda stub: stub.ResumeSession,
            sandbox_service_pb2.SessionRequest(
                destination=sandbox_service_pb2.SessionDestination(sandbox=self.destination, session_id=session_id)
            ),
            timeout_s=self.service.lifecycle_timeout_s,
        )

    async def command(
        self, session_id: str, command: command_pb2.Command, *, after_cursor: int
    ) -> event_log_pb2.EventEntry:
        if self.service.command_admission_timeout_s is None:
            raise ValueError("SubmitCommand requires an explicit client deadline")
        receipt = await self.service.unary(
            self.service.gateway.SubmitCommand,
            sandbox_service_pb2.SubmitCommandRequest(
                destination=sandbox_service_pb2.SessionDestination(sandbox=self.destination, session_id=session_id),
                command=command,
                follow=event_log_pb2.Follow(after_cursor=after_cursor),
            ),
            timeout_s=self.service.command_admission_timeout_s,
        )
        if not receipt.event.HasField("command_admitted") or receipt.event.command_admitted.command != command:
            raise ConnectionError("Sandbox Service did not return the exact command admission")
        return receipt

    async def get_command(self, session_id: str, command_id: str) -> sandbox_service_pb2.CommandStatus:
        return await self.service.unary(
            self.service.gateway.GetCommand,
            sandbox_service_pb2.GetCommandRequest(
                destination=sandbox_service_pb2.SessionDestination(sandbox=self.destination, session_id=session_id),
                command_id=command_id,
            ),
        )

    async def attach(self, session_id: str, *, after_cursor: int = 0) -> Attachment:
        call = self.service.gateway.FollowSession(
            sandbox_service_pb2.FollowSessionRequest(
                destination=sandbox_service_pb2.SessionDestination(sandbox=self.destination, session_id=session_id),
                follow=event_log_pb2.Follow(after_cursor=after_cursor),
            ),
            metadata=await self.service.metadata(),
            timeout=self.service.follow_timeout_s,
        )
        try:
            async with asyncio.timeout(self.service.request_timeout_s):
                message = await call.read()
            if not isinstance(message, sandbox_service_pb2.FollowSessionResponse) or not message.HasField("attached"):
                raise ConnectionError("Sandbox Service did not provide an Attached snapshot")
            return Attachment(call, message.attached)
        except BaseException as error:
            call.cancel()
            if isinstance(error, grpc.aio.AioRpcError):
                _raise(error)
            raise

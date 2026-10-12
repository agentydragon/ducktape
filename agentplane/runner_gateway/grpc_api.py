"""Authenticated gRPC boundary for Sessions and commands. Runners remain the command and execution-event
authority."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import override
from uuid import UUID

import grpc
from google.protobuf.json_format import ParseError
from sqlalchemy.exc import SQLAlchemyError

from agentplane.grpc_servicer import Abort, ServicerResources, Status, status_mapping
from agentplane.proto import (
    runner_control_pb2,
    runner_gateway_pb2,
    runner_gateway_pb2_grpc,
    runner_pb2,
    sandbox_service_pb2,
)
from agentplane.proto.sandbox_service_pb2 import SandboxBinding, SandboxDestination
from agentplane.runner.errors import RunnerError
from agentplane.runner_gateway import session_lifecycle, wire
from agentplane.runner_gateway.destinations import DestinationResolver, DestinationUnavailableError
from agentplane.runner_gateway.lifecycle_control import ConnectionNotHeldError, LifecycleControl
from agentplane.runner_gateway.sealing import SEAL_S, Sealing
from agentplane.runner_gateway.session_registry import SessionConflictError, SessionNotFoundError, SessionRegistry
from agentplane.sandbox_service.errors import InventoryError, SandboxNotFoundError
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.grpc_caller import authenticate

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Resources(ServicerResources):
    """What `RunnerGateway` serves from."""

    destinations: DestinationResolver
    platform_instructions: str
    runner_admission_ack_timeout_s: float
    sessions: SessionRegistry
    control: LifecycleControl
    # The Sandbox Service's account, which alone may seal a Sandbox for teardown.
    sealer_accounts: frozenset[ServiceAccountRef]
    admission_timeout_s: float
    lifecycle_timeout_s: float

    @property
    def sealing(self) -> Sealing:
        return Sealing(self.sessions, control=self.control)


def _runner_status(error: grpc.aio.AioRpcError) -> Status:
    """A `Runner` call's failure: the runner's refusal, or the runner connection's loss or deadline."""
    match code := error.code():
        case grpc.StatusCode.UNAVAILABLE:
            return code, "destination unavailable; no offline admission"
        case grpc.StatusCode.DEADLINE_EXCEEDED:
            return code, "service deadline expired"
        case grpc.StatusCode.INVALID_ARGUMENT:
            return code, f"runner refused the request: {code.name}"
    return grpc.StatusCode.FAILED_PRECONDITION, f"runner refused the request: {code.name}"


# No Kubernetes wire responses or bearer data reach clients.
errors = status_mapping(
    Abort((SandboxNotFoundError, SessionNotFoundError), (grpc.StatusCode.NOT_FOUND, "sandbox incarnation not found")),
    Abort(SessionConflictError, (grpc.StatusCode.ALREADY_EXISTS, "session ID already belongs to another Sandbox")),
    Abort(grpc.aio.AioRpcError, _runner_status),
    Abort((ValueError, ParseError), (grpc.StatusCode.INVALID_ARGUMENT, "invalid service request")),
    Abort(
        (InventoryError, RunnerError),
        (grpc.StatusCode.FAILED_PRECONDITION, "runner or sandbox state refused the request"),
    ),
    deadline="service deadline expired",
    unavailable="destination unavailable; no offline admission",
    also_unavailable=(DestinationUnavailableError, SQLAlchemyError),
)


def parse_session_id(session_id: str) -> UUID:
    """A public Session ID, in its canonical form only."""
    parsed = UUID(session_id)
    if str(parsed) != session_id:
        raise ValueError("session ID must be a canonical UUID")
    return parsed


class RunnerGateway(runner_gateway_pb2_grpc.RunnerGatewayServicer):
    """Every request travels over the Sandbox's runner control stream. Lifecycle requests are served
    only by the replica holding that connection; any other refuses before acting, and the caller
    retries through the control Service."""

    def __init__(self, resources: Resources) -> None:
        self.resources = resources

    @asynccontextmanager
    async def request(
        self, context: grpc.aio.ServicerContext, *, timeout_s: float | None = None
    ) -> AsyncIterator[None]:
        async with errors(context), asyncio.timeout(timeout_s or self.resources.admission_timeout_s):
            await self.resources.authenticate(context)
            yield

    async def held(self, destination: SandboxDestination, *, writes: bool) -> SandboxBinding | None:
        """Verify the incarnation and its owner, and that this replica holds its runner connection.
        Returns the Sandbox's binding."""
        binding = await self.resources.destinations.resolve(destination, writes=writes)
        if self.resources.control.holder(UUID(destination.sandbox_uid)) is None:
            raise ConnectionNotHeldError("another replica holds this Sandbox's runner connection")
        return binding

    async def list_runner_sessions(self, destination: SandboxDestination) -> list[runner_pb2.SessionSummary]:
        await self.held(destination, writes=False)
        runner = self.resources.control.runner(UUID(destination.sandbox_uid))
        return list((await runner.ListSessions(runner_pb2.ListSessionsRequest())).sessions)

    @override
    async def ListSessions(
        self, request: runner_gateway_pb2.ListSessionsRequest, context: grpc.aio.ServicerContext
    ) -> runner_gateway_pb2.ListSessionsResponse:
        async with self.request(context):
            sessions = []
            for summary in await self.list_runner_sessions(request.destination):
                public_id = await self.resources.sessions.public_id(
                    summary.session_id,
                    sandbox_namespace=self.resources.destinations.sandboxes.namespace,
                    sandbox_name=request.destination.sandbox,
                    sandbox_uid=UUID(request.destination.sandbox_uid),
                )
                if public_id is None:
                    logger.warning("runner Session %s has no registered Session; not listed", summary.session_id)
                    continue
                summary.session_id = str(public_id)
                sessions.append(summary)
            return runner_gateway_pb2.ListSessionsResponse(sessions=sessions)

    async def runner_session_id(self, destination: sandbox_service_pb2.SessionDestination) -> str:
        """The runner's ID for a Session registered in the destination's Sandbox incarnation."""
        session_id = parse_session_id(destination.session_id)
        await self.resources.sessions.require(
            session_id,
            sandbox_namespace=self.resources.destinations.sandboxes.namespace,
            sandbox_name=destination.sandbox.sandbox,
            sandbox_uid=UUID(destination.sandbox.sandbox_uid),
        )
        return str(session_id)

    @override
    async def CreateSession(
        self, request: runner_gateway_pb2.CreateSessionRequest, context: grpc.aio.ServicerContext
    ) -> runner_gateway_pb2.CreateSessionResponse:
        async with errors(context), asyncio.timeout(self.resources.lifecycle_timeout_s):
            await self.resources.authenticate(context)
            if len(request.setup_script) > 65_536:
                raise ValueError("setup script is too long")
            if not request.session_id:
                raise ValueError("session ID is required")
            session_id = parse_session_id(request.session_id)
            overrides = wire.launch_overrides(request)
            binding = await self.held(request.sandbox, writes=True)
            sandbox_uid = UUID(request.sandbox.sandbox_uid)
            await self.resources.sessions.register(
                session_id,
                sandbox_namespace=self.resources.destinations.sandboxes.namespace,
                sandbox_name=request.sandbox.sandbox,
                sandbox_uid=sandbox_uid,
            )
            spec = session_lifecycle.launch_spec(
                sandbox_service_pb2.SessionDestination(sandbox=request.sandbox, session_id=str(session_id)),
                overrides,
                binding=binding,
                platform_instructions=self.resources.platform_instructions,
                sandbox_namespace=self.resources.destinations.sandboxes.namespace,
            )
            setup_script = (
                request.setup_script
                if request.HasField("setup_script")
                else binding.session_defaults.setup_script
                if binding is not None and binding.session_defaults.HasField("setup_script")
                else None
            )
            # A retry recomputes the spec; the runner confirms it against the one it recorded.
            attachment = await session_lifecycle.open_session(
                self.resources.control.runner(sandbox_uid),
                str(session_id),
                spec,
                bootstrap=binding.bootstrap if binding is not None else "",
                setup_script=setup_script,
            )
            attachment.session_id = str(session_id)
            return runner_gateway_pb2.CreateSessionResponse(session_id=str(session_id), attached=attachment)

    @override
    async def ResumeSession(
        self, request: runner_gateway_pb2.ResumeSessionRequest, context: grpc.aio.ServicerContext
    ) -> runner_gateway_pb2.ResumeSessionResponse:
        async with self.request(context, timeout_s=self.resources.lifecycle_timeout_s):
            destination = request.destination
            if not destination.session_id:
                raise ValueError("session ID is required")
            # Refuses a Sandbox being torn down.
            await self.held(destination.sandbox, writes=True)
            runner = self.resources.control.runner(UUID(destination.sandbox.sandbox_uid))
            resumed = await runner.ResumeSession(
                runner_control_pb2.ResumeSessionRequest(session_id=await self.runner_session_id(destination))
            )
            result = resumed.attached
            result.session_id = destination.session_id
            return runner_gateway_pb2.ResumeSessionResponse(attached=result)

    @override
    async def SubmitCommand(
        self, request: runner_gateway_pb2.SubmitCommandRequest, context: grpc.aio.ServicerContext
    ) -> runner_gateway_pb2.SubmitCommandResponse:
        async with self.request(context, timeout_s=self.resources.runner_admission_ack_timeout_s):
            if not 1 <= len(request.command.command_id) <= 128 or request.command.WhichOneof("operation") is None:
                raise ValueError("a command needs a 1 to 128 character ID and an operation")
            destination = request.destination
            # Refuses a Sandbox being torn down.
            await self.held(destination.sandbox, writes=True)
            delivery = runner_control_pb2.DeliverCommandRequest(
                session_id=await self.runner_session_id(destination), command=request.command
            )
            try:
                delivered = await self.resources.control.runner(UUID(destination.sandbox.sandbox_uid)).DeliverCommand(
                    delivery
                )
            except grpc.aio.AioRpcError as error:
                # Only here does the runner's conflict mean the caller's own ID clashed.
                if error.code() == grpc.StatusCode.ALREADY_EXISTS:
                    await context.abort(error.code(), "the command ID was admitted for other work")
                raise
            return runner_gateway_pb2.SubmitCommandResponse(admission=delivered.admission)

    @override
    async def BatchGetSessionSandboxes(
        self, request: runner_gateway_pb2.BatchGetSessionSandboxesRequest, context: grpc.aio.ServicerContext
    ) -> runner_gateway_pb2.BatchGetSessionSandboxesResponse:
        async with self.request(context):
            session_ids = {UUID(session_id) for session_id in request.session_ids}
            return runner_gateway_pb2.BatchGetSessionSandboxesResponse(
                sessions=await self.resources.sessions.sandboxes(session_ids)
            )

    @override
    async def FindSessions(
        self, request: runner_gateway_pb2.FindSessionsRequest, context: grpc.aio.ServicerContext
    ) -> runner_gateway_pb2.FindSessionsResponse:
        async with self.request(context):
            match request.WhichOneof("sandbox"):
                case "sandbox_name":
                    found = await self.resources.sessions.find(sandbox_name=request.sandbox_name)
                case "sandbox_name_contains":
                    found = await self.resources.sessions.find(sandbox_name_contains=request.sandbox_name_contains)
                case _:
                    raise ValueError("a Sandbox name match is required")
            return runner_gateway_pb2.FindSessionsResponse(sessions=found)

    @override
    async def SealSandbox(
        self, request: runner_gateway_pb2.SealSandboxRequest, context: grpc.aio.ServicerContext
    ) -> runner_gateway_pb2.SealSandboxResponse:
        async with errors(context), asyncio.timeout(SEAL_S + self.resources.admission_timeout_s):
            await authenticate(self.resources.principals, self.resources.sealer_accounts, context)
            view = await self.resources.destinations.sandboxes.incarnation(request.destination)
            sealed = await self.resources.sealing.seal(
                namespace=view.namespace, name=view.name, sandbox_uid=UUID(view.uid)
            )
            return runner_gateway_pb2.SealSandboxResponse(sessions=sealed.sessions)


def add_service(resources: Resources, server: grpc.aio.Server) -> None:
    runner_gateway_pb2_grpc.add_RunnerGatewayServicer_to_server(RunnerGateway(resources), server)

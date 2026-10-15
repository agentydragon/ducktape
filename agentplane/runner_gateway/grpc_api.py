"""Authenticated gRPC boundary for Sessions and commands. Runners remain the command and execution-event
authority."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import override
from uuid import UUID

import grpc
from google.protobuf.json_format import ParseError
from sqlalchemy.exc import SQLAlchemyError

from agentplane.grpc_servicer import Abort, ServicerResources, Status, status_mapping
from agentplane.proto import runner_control_pb2, runner_gateway_pb2, runner_gateway_pb2_grpc, runner_pb2, sandboxes_pb2
from agentplane.proto.sandboxes_pb2 import SandboxBinding, SandboxDestination
from agentplane.runner_gateway import session_lifecycle
from agentplane.runner_gateway.destinations import DestinationResolver, DestinationUnavailableError
from agentplane.runner_gateway.lifecycle_control import ConnectionNotHeldError, LifecycleControl
from agentplane.runner_gateway.session_registry import SessionConflictError, SessionNotFoundError, SessionRegistry
from agentplane.sandboxes.errors import InventoryError, SandboxNotFoundError

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio


@dataclass(frozen=True)
class Resources(ServicerResources):
    """What `RunnerGateway` serves from."""

    destinations: DestinationResolver
    runner_admission_ack_timeout_s: float
    sessions: SessionRegistry
    control: LifecycleControl
    admission_timeout_s: float
    lifecycle_timeout_s: float


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
    Abort(InventoryError, (grpc.StatusCode.FAILED_PRECONDITION, "runner or sandbox state refused the request")),
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
            return runner_gateway_pb2.ListSessionsResponse(
                sessions=await self.list_runner_sessions(request.destination)
            )

    async def runner_session_id(self, destination: sandboxes_pb2.SessionDestination) -> str:
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
            session_lifecycle.validate_spec(request.spec)
            await self.held(request.sandbox, writes=True)
            sandbox_uid = UUID(request.sandbox.sandbox_uid)
            await self.resources.sessions.register(
                session_id,
                sandbox_namespace=self.resources.destinations.sandboxes.namespace,
                sandbox_name=request.sandbox.sandbox,
                sandbox_uid=sandbox_uid,
            )
            setup_script = request.setup_script if request.HasField("setup_script") else None
            opening = runner_control_pb2.OpenSessionRequest(session_id=str(session_id), spec=request.spec)
            if setup_script is not None:
                opening.setup_script = setup_script
            # A retry recomputes the spec; the runner confirms it against the one it recorded.
            attachment = (await self.resources.control.runner(sandbox_uid).OpenSession(opening)).attached
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


def add_service(resources: Resources, server: grpc.aio.Server) -> None:
    runner_gateway_pb2_grpc.add_RunnerGatewayServicer_to_server(RunnerGateway(resources), server)

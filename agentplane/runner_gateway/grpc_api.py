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
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.exc import SQLAlchemyError

from agentplane.proto import (
    event_log_pb2,
    runner_control_pb2,
    runner_gateway_pb2,
    runner_gateway_pb2_grpc,
    runner_pb2,
    sandbox_service_pb2,
)
from agentplane.proto.sandbox_service_pb2 import SandboxBinding, SandboxDestination
from agentplane.runner.errors import RunnerError
from agentplane.runner_gateway import session_lifecycle
from agentplane.runner_gateway.destinations import DestinationResolver, DestinationUnavailableError
from agentplane.runner_gateway.lifecycle_control import (
    CommandRefusedError,
    ConnectionNotHeldError,
    ControlRefusedError,
    LifecycleControl,
)
from agentplane.runner_gateway.sealing import SEAL_S, Sealing
from agentplane.runner_gateway.session_registry import SessionConflictError, SessionNotFoundError, SessionRegistry
from agentplane.sandbox_service import wire
from agentplane.sandbox_service.errors import InventoryError, SandboxNotFoundError
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.grpc_caller import authenticate
from agentplane.workload_auth.principal import WorkloadPrincipalRejectedError, WorkloadPrincipalResolver

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Resources:
    """What `RunnerGateway` serves from."""

    principals: WorkloadPrincipalResolver
    destinations: DestinationResolver
    caller_accounts: frozenset[ServiceAccountRef]
    platform_instructions: str
    runner_admission_ack_timeout_s: float
    sessions: SessionRegistry
    control: LifecycleControl
    # The Sandbox Service's account, which alone may seal a Sandbox for teardown.
    sealer_accounts: frozenset[ServiceAccountRef]
    admission_timeout_s: float
    lifecycle_timeout_s: float

    def __post_init__(self) -> None:
        if min(self.admission_timeout_s, self.runner_admission_ack_timeout_s, self.lifecycle_timeout_s) <= 0:
            raise ValueError("timeouts must be positive")
        if not self.caller_accounts:
            raise ValueError("at least one service caller is required")

    async def authenticate(self, context: grpc.aio.ServicerContext) -> ServiceAccountRef:
        return await authenticate(self.principals, self.caller_accounts, context)

    @property
    def sealing(self) -> Sealing:
        return Sealing(self.sessions, control=self.control)


@asynccontextmanager
async def errors(context: grpc.aio.ServicerContext) -> AsyncIterator[None]:
    """Sanitize backend failures; no Kubernetes wire responses or bearer data reach clients."""
    try:
        yield
    except WorkloadPrincipalRejectedError:
        await context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid workload bearer")
    except SandboxNotFoundError, SessionNotFoundError:
        await context.abort(grpc.StatusCode.NOT_FOUND, "sandbox incarnation not found")
    except SessionConflictError:
        await context.abort(grpc.StatusCode.ALREADY_EXISTS, "session ID already belongs to another Sandbox")
    except CommandRefusedError as refused:
        code = {
            runner_control_pb2.CommandRefused.REASON_COMMAND_CONFLICT: grpc.StatusCode.ALREADY_EXISTS,
            runner_control_pb2.CommandRefused.REASON_INVALID_COMMAND: grpc.StatusCode.INVALID_ARGUMENT,
        }.get(refused.refusal.reason, grpc.StatusCode.FAILED_PRECONDITION)
        await context.abort(code, f"runner refused the command: {refused}")
    except ControlRefusedError as refused:
        if refused.refusal.reason == runner_control_pb2.ControlRefused.REASON_INVALID_REQUEST:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "runner rejected invalid request")
        await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "runner rejected session or bootstrap state")
    except ValueError, ParseError:
        await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid service request")
    except TimeoutError:
        await context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "service deadline expired")
    except InventoryError, RunnerError:
        await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "runner or sandbox state refused the request")
    except DestinationUnavailableError, ConnectionError, k8s_client.ApiException, SQLAlchemyError:
        await context.abort(grpc.StatusCode.UNAVAILABLE, "destination unavailable; no offline admission")


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
        listing = runner_control_pb2.ControlRequest(list_sessions=runner_pb2.ListSessionsRequest())
        result = await self.resources.control.request(UUID(destination.sandbox_uid), listing)
        return list(result.list_sessions.sessions)

    @override
    async def ListSessions(
        self, request: sandbox_service_pb2.SandboxRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.ListSessionsResponse:
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
            return runner_pb2.ListSessionsResponse(sessions=sessions)

    @override
    async def CreateSession(
        self, request: sandbox_service_pb2.CreateSessionRequest, context: grpc.aio.ServicerContext
    ) -> sandbox_service_pb2.CreateSessionResponse:
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
                self.resources.control,
                sandbox_uid,
                str(session_id),
                spec,
                bootstrap=binding.bootstrap if binding is not None else "",
                setup_script=setup_script,
            )
            attachment.session_id = str(session_id)
            return sandbox_service_pb2.CreateSessionResponse(session_id=str(session_id), attached=attachment)

    @override
    async def ResumeSession(
        self, request: runner_gateway_pb2.ResumeSessionRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.Attached:
        async with self.request(context, timeout_s=self.resources.lifecycle_timeout_s):
            public_id = parse_session_id(request.session_id)
            placement = await self.resources.sessions.placement(public_id)
            # Refuses a Sandbox incarnation that is gone or being torn down.
            await self.resources.destinations.resolve_incarnation(
                placement.sandbox_name, placement.sandbox_uid, writes=True
            )
            result = await session_lifecycle.resume_session(
                self.resources.control, placement.sandbox_uid, str(public_id)
            )
            result.session_id = str(public_id)
            return result

    @override
    async def SubmitCommand(
        self, request: sandbox_service_pb2.SubmitCommandRequest, context: grpc.aio.ServicerContext
    ) -> event_log_pb2.EventEntry:
        async with self.request(context, timeout_s=self.resources.runner_admission_ack_timeout_s):
            public_id = parse_session_id(request.session_id)
            if not 1 <= len(request.command.command_id) <= 128 or request.command.WhichOneof("operation") is None:
                raise ValueError("a command needs a 1 to 128 character ID and an operation")
            placement = await self.resources.sessions.placement(public_id)
            # Refuses a Sandbox incarnation that is gone or being torn down.
            await self.resources.destinations.resolve_incarnation(
                placement.sandbox_name, placement.sandbox_uid, writes=True
            )
            return await self.resources.control.deliver(placement.sandbox_uid, str(public_id), request.command)

    @override
    async def BatchGetSessionSandboxes(
        self, request: runner_gateway_pb2.BatchGetSessionSandboxesRequest, context: grpc.aio.ServicerContext
    ) -> runner_gateway_pb2.SessionSandboxes:
        async with self.request(context):
            session_ids = {UUID(session_id) for session_id in request.session_ids}
            return runner_gateway_pb2.SessionSandboxes(sessions=await self.resources.sessions.sandboxes(session_ids))

    @override
    async def FindSessions(
        self, request: runner_gateway_pb2.FindSessionsRequest, context: grpc.aio.ServicerContext
    ) -> runner_gateway_pb2.SessionSandboxes:
        async with self.request(context):
            match request.WhichOneof("sandbox"):
                case "sandbox_name":
                    found = await self.resources.sessions.find(sandbox_name=request.sandbox_name)
                case "sandbox_name_contains":
                    found = await self.resources.sessions.find(sandbox_name_contains=request.sandbox_name_contains)
                case _:
                    raise ValueError("a Sandbox name match is required")
            return runner_gateway_pb2.SessionSandboxes(sessions=found)

    @override
    async def SealSandbox(
        self, request: sandbox_service_pb2.SandboxRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.SealResponse:
        async with errors(context), asyncio.timeout(SEAL_S + self.resources.admission_timeout_s):
            await authenticate(self.resources.principals, self.resources.sealer_accounts, context)
            view = await self.resources.destinations.sandboxes.incarnation(request.destination)
            return await self.resources.sealing.seal(view)


def add_service(resources: Resources, server: grpc.aio.Server) -> None:
    runner_gateway_pb2_grpc.add_RunnerGatewayServicer_to_server(RunnerGateway(resources), server)

"""Authenticated gRPC boundary for Sessions and commands. Runners remain the command and execution-event
authority."""

import asyncio
import json
import logging
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import override
from uuid import UUID

import grpc
from google.protobuf.json_format import ParseError
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.exc import SQLAlchemyError

from agentplane.proto import (
    command_pb2,
    event_log_pb2,
    runner_control_pb2 as control_pb2,
    runner_gateway_pb2,
    runner_gateway_pb2_grpc,
    runner_pb2,
    sandbox_service_pb2,
)
from agentplane.proto.sandbox_service_pb2 import SandboxBinding, SandboxDestination
from agentplane.runner.errors import RunnerError
from agentplane.runner_gateway import session_lifecycle
from agentplane.runner_gateway.command_delivery import CommandDelivery, CommandRefusedError
from agentplane.runner_gateway.commands.store import (
    CommandSubmission,
    Submission,
    SubmissionConflictError,
    SubmissionNotFoundError,
    SubmissionState,
    SubmissionStore,
    submit_command,
)
from agentplane.runner_gateway.destinations import DestinationResolver, DestinationUnavailableError
from agentplane.runner_gateway.lifecycle_control import ConnectionNotHeldError, ControlRefusedError, LifecycleControl
from agentplane.runner_gateway.sealing import SEAL_S, Sealing
from agentplane.runner_gateway.session_registry import SessionNotFoundError, SessionRegistry
from agentplane.sandbox_service import wire
from agentplane.sandbox_service.models import InventoryError, SandboxNotFoundError
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
    submissions: SubmissionStore
    delivery: CommandDelivery
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
    except SubmissionNotFoundError:
        await context.abort(grpc.StatusCode.NOT_FOUND, "command not found")
    except SubmissionConflictError:
        await context.abort(grpc.StatusCode.ALREADY_EXISTS, "command ID already belongs to a different submission")
    except CommandRefusedError as refused:
        code = {
            control_pb2.CommandRefused.REASON_COMMAND_CONFLICT: grpc.StatusCode.ALREADY_EXISTS,
            control_pb2.CommandRefused.REASON_INVALID_COMMAND: grpc.StatusCode.INVALID_ARGUMENT,
        }.get(refused.refusal.reason, grpc.StatusCode.FAILED_PRECONDITION)
        await context.abort(code, f"runner refused the command: {refused}")
    except ControlRefusedError as refused:
        if refused.refusal.reason == control_pb2.ControlRefused.REASON_INVALID_REQUEST:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "runner rejected invalid request")
        await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "runner rejected session or bootstrap state")
    except ValueError, ParseError:
        await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid service request")
    except TimeoutError:
        await context.abort(
            grpc.StatusCode.DEADLINE_EXCEEDED, "service deadline expired; mutation outcome may be uncertain"
        )
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


SUBMISSION_STATES = {
    sandbox_service_pb2.COMMAND_STATE_PENDING_ADMISSION: SubmissionState.PENDING_ADMISSION,
    sandbox_service_pb2.COMMAND_STATE_ADMITTED: SubmissionState.ADMITTED,
    sandbox_service_pb2.COMMAND_STATE_REJECTED: SubmissionState.REJECTED,
}
COMMAND_STATES = {value: key for key, value in SUBMISSION_STATES.items()}


def command_status(submission: Submission) -> sandbox_service_pb2.CommandStatus:
    result = sandbox_service_pb2.CommandStatus(command=submission.command, state=COMMAND_STATES[submission.state])
    result.accepted_at.FromDatetime(submission.accepted_at)
    if submission.admission is not None:
        result.admission.CopyFrom(submission.admission)
    if submission.rejection is not None:
        result.rejection = submission.rejection
    return result


def write_page_token(accepted_at: datetime, command_id: str) -> str:
    """An opaque position in a Session's acceptance order."""
    return urlsafe_b64encode(json.dumps([accepted_at.isoformat(), command_id]).encode()).decode()


def read_page_token(token: str) -> tuple[datetime, str]:
    match json.loads(urlsafe_b64decode(token.encode())):
        case [str() as accepted_at, str() as command_id]:
            return datetime.fromisoformat(accepted_at), command_id
        case _:
            raise ValueError("invalid page token")


class RunnerGateway(runner_gateway_pb2_grpc.RunnerGatewayServicer):
    """Every request travels over the Sandbox's runner control stream. Lifecycle requests are served
    only by the replica holding that connection; any other refuses before acting, and the caller
    retries through the control Service. Commands reach the holder through command delivery."""

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
        listing = control_pb2.ControlRequest(list_sessions=runner_pb2.ListSessionsRequest())
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
        self, request: sandbox_service_pb2.CreateSessionRequest, context: grpc.aio.ServicerContext
    ) -> sandbox_service_pb2.CreateSessionResponse:
        async with errors(context), asyncio.timeout(self.resources.lifecycle_timeout_s):
            caller = await self.resources.authenticate(context)
            if len(request.idempotency_key) > 128 or len(request.setup_script) > 65_536:
                raise ValueError("Open key or setup script is too long")
            if not request.session_id:
                raise ValueError("session ID is required")
            client_id = parse_session_id(request.session_id)
            overrides = wire.launch_overrides(request)
            binding = await self.held(request.sandbox, writes=True)
            sandbox_uid = UUID(request.sandbox.sandbox_uid)

            def freeze(session_id: UUID) -> bytes:
                destination = sandbox_service_pb2.SessionDestination(
                    sandbox=request.sandbox, session_id=str(session_id)
                )
                # Resolve workspace templates against the reserved public ID, and freeze the result.
                effective_overrides = dict(overrides)
                if isinstance(cwd := effective_overrides.get("cwd"), str):
                    effective_overrides["cwd"] = cwd.replace("{session_id}", str(session_id))
                spec = session_lifecycle.launch_spec(
                    destination,
                    effective_overrides,
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
                launch = sandbox_service_pb2.FrozenLaunch(
                    open=runner_pb2.Open(session_id=str(session_id), spec=spec),
                    bootstrap=binding.bootstrap if binding is not None else "",
                )
                if setup_script is not None:
                    launch.open.setup_script = setup_script
                return launch.SerializeToString(deterministic=True)

            reservation = await self.resources.sessions.reserve(
                session_id=client_id,
                caller_namespace=caller.namespace,
                caller_name=caller.name,
                sandbox_namespace=self.resources.destinations.sandboxes.namespace,
                sandbox_name=request.sandbox.sandbox,
                sandbox_uid=sandbox_uid,
                open_key=request.idempotency_key or None,
                open_request=request.SerializeToString(deterministic=True),
                launch_spec=freeze,
            )
            launch = sandbox_service_pb2.FrozenLaunch.FromString(reservation.launch_spec)
            public_id = str(reservation.session_id)
            attachment = await session_lifecycle.open_session(
                self.resources.control,
                sandbox_uid,
                launch.open.session_id,
                launch.open.spec,
                bootstrap=launch.bootstrap,
                setup_script=launch.open.setup_script if launch.open.HasField("setup_script") else None,
            )
            attachment.session_id = public_id
            return sandbox_service_pb2.CreateSessionResponse(session_id=public_id, attached=attachment)

    @override
    async def LookupSession(
        self, request: sandbox_service_pb2.LookupSessionRequest, context: grpc.aio.ServicerContext
    ) -> sandbox_service_pb2.LookupSessionResponse:
        async with errors(context), asyncio.timeout(self.resources.admission_timeout_s):
            caller = await self.resources.authenticate(context)
            if not request.idempotency_key or len(request.idempotency_key) > 128:
                raise ValueError("Open key is required and must not exceed 128 characters")
            await self.resources.destinations.resolve(request.sandbox, writes=False)
            public_id = await self.resources.sessions.lookup_open(
                caller_namespace=caller.namespace,
                caller_name=caller.name,
                sandbox_namespace=self.resources.destinations.sandboxes.namespace,
                sandbox_name=request.sandbox.sandbox,
                sandbox_uid=UUID(request.sandbox.sandbox_uid),
                open_key=request.idempotency_key,
            )
            if public_id is None:
                return sandbox_service_pb2.LookupSessionResponse()
            result = sandbox_service_pb2.LookupSessionResponse(session_id=str(public_id))
            # DB reservation precedes runner Open, and a retained spec is not proof of a successful
            # Open: only the Session's first launch outcome is. A stopped Session may have run before.
            for summary in await self.list_runner_sessions(request.sandbox):
                if summary.session_id != str(public_id):
                    continue
                if summary.launch_outcome == runner_pb2.LAUNCH_OUTCOME_STARTED:
                    result.summary.CopyFrom(summary)
                    result.summary.session_id = str(public_id)
                elif summary.launch_outcome == runner_pb2.LAUNCH_OUTCOME_FAILED:
                    result.failed = True
                break
            return result

    @override
    async def ResumeSession(
        self, request: sandbox_service_pb2.SessionRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.Attached:
        async with self.request(context, timeout_s=self.resources.lifecycle_timeout_s):
            destination = request.destination
            if not destination.session_id:
                raise ValueError("session ID is required")
            # Refuses a Sandbox being torn down.
            await self.held(destination.sandbox, writes=True)
            result = await session_lifecycle.resume_session(
                self.resources.control, UUID(destination.sandbox.sandbox_uid), await self.runner_session_id(destination)
            )
            result.session_id = destination.session_id
            return result

    @override
    async def SubmitCommand(
        self, request: sandbox_service_pb2.SubmitCommandRequest, context: grpc.aio.ServicerContext
    ) -> event_log_pb2.EventEntry:
        async with errors(context), asyncio.timeout(self.resources.runner_admission_ack_timeout_s):
            caller = await self.resources.authenticate(context)
            public_id = parse_session_id(request.session_id)
            submission = CommandSubmission(command=request.command)
            # Nothing is persisted for a Session whose Sandbox incarnation is gone or going.
            placement = await self.resources.sessions.placement(public_id)
            await self.resources.destinations.resolve_incarnation(
                placement.sandbox_name, placement.sandbox_uid, writes=True
            )
            delivery = self.resources.delivery

            async def dispatch(command: command_pb2.Command) -> event_log_pb2.EventEntry:
                return await delivery.deliver(
                    sandbox_uid=placement.sandbox_uid,
                    session_id=public_id,
                    command_id=command.command_id,
                    runner_session_id=str(public_id),
                    deadline_s=min(self.resources.runner_admission_ack_timeout_s, 60),
                )

            return await submit_command(
                self.resources.submissions, public_id, submission, caller=caller, dispatch=dispatch
            )

    async def registered_session(self, session_id: str) -> UUID:
        """A registered public Session ID. Its submissions outlive its Sandbox."""
        public_id = parse_session_id(session_id)
        await self.resources.sessions.placement(public_id)
        return public_id

    @override
    async def GetCommand(
        self, request: sandbox_service_pb2.GetCommandRequest, context: grpc.aio.ServicerContext
    ) -> sandbox_service_pb2.CommandStatus:
        async with self.request(context):
            session_id = await self.registered_session(request.session_id)
            if not 1 <= len(request.command_id) <= 128:
                raise ValueError("command ID must contain 1 to 128 characters")
            return command_status(await self.resources.submissions.read(session_id, request.command_id))

    @override
    async def ListCommands(
        self, request: sandbox_service_pb2.ListCommandsRequest, context: grpc.aio.ServicerContext
    ) -> sandbox_service_pb2.ListCommandsResponse:
        async with self.request(context):
            session_id = await self.registered_session(request.session_id)
            if request.page_size > 1000:
                raise ValueError("page size must not exceed 1000")
            limit = request.page_size or 100
            if not set(request.states) <= SUBMISSION_STATES.keys():
                raise ValueError("unknown command state")
            states = {SUBMISSION_STATES[state] for state in request.states}
            after = read_page_token(request.page_token) if request.page_token else None
            page = await self.resources.submissions.list(session_id, states=states, after=after, limit=limit)
            result = sandbox_service_pb2.ListCommandsResponse(commands=[command_status(row) for row in page])
            if len(page) == limit:
                last = page[-1]
                result.next_page_token = write_page_token(last.accepted_at, last.command.command_id)
            return result

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

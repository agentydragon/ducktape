"""Authenticated gRPC boundary for Sessions and commands. Runners remain the command and execution-event
authority."""

import asyncio
import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import override
from uuid import UUID

import grpc
from google.protobuf.empty_pb2 import Empty
from google.protobuf.json_format import ParseError
from google.protobuf.message_factory import GetMessageClass
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.exc import SQLAlchemyError

from agentplane.grpc_options import grpc_channel_option_kvps
from agentplane.proto import command_pb2, event_log_pb2, runner_control_pb2 as control_pb2, runner_pb2
from agentplane.runner.client import RunnerClient
from agentplane.runner.errors import OpenTimeoutError, RunnerError, SessionSealedError, StreamClosedError

# Aliased: `protocol_pb2` is the Sandbox Service's messages.
from agentplane.runner_gateway import protocol_pb2 as gateway_pb2, protocol_pb2_grpc, session_lifecycle
from agentplane.runner_gateway.command_delivery import CommandDelivery, CommandRefusedError
from agentplane.runner_gateway.command_relay import admit_running_command
from agentplane.runner_gateway.commands.store import (
    CommandSubmission,
    Submission,
    SubmissionConflictError,
    SubmissionNotFoundError,
    SubmissionState,
    SubmissionStore,
    submit_command,
)
from agentplane.runner_gateway.lifecycle_control import ConnectionNotHeldError, ControlRefusedError, LifecycleControl
from agentplane.runner_gateway.runner_control import ControlOperation
from agentplane.runner_gateway.sealing import SEAL_S, Sealing
from agentplane.runner_gateway.session_changes import SessionChanges
from agentplane.runner_gateway.session_registry import SessionNotFoundError, SessionRegistry
from agentplane.sandbox_service import protocol_pb2, wire
from agentplane.sandbox_service.destinations import DestinationResolver, DestinationUnavailableError, RunnerEndpoint
from agentplane.sandbox_service.models import InventoryError, SandboxNotFoundError
from agentplane.sandbox_service.protocol_pb2 import SandboxDestination
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.grpc_caller import authenticate
from agentplane.workload_auth.principal import WorkloadPrincipalRejectedError, WorkloadPrincipalResolver

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio

WATCH_PAGE = 256


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
    session_changes: SessionChanges
    # The Sandbox Service's account, which alone may seal a Sandbox for teardown; none where the
    # Sandbox Service seals in-process.
    sealer_accounts: frozenset[ServiceAccountRef] = frozenset()
    admission_timeout_s: float = 15
    follow_lease_s: float = 900
    lifecycle_timeout_s: float = 300
    runner_grpc_channel_options: dict[str, int | str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            min(
                self.admission_timeout_s,
                self.runner_admission_ack_timeout_s,
                self.follow_lease_s,
                self.lifecycle_timeout_s,
            )
            <= 0
        ):
            raise ValueError("timeouts must be positive")
        if not self.caller_accounts:
            raise ValueError("at least one service caller is required")

    async def authenticate(self, context: grpc.aio.ServicerContext) -> ServiceAccountRef:
        return await authenticate(self.principals, self.caller_accounts, context)

    @property
    def sealing(self) -> Sealing:
        return Sealing(
            self.destinations,
            self.sessions,
            connections=self.delivery.connections,
            control=self.control,
            runner_grpc_channel_options=self.runner_grpc_channel_options,
        )


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
    except TimeoutError, OpenTimeoutError:
        await context.abort(
            grpc.StatusCode.DEADLINE_EXCEEDED, "service deadline expired; mutation outcome may be uncertain"
        )
    except InventoryError, RunnerError, StreamClosedError:
        await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "runner or sandbox state refused the request")
    except DestinationUnavailableError, ConnectionError, k8s_client.ApiException, SQLAlchemyError:
        await context.abort(grpc.StatusCode.UNAVAILABLE, "destination unavailable; no offline admission")
    except grpc.RpcError as error:
        if isinstance(error, grpc.aio.AioRpcError):
            if error.code() == grpc.StatusCode.INVALID_ARGUMENT:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "runner rejected invalid request")
            if error.code() in (grpc.StatusCode.FAILED_PRECONDITION, grpc.StatusCode.NOT_FOUND):
                await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "runner rejected session or bootstrap state")
        await context.abort(grpc.StatusCode.UNAVAILABLE, "runner unavailable; mutation outcome may be uncertain")


def public_session_id(session_id: str) -> UUID | None:
    """The service-owned Session ID, or None for a legacy caller-chosen runner ID."""
    try:
        parsed = UUID(session_id)
    except ValueError:
        return None
    return parsed if str(parsed) == session_id else None


SUBMISSION_STATES = {
    protocol_pb2.COMMAND_STATE_PENDING_ADMISSION: SubmissionState.PENDING_ADMISSION,
    protocol_pb2.COMMAND_STATE_ADMITTED: SubmissionState.ADMITTED,
    protocol_pb2.COMMAND_STATE_REJECTED: SubmissionState.REJECTED,
}
COMMAND_STATES = {value: key for key, value in SUBMISSION_STATES.items()}


def command_status(submission: Submission) -> protocol_pb2.CommandStatus:
    result = protocol_pb2.CommandStatus(command=submission.command, state=COMMAND_STATES[submission.state])
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


class RunnerGateway(protocol_pb2_grpc.RunnerGatewayServicer):
    def __init__(self, resources: Resources) -> None:
        self.resources = resources

    def _runner_client(self, target: str) -> RunnerClient:
        channel = grpc.aio.insecure_channel(
            target, options=grpc_channel_option_kvps(self.resources.runner_grpc_channel_options)
        )
        return RunnerClient(channel)

    @asynccontextmanager
    async def request(
        self, context: grpc.aio.ServicerContext, *, timeout_s: float | None = None
    ) -> AsyncIterator[None]:
        async with errors(context), asyncio.timeout(timeout_s or self.resources.admission_timeout_s):
            await self.resources.authenticate(context)
            yield

    @asynccontextmanager
    async def runner(
        self, destination: SandboxDestination, *, writes: bool
    ) -> AsyncIterator[tuple[RunnerClient, RunnerEndpoint]]:
        endpoint = await self.resources.destinations.resolve(destination, writes=writes)
        # TODO: runner RPC authentication/TLS. V1 relies on the deployment network boundary.
        client = self._runner_client(endpoint.target)
        try:
            yield client, endpoint
        finally:
            await client.close()

    async def lifecycle_routed(self, destination: SandboxDestination) -> bool:
        """Whether the Sandbox's lifecycle requests travel over its runner control stream, which they do for
        good once a connection agreed `lifecycle`. Only the replica holding that connection serves
        them; any other refuses before acting, and the caller retries through the control Service."""
        sandbox_uid = UUID(destination.sandbox_uid)
        if ControlOperation.LIFECYCLE not in await self.resources.delivery.connections.read_operations(sandbox_uid):
            return False
        if self.resources.control.holder(sandbox_uid) is None:
            raise ConnectionNotHeldError("another replica holds this Sandbox's runner connection")
        return True

    async def list_runner_sessions(self, destination: SandboxDestination) -> list[runner_pb2.SessionSummary]:
        if await self.lifecycle_routed(destination):
            # Verifies the incarnation and its owner, as the inbound path's runner lookup does.
            await self.resources.destinations.resolve(destination, writes=False)
            listing = control_pb2.ControlRequest(list_sessions=runner_pb2.ListSessionsRequest())
            result = await self.resources.control.request(UUID(destination.sandbox_uid), listing)
            return list(result.list_sessions.sessions)
        async with self.runner(destination, writes=False) as (client, _):
            return await client.list_sessions()

    @override
    async def ListSessions(
        self, request: protocol_pb2.SandboxRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.ListSessionsResponse:
        async with self.request(context):
            sessions = await self.list_runner_sessions(request.destination)
            for summary in sessions:
                public_id = await self.resources.sessions.session_id(
                    sandbox_namespace=self.resources.destinations.inventory.namespace,
                    sandbox_name=request.destination.sandbox,
                    sandbox_uid=UUID(request.destination.sandbox_uid),
                    runner_session_id=summary.session_id,
                )
                if public_id is not None:
                    summary.session_id = str(public_id)
            return runner_pb2.ListSessionsResponse(sessions=sessions)

    async def runner_session_id(self, destination: protocol_pb2.SessionDestination) -> str:
        """Keep physical runner identifiers behind this service, including on follow."""
        if (session_id := public_session_id(destination.session_id)) is None:
            return destination.session_id  # legacy caller-chosen runner ID
        return await self.resources.sessions.runner_id(
            session_id,
            sandbox_namespace=self.resources.destinations.inventory.namespace,
            sandbox_name=destination.sandbox.sandbox,
            sandbox_uid=UUID(destination.sandbox.sandbox_uid),
        )

    @override
    async def CreateSession(
        self, request: protocol_pb2.CreateSessionRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.CreateSessionResponse:
        async with errors(context), asyncio.timeout(self.resources.lifecycle_timeout_s):
            caller = await self.resources.authenticate(context)
            if len(request.idempotency_key) > 128 or len(request.setup_script) > 65_536:
                raise ValueError("Open key or setup script is too long")
            # CLEANUP(added 2026-10-11): Require session_id, and stop minting here, once the deployed
            #   app sends it on every CreateSession (agentplane/sandbox_service/client.py's create).
            client_id = public_session_id(request.session_id) if request.session_id else None
            if request.session_id and client_id is None:
                raise ValueError("session ID must be a UUID")
            overrides = wire.launch_overrides(request)
            endpoint = await self.resources.destinations.resolve(request.sandbox, writes=True)
            control_routed = await self.lifecycle_routed(request.sandbox)
            sandbox_uid = UUID(request.sandbox.sandbox_uid)

            def freeze(session_id: UUID) -> bytes:
                destination = protocol_pb2.SessionDestination(sandbox=request.sandbox, session_id=str(session_id))
                # Resolve workspace templates against the reserved public ID, and freeze the result.
                effective_overrides = dict(overrides)
                if isinstance(cwd := effective_overrides.get("cwd"), str):
                    effective_overrides["cwd"] = cwd.replace("{session_id}", str(session_id))
                spec = session_lifecycle.launch_spec(
                    destination,
                    effective_overrides,
                    binding=endpoint.binding,
                    platform_instructions=self.resources.platform_instructions,
                    sandbox_namespace=self.resources.destinations.inventory.namespace,
                )
                setup_script = (
                    request.setup_script
                    if request.HasField("setup_script")
                    else endpoint.binding.session_defaults.setup_script
                    if endpoint.binding is not None and endpoint.binding.session_defaults.HasField("setup_script")
                    else None
                )
                launch = protocol_pb2.FrozenLaunch(
                    open=runner_pb2.Open(session_id=str(session_id) if client_id else f"r-{session_id}", spec=spec),
                    bootstrap=endpoint.binding.bootstrap if endpoint.binding is not None else "",
                )
                if setup_script is not None:
                    launch.open.setup_script = setup_script
                return launch.SerializeToString(deterministic=True)

            reservation = await self.resources.sessions.reserve(
                session_id=client_id,
                caller_namespace=caller.namespace,
                caller_name=caller.name,
                sandbox_namespace=self.resources.destinations.inventory.namespace,
                sandbox_name=request.sandbox.sandbox,
                sandbox_uid=sandbox_uid,
                open_key=request.idempotency_key or None,
                open_request=request.SerializeToString(deterministic=True),
                launch_spec=freeze,
            )
            launch = protocol_pb2.FrozenLaunch.FromString(reservation.launch_spec)
            public_id = str(reservation.session_id)
            setup_script = launch.open.setup_script if launch.open.HasField("setup_script") else None
            if control_routed:
                attachment = await session_lifecycle.open_control_session(
                    self.resources.control,
                    sandbox_uid,
                    launch.open.session_id,
                    launch.open.spec,
                    bootstrap=launch.bootstrap,
                    setup_script=setup_script,
                )
            else:
                client = self._runner_client(endpoint.target)
                try:
                    attachment = await session_lifecycle.open_session(
                        client,
                        launch.open.session_id,
                        launch.open.spec,
                        bootstrap=launch.bootstrap,
                        setup_script=setup_script,
                    )
                finally:
                    await client.close()
            attachment.session_id = public_id
            return protocol_pb2.CreateSessionResponse(session_id=public_id, attached=attachment)

    @override
    async def LookupSession(
        self, request: protocol_pb2.LookupSessionRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.LookupSessionResponse:
        async with errors(context), asyncio.timeout(self.resources.admission_timeout_s):
            caller = await self.resources.authenticate(context)
            if not request.idempotency_key or len(request.idempotency_key) > 128:
                raise ValueError("Open key is required and must not exceed 128 characters")
            await self.resources.destinations.resolve(request.sandbox, writes=False)
            public_id = await self.resources.sessions.lookup_open(
                caller_namespace=caller.namespace,
                caller_name=caller.name,
                sandbox_namespace=self.resources.destinations.inventory.namespace,
                sandbox_name=request.sandbox.sandbox,
                sandbox_uid=UUID(request.sandbox.sandbox_uid),
                open_key=request.idempotency_key,
            )
            if public_id is None:
                return protocol_pb2.LookupSessionResponse()
            result = protocol_pb2.LookupSessionResponse(session_id=str(public_id))
            runner_session_id = await self.resources.sessions.runner_id(
                public_id,
                sandbox_namespace=self.resources.destinations.inventory.namespace,
                sandbox_name=request.sandbox.sandbox,
                sandbox_uid=UUID(request.sandbox.sandbox_uid),
            )
            # DB reservation precedes runner Open. Inventory includes sessions whose
            # native handshake failed: a retained spec is not proof of a successful Open.
            async with self.runner(request.sandbox, writes=False) as (client, _):
                for summary in await client.list_sessions():
                    if summary.session_id != runner_session_id:
                        continue
                    if summary.setup_state in (runner_pb2.SETUP_STATE_FAILED, runner_pb2.SETUP_STATE_INTERRUPTED):
                        result.failed = True
                    elif summary.harness_state == runner_pb2.HARNESS_STATE_RUNNING:
                        result.summary.CopyFrom(summary)
                    elif summary.last_cursor:
                        # Observe the retained journal without a spec: this cannot restart
                        # the harness or replay bootstrap. A stopped session may have
                        # successfully run before stopping; an early exit may not.
                        attachment = await client.attach(summary.session_id)
                        try:
                            while True:
                                entry = await attachment.next_entry()
                                kind = entry.event.WhichOneof("observation")
                                if kind == "harness_started":
                                    result.summary.CopyFrom(summary)
                                    break
                                if kind in ("harness_exited", "harness_launch_failed", "setup_interrupted") or (
                                    kind == "setup_finished" and entry.event.setup_finished.exit_code != 0
                                ):
                                    result.failed = True
                                    break
                                if entry.cursor >= summary.last_cursor:
                                    break
                        finally:
                            attachment.cancel()
                    if result.HasField("summary"):
                        result.summary.session_id = str(public_id)
                    break
            return result

    @override
    async def ResumeSession(
        self, request: protocol_pb2.SessionRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.Attached:
        async with self.request(context, timeout_s=self.resources.lifecycle_timeout_s):
            destination = request.destination
            if not destination.session_id:
                raise ValueError("session ID is required")
            if await self.lifecycle_routed(destination.sandbox):
                # Verifies the incarnation, its owner and that it is not being torn down.
                await self.resources.destinations.resolve(destination.sandbox, writes=True)
                result = await session_lifecycle.resume_control_session(
                    self.resources.control,
                    UUID(destination.sandbox.sandbox_uid),
                    await self.runner_session_id(destination),
                )
            else:
                async with self.runner(destination.sandbox, writes=True) as (client, _):
                    result = await session_lifecycle.resume_session(client, await self.runner_session_id(destination))
            result.session_id = destination.session_id
            return result

    @override
    async def SubmitCommand(
        self, request: protocol_pb2.SubmitCommandRequest, context: grpc.aio.ServicerContext
    ) -> event_log_pb2.EventEntry:
        async with errors(context), asyncio.timeout(self.resources.runner_admission_ack_timeout_s):
            caller = await self.resources.authenticate(context)
            destination = request.destination
            if not destination.session_id:
                raise ValueError("session ID is required")
            submission = CommandSubmission(command=request.command)
            # Resolving the public ID binds the Session to this Sandbox incarnation before anything
            # is persisted for it.
            runner_session_id = await self.runner_session_id(destination)
            public_id = public_session_id(destination.session_id)
            # Once a runner of this Sandbox has agreed commands over the control stream, they never fall
            # back to the relay.
            control_routed = ControlOperation.COMMANDS in (
                await self.resources.delivery.connections.read_operations(UUID(destination.sandbox.sandbox_uid))
            )
            if public_id is None:
                if control_routed:
                    raise DestinationUnavailableError("a control-routed Sandbox takes public Session IDs only")
                # CLEANUP(added 2026-10-10): Remove the relay-only path once no Session opened with a
                # caller-chosen runner ID remains on staging.
                async with self.runner(destination.sandbox, writes=True) as (client, _):
                    return await admit_running_command(
                        client,
                        runner_session_id,
                        submission.copy_command(),
                        after_cursor=request.follow.after_cursor,
                        timeout_s=self.resources.runner_admission_ack_timeout_s,
                    )
            submissions = self.resources.submissions

            if control_routed:
                delivery = self.resources.delivery
                # Verifies the incarnation and its owner, as the relay's runner lookup does.
                await self.resources.destinations.resolve(destination.sandbox, writes=True)

                async def dispatch(command: command_pb2.Command) -> event_log_pb2.EventEntry:
                    return await delivery.deliver(
                        sandbox_uid=UUID(destination.sandbox.sandbox_uid),
                        session_id=public_id,
                        command_id=command.command_id,
                        runner_session_id=runner_session_id,
                        deadline_s=min(self.resources.runner_admission_ack_timeout_s, 60),
                    )

            else:

                async def dispatch(command: command_pb2.Command) -> event_log_pb2.EventEntry:
                    # The caller picks a follow cursor before any earlier admission of this command,
                    # so a retry finds the original receipt in the replay.
                    async with self.runner(destination.sandbox, writes=True) as (client, _):
                        return await admit_running_command(
                            client,
                            runner_session_id,
                            command,
                            after_cursor=request.follow.after_cursor,
                            timeout_s=self.resources.runner_admission_ack_timeout_s,
                        )

            return await submit_command(submissions, public_id, submission, caller=caller, dispatch=dispatch)

    async def checked_public_session(self, destination: protocol_pb2.SessionDestination) -> UUID:
        """The public Session ID of this Sandbox incarnation. Its submissions outlive the Sandbox, so
        the owner is checked only while the incarnation exists."""
        sandbox = destination.sandbox
        if not all((sandbox.sandbox, sandbox.sandbox_uid, sandbox.owner.namespace, sandbox.owner.name)):
            raise ValueError("Sandbox name, UID, and owner are required")
        if (session_id := public_session_id(destination.session_id)) is None:
            raise ValueError("a public Session ID is required")
        await self.runner_session_id(destination)
        try:
            view = await self.resources.destinations.inventory.get(sandbox.sandbox)
        except SandboxNotFoundError:
            return session_id
        if view.uid == sandbox.sandbox_uid and view.service_account != sandbox.owner:
            raise SandboxNotFoundError(sandbox.sandbox)
        return session_id

    @override
    async def GetCommand(
        self, request: protocol_pb2.GetCommandRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.CommandStatus:
        async with self.request(context):
            session_id = await self.checked_public_session(request.destination)
            if not 1 <= len(request.command_id) <= 128:
                raise ValueError("command ID must contain 1 to 128 characters")
            return command_status(await self.resources.submissions.read(session_id, request.command_id))

    @override
    async def ListCommands(
        self, request: protocol_pb2.ListCommandsRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.ListCommandsResponse:
        async with self.request(context):
            session_id = await self.checked_public_session(request.destination)
            if request.page_size > 1000:
                raise ValueError("page size must not exceed 1000")
            limit = request.page_size or 100
            if not set(request.states) <= SUBMISSION_STATES.keys():
                raise ValueError("unknown command state")
            states = {SUBMISSION_STATES[state] for state in request.states}
            after = read_page_token(request.page_token) if request.page_token else None
            page = await self.resources.submissions.list(session_id, states=states, after=after, limit=limit)
            result = protocol_pb2.ListCommandsResponse(commands=[command_status(row) for row in page])
            if len(page) == limit:
                last = page[-1]
                result.next_page_token = write_page_token(last.accepted_at, last.command.command_id)
            return result

    # mypy-protobuf omits aio's supported writer-style streaming handlers. Explicit writes are
    # intentional: flow-control stalls must stay inside the deadline and cleanup scope.
    @override
    async def FollowSession(  # type: ignore[override]
        self, request: protocol_pb2.FollowSessionRequest, context: grpc.aio.ServicerContext
    ) -> None:
        # Explicit writes keep flow-control stalls inside our deadline and finally blocks.
        async with errors(context):
            async with asyncio.timeout(self.resources.admission_timeout_s):
                await self.resources.authenticate(context)
                destination = request.destination
                if not destination.session_id:
                    raise ValueError("session ID is required")
                endpoint = await self.resources.destinations.resolve(destination.sandbox, writes=False)
            client = self._runner_client(endpoint.target)
            try:
                async with asyncio.timeout(self.resources.admission_timeout_s):
                    attachment = await client.attach(
                        await self.runner_session_id(destination), after_cursor=request.follow.after_cursor
                    )
                try:
                    deadline = asyncio.get_running_loop().time() + self.resources.follow_lease_s
                    async with asyncio.timeout(self.resources.admission_timeout_s):
                        attached = runner_pb2.Attached()
                        attached.CopyFrom(attachment.attached)
                        attached.session_id = destination.session_id
                        await context.write(protocol_pb2.FollowSessionResponse(attached=attached))
                    while True:
                        # Idle runners may stay quiet for the whole lease. Only writes have
                        # the short timeout: a blocked consumer must not pin an attachment.
                        lease = asyncio.timeout_at(deadline)
                        try:
                            async with lease:
                                entry = await attachment.next_entry()
                        except TimeoutError:
                            if not lease.expired():
                                raise
                            terminal = protocol_pb2.FollowSessionResponse(reconnect_required=Empty())
                            break
                        except SessionSealedError as sealed:
                            terminal = protocol_pb2.FollowSessionResponse(
                                sealed=protocol_pb2.Sealed(cursor=sealed.cursor)
                            )
                            break
                        except StreamClosedError:
                            terminal = protocol_pb2.FollowSessionResponse(ended=Empty())
                            break
                        async with asyncio.timeout(self.resources.admission_timeout_s):
                            await context.write(protocol_pb2.FollowSessionResponse(entry=entry))
                    async with asyncio.timeout(self.resources.admission_timeout_s):
                        await context.write(terminal)
                finally:
                    attachment.cancel()
            finally:
                await client.close()

    @override
    async def WatchSessions(  # type: ignore[override]
        self, request: protocol_pb2.WatchSessionsRequest, context: grpc.aio.ServicerContext
    ) -> None:
        async with errors(context):
            async with asyncio.timeout(self.resources.admission_timeout_s):
                # Any allowed service caller may watch; the feed carries no transcript.
                await self.resources.authenticate(context)
                sessions, changes = self.resources.sessions, self.resources.session_changes
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.resources.follow_lease_s
            position = request.after_position
            with changes.subscribe() as changed:
                while loop.time() < deadline:
                    changed.clear()
                    async with asyncio.timeout(self.resources.admission_timeout_s):
                        page = await sessions.read_changes(after_position=position, limit=WATCH_PAGE)
                        sandboxes = await self.resources.destinations.inventory.list_sandboxes() if page else []
                    owners = {(sandbox.name, sandbox.uid): sandbox.service_account for sandbox in sandboxes}
                    for change in page:
                        if (owner := owners.get((change.sandbox, change.sandbox_uid))) is not None:
                            change.owner.CopyFrom(owner)
                        async with asyncio.timeout(self.resources.admission_timeout_s):
                            await context.write(protocol_pb2.WatchSessionsResponse(change=change))
                        position = change.position
                    if len(page) == WATCH_PAGE:
                        continue
                    lease = asyncio.timeout_at(deadline)
                    try:
                        async with lease:
                            await changed.wait()
                    except TimeoutError:
                        if not lease.expired():
                            raise
            async with asyncio.timeout(self.resources.admission_timeout_s):
                await context.write(protocol_pb2.WatchSessionsResponse(reconnect_required=Empty()))

    @override
    async def SealSandbox(
        self, request: protocol_pb2.SandboxRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.SealResponse:
        async with errors(context), asyncio.timeout(SEAL_S + self.resources.admission_timeout_s):
            await authenticate(self.resources.principals, self.resources.sealer_accounts, context)
            destination = request.destination
            if not all(
                (destination.sandbox, destination.sandbox_uid, destination.owner.namespace, destination.owner.name)
            ):
                raise ValueError("Sandbox name, UID, and owner are required")
            view = await self.resources.destinations.inventory.get(destination.sandbox)
            if view.uid != destination.sandbox_uid or view.service_account != destination.owner:
                raise SandboxNotFoundError(destination.sandbox)
            return await self.resources.sealing.seal(view)


def add_service(resources: Resources, server: grpc.aio.Server) -> None:
    protocol_pb2_grpc.add_RunnerGatewayServicer_to_server(RunnerGateway(resources), server)


# CLEANUP(added 2026-10-11): Delete once the manifests run the Runner Gateway Deployment and point
#   every client at it (SANDBOX_SERVICE_DEPLOYMENT_SPLIT's manifest PR).
LEGACY_SERVICE = "ducktape.agentplane.sandbox.v1.SandboxService"


def add_legacy_service(resources: Resources, server: grpc.aio.Server) -> None:
    """Also answer the gateway's RPCs under the Sandbox Service's name, where clients still running
    an image from before the split call them on the Sandbox Service's one Deployment."""
    gateway = RunnerGateway(resources)
    handlers = {}
    for method in gateway_pb2.DESCRIPTOR.services_by_name["RunnerGateway"].methods:
        handler = (
            grpc.unary_stream_rpc_method_handler if method.server_streaming else grpc.unary_unary_rpc_method_handler
        )
        handlers[method.name] = handler(
            # The servicer method of the descriptor's name, as the generated registration binds it.
            getattr(gateway, method.name),
            request_deserializer=GetMessageClass(method.input_type).FromString,
            response_serializer=GetMessageClass(method.output_type).SerializeToString,
        )
    server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler(LEGACY_SERVICE, handlers),))

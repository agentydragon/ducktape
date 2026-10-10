"""Authenticated gRPC boundary. Runners remain the command and execution-event authority."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import override
from uuid import UUID

import grpc
from google.protobuf.empty_pb2 import Empty
from google.protobuf.json_format import ParseError
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.exc import SQLAlchemyError

from agentplane.grpc_options import grpc_channel_option_kvps
from agentplane.protocol import event_log_pb2
from agentplane.runner import protocol_pb2 as runner_pb2
from agentplane.runner.client import RunnerClient
from agentplane.runner.errors import OpenTimeoutError, RunnerError, StreamClosedError
from agentplane.sandbox_service import protocol_pb2, protocol_pb2_grpc, session_lifecycle, wire
from agentplane.sandbox_service.action_policy_views import UnknownPolicySetError
from agentplane.sandbox_service.command_relay import admit_running_command
from agentplane.sandbox_service.destinations import DestinationResolver, DestinationUnavailableError, RunnerEndpoint
from agentplane.sandbox_service.egress_views import BindingNotFoundError, UnknownPolicyError
from agentplane.sandbox_service.models import (
    HoldNotFoundError,
    InventoryError,
    RetentionHeldError,
    SandboxConflictError,
    SandboxNotFoundError,
)
from agentplane.sandbox_service.protocol_pb2 import Sandbox, SandboxDestination
from agentplane.sandbox_service.provisioning import Provisioning
from agentplane.sandbox_service.retention_holds import MAX_HOLD_KEY_LENGTH
from agentplane.sandbox_service.session_history.session_changes import SessionChanges
from agentplane.sandbox_service.session_history.store import HistoryNotFoundError, Store
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.bearer import parse_bearer, sole_header
from agentplane.workload_auth.principal import WorkloadPrincipalRejectedError, WorkloadPrincipalResolver

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio

WATCH_PAGE = 256


@dataclass(frozen=True)
class Resources:
    principals: WorkloadPrincipalResolver
    destinations: DestinationResolver
    provisioning: Provisioning
    caller_accounts: frozenset[ServiceAccountRef]
    platform_instructions: str
    runner_admission_ack_timeout_s: float
    history: Store | None = None
    session_changes: SessionChanges | None = None
    history_reader_accounts: frozenset[ServiceAccountRef] = frozenset()
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
        if not self.history_reader_accounts <= self.caller_accounts:
            raise ValueError("history readers must be allowed service callers")

    async def authenticate(self, context: grpc.aio.ServicerContext) -> ServiceAccountRef:
        values = [value for key, value in (context.invocation_metadata() or ()) if key == "authorization"]
        if any(not isinstance(value, str) for value in values):
            raise WorkloadPrincipalRejectedError("invalid bearer metadata")
        value = sole_header([value for value in values if isinstance(value, str)])
        token = parse_bearer(value) if value is not None else None
        if token is None:
            raise WorkloadPrincipalRejectedError("invalid bearer metadata")
        principal = await self.principals.resolve_workload(token)
        if principal.account not in self.caller_accounts:
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "service caller not allowed")
        return principal.account

    async def authenticate_history_reader(self, context: grpc.aio.ServicerContext) -> None:
        # A public Session UUID is not itself authority. Notifications and future
        # agent callers admitted for lifecycle may not read transcripts.
        if await self.authenticate(context) not in self.history_reader_accounts:
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "session history reader not allowed")


@asynccontextmanager
async def errors(context: grpc.aio.ServicerContext) -> AsyncIterator[None]:
    """Sanitize backend failures; no Kubernetes wire responses or bearer data reach clients."""
    try:
        yield
    except WorkloadPrincipalRejectedError:
        await context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid workload bearer")
    except SandboxNotFoundError, BindingNotFoundError, HistoryNotFoundError:
        await context.abort(grpc.StatusCode.NOT_FOUND, "sandbox incarnation not found")
    except HoldNotFoundError:
        await context.abort(grpc.StatusCode.NOT_FOUND, "retention hold not found")
    except RetentionHeldError:
        await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "retention holds block deletion; ListHolds names them")
    except SandboxConflictError:
        await context.abort(grpc.StatusCode.ALREADY_EXISTS, "sandbox name or initialization conflicts")
    except ValueError, ParseError, UnknownPolicyError, UnknownPolicySetError:
        await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid service request or grant selection")
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


class SandboxService(protocol_pb2_grpc.SandboxServiceServicer):
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
    async def runner(self, destination: SandboxDestination) -> AsyncIterator[tuple[RunnerClient, RunnerEndpoint]]:
        endpoint = await self.resources.destinations.resolve(destination)
        # TODO: runner RPC authentication/TLS. V1 relies on the deployment network boundary.
        client = self._runner_client(endpoint.target)
        try:
            yield client, endpoint
        finally:
            await client.close()

    @override
    async def ReadSessionEvents(
        self, request: protocol_pb2.ReadSessionEventsRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.ReadSessionEventsResponse:
        async with errors(context), asyncio.timeout(self.resources.admission_timeout_s):
            await self.resources.authenticate_history_reader(context)
            if self.resources.history is None:
                raise ConnectionError("history unavailable")
            if not 1 <= request.limit <= 1000:
                raise ValueError("invalid history page size")
            session_id = UUID(request.session_id)
            return await self.resources.history.read_page(
                session_id, after_cursor=request.after_cursor, limit=request.limit
            )

    @override
    async def ReadSessionObservations(
        self, request: protocol_pb2.ReadSessionObservationsRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.ReadSessionObservationsResponse:
        async with errors(context), asyncio.timeout(self.resources.admission_timeout_s):
            await self.resources.authenticate_history_reader(context)
            if self.resources.history is None:
                raise ConnectionError("history unavailable")
            last_cursor, observations = await self.resources.history.read_observations(
                UUID(request.session_id),
                before_cursor=request.before_cursor if request.HasField("before_cursor") else None,
                after_cursor=request.after_cursor if request.HasField("after_cursor") else None,
                limit=request.limit,
            )
            return protocol_pb2.ReadSessionObservationsResponse(
                last_cursor=last_cursor,
                observations=[
                    protocol_pb2.SessionObservation(cursor=cursor, kind=kind) for cursor, kind in observations
                ],
            )

    @override
    async def ListSandboxes(
        self, request: Empty, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.ListSandboxesResponse:
        async with self.request(context):
            inventory = self.resources.provisioning.inventory
            return protocol_pb2.ListSandboxesResponse(sandboxes=await inventory.list_sandboxes())

    @override
    async def GetSandbox(
        self, request: protocol_pb2.GetSandboxRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.Sandbox:
        async with self.request(context):
            inventory = self.resources.provisioning.inventory
            if not request.name:
                raise ValueError("name is required")
            return await inventory.get(request.name)

    @override
    async def CreateSandbox(
        self, request: protocol_pb2.CreateSandboxRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.Sandbox:
        async with errors(context), asyncio.timeout(self.resources.lifecycle_timeout_s):
            principal = await self.resources.authenticate(context)
            provisioning = self.resources.provisioning
            return await provisioning.create(request, caller=principal)

    async def checked_sandbox(self, request: protocol_pb2.SandboxRequest) -> tuple[Provisioning, Sandbox]:
        provisioning = self.resources.provisioning
        destination = request.destination
        if not all((destination.sandbox, destination.sandbox_uid, destination.owner.namespace, destination.owner.name)):
            raise ValueError("Sandbox name, UID, and owner are required")
        view = await provisioning.inventory.get(destination.sandbox)
        if view.uid != destination.sandbox_uid or view.service_account != destination.owner:
            raise SandboxNotFoundError(destination.sandbox)
        return provisioning, view

    @override
    async def SuspendSandbox(self, request: protocol_pb2.SandboxRequest, context: grpc.aio.ServicerContext) -> Empty:
        async with self.request(context):
            provisioning, view = await self.checked_sandbox(request)
            await provisioning.inventory.suspend(view.name, uid=view.uid)
            return Empty()

    @override
    async def ResumeSandbox(self, request: protocol_pb2.SandboxRequest, context: grpc.aio.ServicerContext) -> Empty:
        async with self.request(context):
            provisioning, view = await self.checked_sandbox(request)
            if await provisioning.inventory.pending_grants(view.name) is not None:
                raise InventoryError("Sandbox provisioning is incomplete")
            await provisioning.inventory.resume(view.name, uid=view.uid)
            return Empty()

    @override
    async def DeleteSandbox(self, request: protocol_pb2.SandboxRequest, context: grpc.aio.ServicerContext) -> Empty:
        async with self.request(context):
            provisioning, view = await self.checked_sandbox(request)
            await provisioning.inventory.delete(view.name, uid=view.uid)
            return Empty()

    async def checked_hold_sandbox(
        self, request: protocol_pb2.HoldRequest | protocol_pb2.ConfirmHoldRequest
    ) -> Sandbox:
        if not all(
            1 <= len(value) <= MAX_HOLD_KEY_LENGTH for value in (request.destination.session_id, request.holder)
        ):
            raise ValueError("Session ID and holder are required and must not exceed 128 characters")
        _, view = await self.checked_sandbox(protocol_pb2.SandboxRequest(destination=request.destination.sandbox))
        return view

    @override
    async def PlaceHold(
        self, request: protocol_pb2.HoldRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.Hold:
        async with self.request(context):
            view = await self.checked_hold_sandbox(request)
            return await self.resources.provisioning.inventory.place_hold(
                view.name, uid=view.uid, session_id=request.destination.session_id, holder=request.holder
            )

    @override
    async def ConfirmHold(
        self, request: protocol_pb2.ConfirmHoldRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.Hold:
        async with self.request(context):
            view = await self.checked_hold_sandbox(request)
            return await self.resources.provisioning.inventory.confirm_hold(
                view.name,
                uid=view.uid,
                session_id=request.destination.session_id,
                holder=request.holder,
                through_cursor=request.through_cursor,
            )

    @override
    async def ReleaseHold(self, request: protocol_pb2.HoldRequest, context: grpc.aio.ServicerContext) -> Empty:
        async with self.request(context):
            view = await self.checked_hold_sandbox(request)
            await self.resources.provisioning.inventory.release_hold(
                view.name, uid=view.uid, session_id=request.destination.session_id, holder=request.holder
            )
            return Empty()

    @override
    async def ListHolds(
        self, request: protocol_pb2.SandboxRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.ListHoldsResponse:
        async with self.request(context):
            provisioning, view = await self.checked_sandbox(request)
            return protocol_pb2.ListHoldsResponse(
                holds=await provisioning.inventory.list_holds(view.name, uid=view.uid)
            )

    @override
    async def ListTemplates(
        self, request: Empty, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.ListTemplatesResponse:
        async with self.request(context):
            inventory = self.resources.provisioning.inventory
            return protocol_pb2.ListTemplatesResponse(templates=await inventory.list_templates())

    @override
    async def GrantEgress(
        self, request: protocol_pb2.GrantEgressRequest, context: grpc.aio.ServicerContext
    ) -> protocol_pb2.GrantEgressResponse:
        async with self.request(context):
            provisioning, view = await self.checked_sandbox(
                protocol_pb2.SandboxRequest(destination=request.destination)
            )
            if not request.egress_policies:
                raise ValueError("at least one egress policy is required")
            binding = await provisioning.egress.grant(view, list(request.egress_policies))
            return protocol_pb2.GrantEgressResponse(binding_name=binding.name)

    @override
    async def RevokeEgress(self, request: protocol_pb2.RevokeEgressRequest, context: grpc.aio.ServicerContext) -> Empty:
        async with self.request(context):
            provisioning = self.resources.provisioning
            if not request.binding_name:
                raise ValueError("binding name is required")
            await provisioning.egress.revoke(request.binding_name)
            return Empty()

    @override
    async def ListSessions(
        self, request: protocol_pb2.SandboxRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.ListSessionsResponse:
        async with self.request(context), self.runner(request.destination) as (client, _):
            sessions = await client.list_sessions()
            if self.resources.history is not None:
                for summary in sessions:
                    public_id = await self.resources.history.session_id(
                        sandbox_namespace=self.resources.destinations.inventory.namespace,
                        sandbox_name=request.destination.sandbox,
                        sandbox_uid=UUID(request.destination.sandbox_uid),
                        runner_session_id=summary.session_id,
                    )
                    if public_id is not None:
                        summary.session_id = str(public_id)
            return runner_pb2.ListSessionsResponse(sessions=sessions)

    @override
    async def OpenSession(
        self, request: protocol_pb2.OpenSessionRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.Attached:
        async with self.request(context, timeout_s=self.resources.lifecycle_timeout_s):
            destination = request.destination
            if not destination.session_id:
                raise ValueError("session ID is required")
            async with self.runner(destination.sandbox) as (client, endpoint):
                if len(request.setup_script) > 65_536:
                    raise ValueError("setup script is too long")
                spec = session_lifecycle.launch_spec(
                    destination,
                    wire.launch_overrides(request),
                    binding=endpoint.binding,
                    platform_instructions=self.resources.platform_instructions,
                    sandbox_namespace=self.resources.destinations.inventory.namespace,
                )
                return await session_lifecycle.open_session(
                    client,
                    destination,
                    spec,
                    binding=endpoint.binding,
                    setup_script=request.setup_script if request.HasField("setup_script") else None,
                )

    def history(self) -> Store:
        if self.resources.history is None:
            raise DestinationUnavailableError("Session history database not configured")
        return self.resources.history

    async def runner_session_id(self, destination: protocol_pb2.SessionDestination) -> str:
        """Keep physical runner identifiers behind this service, including on follow."""
        try:
            session_id = UUID(destination.session_id)
        except ValueError:
            return destination.session_id  # legacy caller-chosen runner ID
        if str(session_id) != destination.session_id:
            return destination.session_id
        return await self.history().runner_id(
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
            overrides = wire.launch_overrides(request)
            async with self.runner(request.sandbox) as (client, endpoint):
                sandbox_uid = UUID(request.sandbox.sandbox_uid)

                def freeze(session_id: UUID) -> bytes:
                    destination = protocol_pb2.SessionDestination(sandbox=request.sandbox, session_id=str(session_id))
                    # CreateSession callers cannot know the public ID before Open. Resolve
                    # workspace templates only after reservation, and freeze the result.
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
                        open=runner_pb2.Open(session_id=f"r-{session_id}", spec=spec),
                        bootstrap=endpoint.binding.bootstrap if endpoint.binding is not None else "",
                    )
                    if setup_script is not None:
                        launch.open.setup_script = setup_script
                    return launch.SerializeToString(deterministic=True)

                reservation = await self.history().reserve(
                    caller_namespace=caller.namespace,
                    caller_name=caller.name,
                    sandbox_namespace=self.resources.destinations.inventory.namespace,
                    sandbox_name=request.sandbox.sandbox,
                    sandbox_uid=sandbox_uid,
                    open_key=request.idempotency_key,
                    open_request=request.SerializeToString(deterministic=True),
                    launch_spec=freeze,
                )
                launch = protocol_pb2.FrozenLaunch.FromString(reservation.launch_spec)
                public_id = str(reservation.session_id)
                attachment = await session_lifecycle.open_session(
                    client,
                    protocol_pb2.SessionDestination(sandbox=request.sandbox, session_id=launch.open.session_id),
                    launch.open.spec,
                    binding=protocol_pb2.SandboxBinding(bootstrap=launch.bootstrap),
                    setup_script=launch.open.setup_script if launch.open.HasField("setup_script") else None,
                )
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
            await self.checked_sandbox(protocol_pb2.SandboxRequest(destination=request.sandbox))
            public_id = await self.history().lookup_open(
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
            # DB reservation precedes runner Open. Inventory includes sessions whose
            # native handshake failed: a retained spec is not proof of a successful Open.
            async with self.runner(request.sandbox) as (client, _):
                for summary in await client.list_sessions():
                    if summary.session_id != f"r-{public_id}":
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
            async with self.runner(destination.sandbox) as (client, _):
                result = await session_lifecycle.resume_session(client, await self.runner_session_id(destination))
                result.session_id = destination.session_id
                return result

    @override
    async def SubmitCommand(
        self, request: protocol_pb2.SubmitCommandRequest, context: grpc.aio.ServicerContext
    ) -> event_log_pb2.EventEntry:
        async with self.request(context, timeout_s=self.resources.runner_admission_ack_timeout_s):
            destination = request.destination
            if (
                not destination.session_id
                or not request.command.command_id
                or request.command.WhichOneof("operation") is None
            ):
                raise ValueError("command ID and operation are required")
            async with self.runner(destination.sandbox) as (client, _):
                return await admit_running_command(
                    client,
                    await self.runner_session_id(destination),
                    request.command,
                    after_cursor=request.follow.after_cursor,
                    timeout_s=self.resources.runner_admission_ack_timeout_s,
                )

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
                endpoint = await self.resources.destinations.resolve(destination.sandbox)
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
                if self.resources.session_changes is None:
                    raise DestinationUnavailableError("Session change notifications not configured")
                history, changes = self.history(), self.resources.session_changes
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.resources.follow_lease_s
            position = request.after_position
            with changes.subscribe() as changed:
                while loop.time() < deadline:
                    changed.clear()
                    async with asyncio.timeout(self.resources.admission_timeout_s):
                        page = await history.read_changes(after_position=position, limit=WATCH_PAGE)
                    for change in page:
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


def add_service(resources: Resources, server: grpc.aio.Server) -> None:
    protocol_pb2_grpc.add_SandboxServiceServicer_to_server(SandboxService(resources), server)

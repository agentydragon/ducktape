"""Authenticated gRPC boundary. Runners remain the command and execution-event authority."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import override

import grpc
from google.protobuf.empty_pb2 import Empty
from google.protobuf.json_format import ParseError
from kubernetes_asyncio import client as k8s_client

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
from agentplane.sandbox_service.models import InventoryError, SandboxNotFoundError
from agentplane.sandbox_service.protocol_pb2 import Sandbox, SandboxDestination
from agentplane.sandbox_service.provisioning import Provisioning
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.bearer import parse_bearer, sole_header
from agentplane.workload_auth.principal import WorkloadPrincipalRejectedError, WorkloadPrincipalResolver

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio


@dataclass(frozen=True)
class Resources:
    principals: WorkloadPrincipalResolver
    destinations: DestinationResolver
    provisioning: Provisioning
    caller_accounts: frozenset[ServiceAccountRef]
    platform_instructions: str
    admission_timeout_s: float = 15
    follow_lease_s: float = 900
    lifecycle_timeout_s: float = 300
    runner_grpc_channel_options: dict[str, int | str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if min(self.admission_timeout_s, self.follow_lease_s, self.lifecycle_timeout_s) <= 0:
            raise ValueError("timeouts must be positive")
        if not self.caller_accounts:
            raise ValueError("at least one service caller is required")

    async def authenticate(self, context: grpc.aio.ServicerContext) -> None:
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


@asynccontextmanager
async def errors(context: grpc.aio.ServicerContext) -> AsyncIterator[None]:
    """Sanitize backend failures; no Kubernetes wire responses or bearer data reach clients."""
    try:
        yield
    except WorkloadPrincipalRejectedError:
        await context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid workload bearer")
    except SandboxNotFoundError, BindingNotFoundError:
        await context.abort(grpc.StatusCode.NOT_FOUND, "sandbox incarnation not found")
    except ValueError, ParseError, UnknownPolicyError, UnknownPolicySetError:
        await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid service request or grant selection")
    except TimeoutError, OpenTimeoutError:
        await context.abort(
            grpc.StatusCode.DEADLINE_EXCEEDED, "service deadline expired; mutation outcome may be uncertain"
        )
    except InventoryError, RunnerError, StreamClosedError:
        await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "runner or sandbox state refused the request")
    except DestinationUnavailableError, ConnectionError, k8s_client.ApiException:
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
        async with self.request(context, timeout_s=self.resources.lifecycle_timeout_s):
            provisioning = self.resources.provisioning
            return await provisioning.create(request)

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
            if not request.policies:
                raise ValueError("at least one policy is required")
            binding = await provisioning.egress.grant(view, list(request.policies))
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
            return runner_pb2.ListSessionsResponse(sessions=await client.list_sessions())

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

    @override
    async def ResumeSession(
        self, request: protocol_pb2.SessionRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.Attached:
        async with self.request(context, timeout_s=self.resources.lifecycle_timeout_s):
            destination = request.destination
            if not destination.session_id:
                raise ValueError("session ID is required")
            async with self.runner(destination.sandbox) as (client, _):
                return await session_lifecycle.resume_session(client, destination.session_id)

    @override
    async def SubmitCommand(
        self, request: protocol_pb2.SubmitCommandRequest, context: grpc.aio.ServicerContext
    ) -> event_log_pb2.EventEntry:
        async with self.request(context):
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
                    destination.session_id,
                    request.command,
                    after_cursor=request.follow.after_cursor,
                    timeout_s=self.resources.admission_timeout_s,
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
                    attachment = await client.attach(destination.session_id, after_cursor=request.follow.after_cursor)
                try:
                    deadline = asyncio.get_running_loop().time() + self.resources.follow_lease_s
                    async with asyncio.timeout(self.resources.admission_timeout_s):
                        await context.write(protocol_pb2.FollowSessionResponse(attached=attachment.attached))
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


def add_service(resources: Resources, server: grpc.aio.Server) -> None:
    protocol_pb2_grpc.add_SandboxServiceServicer_to_server(SandboxService(resources), server)

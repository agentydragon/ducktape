"""Authenticated gRPC boundary for Sandboxes and their grants."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import override

import grpc
from google.protobuf.empty_pb2 import Empty
from google.protobuf.json_format import ParseError
from kubernetes_asyncio import client as k8s_client

from agentplane.proto import sandbox_service_pb2, sandbox_service_pb2_grpc
from agentplane.proto.sandbox_service_pb2 import Sandbox
from agentplane.sandbox_service.action_policy_views import UnknownPolicySetError
from agentplane.sandbox_service.egress_views import BindingNotFoundError, UnknownPolicyError
from agentplane.sandbox_service.errors import InventoryError, SandboxConflictError, SandboxNotFoundError
from agentplane.sandbox_service.provisioning import Provisioning
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.grpc_caller import authenticate
from agentplane.workload_auth.principal import WorkloadPrincipalRejectedError, WorkloadPrincipalResolver

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio


@dataclass(frozen=True)
class Resources:
    """What `SandboxService` serves from."""

    principals: WorkloadPrincipalResolver
    provisioning: Provisioning
    caller_accounts: frozenset[ServiceAccountRef]
    admission_timeout_s: float
    lifecycle_timeout_s: float

    def __post_init__(self) -> None:
        if min(self.admission_timeout_s, self.lifecycle_timeout_s) <= 0:
            raise ValueError("timeouts must be positive")
        if not self.caller_accounts:
            raise ValueError("at least one service caller is required")

    async def authenticate(self, context: grpc.aio.ServicerContext) -> ServiceAccountRef:
        return await authenticate(self.principals, self.caller_accounts, context)


@asynccontextmanager
async def errors(context: grpc.aio.ServicerContext) -> AsyncIterator[None]:
    """Sanitize backend failures; no Kubernetes wire responses or bearer data reach clients."""
    try:
        yield
    except WorkloadPrincipalRejectedError:
        await context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid workload bearer")
    except SandboxNotFoundError, BindingNotFoundError:
        await context.abort(grpc.StatusCode.NOT_FOUND, "sandbox incarnation not found")
    except SandboxConflictError:
        await context.abort(grpc.StatusCode.ALREADY_EXISTS, "sandbox name or initialization conflicts")
    except ValueError, ParseError, UnknownPolicyError, UnknownPolicySetError:
        await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid service request or grant selection")
    except TimeoutError:
        await context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "service deadline expired")
    except InventoryError:
        await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "sandbox state refused the request")
    except ConnectionError, k8s_client.ApiException:
        await context.abort(grpc.StatusCode.UNAVAILABLE, "Kubernetes unavailable")


class SandboxService(sandbox_service_pb2_grpc.SandboxServiceServicer):
    def __init__(self, resources: Resources) -> None:
        self.resources = resources

    @asynccontextmanager
    async def request(self, context: grpc.aio.ServicerContext) -> AsyncIterator[None]:
        async with errors(context), asyncio.timeout(self.resources.admission_timeout_s):
            await self.resources.authenticate(context)
            yield

    @override
    async def ListSandboxes(
        self, request: Empty, context: grpc.aio.ServicerContext
    ) -> sandbox_service_pb2.ListSandboxesResponse:
        async with self.request(context):
            inventory = self.resources.provisioning.inventory
            return sandbox_service_pb2.ListSandboxesResponse(sandboxes=await inventory.list_sandboxes())

    @override
    async def GetSandbox(
        self, request: sandbox_service_pb2.GetSandboxRequest, context: grpc.aio.ServicerContext
    ) -> sandbox_service_pb2.Sandbox:
        async with self.request(context):
            inventory = self.resources.provisioning.inventory
            if not request.name:
                raise ValueError("name is required")
            return await inventory.get(request.name)

    @override
    async def CreateSandbox(
        self, request: sandbox_service_pb2.CreateSandboxRequest, context: grpc.aio.ServicerContext
    ) -> sandbox_service_pb2.Sandbox:
        async with errors(context), asyncio.timeout(self.resources.lifecycle_timeout_s):
            principal = await self.resources.authenticate(context)
            provisioning = self.resources.provisioning
            return await provisioning.create(request, caller=principal)

    async def checked_sandbox(self, request: sandbox_service_pb2.SandboxRequest) -> tuple[Provisioning, Sandbox]:
        provisioning = self.resources.provisioning
        return provisioning, await provisioning.inventory.incarnation(request.destination)

    @override
    async def SuspendSandbox(
        self, request: sandbox_service_pb2.SandboxRequest, context: grpc.aio.ServicerContext
    ) -> Empty:
        async with self.request(context):
            provisioning, view = await self.checked_sandbox(request)
            await provisioning.inventory.suspend(view.name, uid=view.uid)
            return Empty()

    @override
    async def ResumeSandbox(
        self, request: sandbox_service_pb2.SandboxRequest, context: grpc.aio.ServicerContext
    ) -> Empty:
        async with self.request(context):
            provisioning, view = await self.checked_sandbox(request)
            if await provisioning.inventory.pending_grants(view.name) is not None:
                raise InventoryError("Sandbox provisioning is incomplete")
            await provisioning.inventory.resume(view.name, uid=view.uid)
            return Empty()

    @override
    async def DeleteSandbox(
        self, request: sandbox_service_pb2.SandboxRequest, context: grpc.aio.ServicerContext
    ) -> Empty:
        async with self.request(context):
            provisioning, view = await self.checked_sandbox(request)
            await provisioning.inventory.begin_teardown(view.name, uid=view.uid)
            return Empty()

    @override
    async def ListTemplates(
        self, request: Empty, context: grpc.aio.ServicerContext
    ) -> sandbox_service_pb2.ListTemplatesResponse:
        async with self.request(context):
            inventory = self.resources.provisioning.inventory
            return sandbox_service_pb2.ListTemplatesResponse(templates=await inventory.list_templates())

    @override
    async def GrantEgress(
        self, request: sandbox_service_pb2.GrantEgressRequest, context: grpc.aio.ServicerContext
    ) -> sandbox_service_pb2.GrantEgressResponse:
        async with self.request(context):
            provisioning, view = await self.checked_sandbox(
                sandbox_service_pb2.SandboxRequest(destination=request.destination)
            )
            if not request.egress_policies:
                raise ValueError("at least one egress policy is required")
            provisioning.endpoints.require_unreserved(request.egress_policies)
            binding = await provisioning.egress.grant(view, list(request.egress_policies))
            return sandbox_service_pb2.GrantEgressResponse(binding_name=binding.name)

    @override
    async def RevokeEgress(
        self, request: sandbox_service_pb2.RevokeEgressRequest, context: grpc.aio.ServicerContext
    ) -> Empty:
        async with self.request(context):
            provisioning = self.resources.provisioning
            if not request.binding_name:
                raise ValueError("binding name is required")
            await provisioning.egress.revoke(request.binding_name)
            return Empty()


def add_service(resources: Resources, server: grpc.aio.Server) -> None:
    sandbox_service_pb2_grpc.add_SandboxServiceServicer_to_server(SandboxService(resources), server)

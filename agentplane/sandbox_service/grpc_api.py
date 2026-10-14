"""Authenticated gRPC boundary for Sandboxes and their grants."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import override

import grpc
from google.protobuf.empty_pb2 import Empty
from google.protobuf.json_format import ParseError

from agentplane.grpc_servicer import Abort, ServicerResources, status_mapping
from agentplane.proto import sandbox_service_pb2, sandbox_service_pb2_grpc, sandboxes_pb2
from agentplane.proto.sandboxes_pb2 import Sandbox
from agentplane.sandbox_service.provisioning import Provisioning
from agentplane.sandboxes.egress_views import BindingNotFoundError, UnknownPolicyError
from agentplane.sandboxes.errors import (
    InventoryError,
    SandboxConflictError,
    SandboxNotFoundError,
    UnknownPolicySetError,
)

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio


@dataclass(frozen=True)
class Resources(ServicerResources):
    """What `SandboxService` serves from."""

    provisioning: Provisioning
    admission_timeout_s: float
    lifecycle_timeout_s: float


# No Kubernetes wire responses or bearer data reach clients.
errors = status_mapping(
    Abort((SandboxNotFoundError, BindingNotFoundError), (grpc.StatusCode.NOT_FOUND, "sandbox incarnation not found")),
    Abort(SandboxConflictError, (grpc.StatusCode.ALREADY_EXISTS, "sandbox name or initialization conflicts")),
    Abort(
        (ValueError, ParseError, UnknownPolicyError, UnknownPolicySetError),
        (grpc.StatusCode.INVALID_ARGUMENT, "invalid service request or grant selection"),
    ),
    Abort(InventoryError, (grpc.StatusCode.FAILED_PRECONDITION, "sandbox state refused the request")),
    deadline="service deadline expired",
    unavailable="Kubernetes unavailable",
)


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
    ) -> sandboxes_pb2.Sandbox:
        async with self.request(context):
            inventory = self.resources.provisioning.inventory
            if not request.name:
                raise ValueError("name is required")
            return await inventory.get(request.name)

    @override
    async def CreateSandbox(
        self, request: sandboxes_pb2.CreateSandboxRequest, context: grpc.aio.ServicerContext
    ) -> sandboxes_pb2.Sandbox:
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

    @override
    async def StreamConsole(
        self, request: sandbox_service_pb2.StreamConsoleRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[sandbox_service_pb2.StreamConsoleResponse]:
        # The stream outlives the admission timeout, so only admission is bounded.
        async with errors(context):
            async with asyncio.timeout(self.resources.admission_timeout_s):
                await self.resources.authenticate(context)
                provisioning, view = await self.checked_sandbox(
                    sandbox_service_pb2.SandboxRequest(destination=request.destination)
                )
            async for output in provisioning.inventory.console(view):
                yield sandbox_service_pb2.StreamConsoleResponse(output=output)


def add_service(resources: Resources, server: grpc.aio.Server) -> None:
    sandbox_service_pb2_grpc.add_SandboxServiceServicer_to_server(SandboxService(resources), server)

"""gRPC client for Sandbox Service, with no Kubernetes or direct-runner fallback."""

from collections.abc import Awaitable, Callable
from pathlib import Path

import grpc
from google.protobuf.empty_pb2 import Empty
from google.protobuf.message import Message

from agentplane.grpc_calls import ServiceError, call_unary
from agentplane.proto import sandbox_service_pb2, sandbox_service_pb2_grpc
from agentplane.proto.sandbox_service_pb2 import CreateSandboxRequest, Sandbox, SandboxDestination
from agentplane.sandbox_service.errors import SandboxNotFoundError

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio


class SandboxServiceClient:
    def __init__(
        self,
        channel: grpc.aio.Channel,
        *,
        # The Sandbox namespace, for callers that check destinations against it.
        namespace: str | None,
        token_file: Path,
        request_timeout_s: float,
        lifecycle_timeout_s: float,
    ) -> None:
        if min(request_timeout_s, lifecycle_timeout_s) <= 0:
            raise ValueError("timeouts must be positive")
        self.stub: sandbox_service_pb2_grpc.SandboxServiceAsyncStub = sandbox_service_pb2_grpc.SandboxServiceStub(
            channel
        )
        self.namespace = namespace
        self.token_file = token_file
        self.request_timeout_s = request_timeout_s
        self.lifecycle_timeout_s = lifecycle_timeout_s

    async def unary[T](
        self, call: Callable[..., Awaitable[T]], request: Message, *, timeout_s: float | None = None
    ) -> T:
        return await call_unary(
            call, request, token_file=self.token_file, timeout_s=timeout_s or self.request_timeout_s
        )

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

"""UI reads desired-state projections; all grant mutations go through Sandbox Service."""

from agentplane.proto.sandbox_service_pb2 import Sandbox
from agentplane.sandbox_service.client import SandboxServiceClient
from agentplane.sandbox_service.egress_views import BindingView, EgressReader, PolicyView
from agentplane.subjects import ServiceAccountRef


class EgressAccess:
    def __init__(self, read: EgressReader, service: SandboxServiceClient) -> None:
        self.read = read
        self.service = service

    async def list_policies(self) -> list[PolicyView]:
        return await self.read.list_policies()

    async def bindings_for(self, subject: ServiceAccountRef) -> list[BindingView]:
        return await self.read.bindings_for(subject)

    async def grant(self, sandbox: Sandbox, policies: list[str]) -> BindingView:
        name = await self.service.grant_egress(sandbox, policies)
        # Read-only UI projection. Failure after a successful mutation must not trigger a retry.
        for binding in await self.bindings_for(
            ServiceAccountRef(namespace=sandbox.service_account.namespace, name=sandbox.service_account.name)
        ):
            if binding.name == name:
                return binding
        raise ConnectionError("Created egress binding is no longer visible; reconcile before retrying")

    async def revoke(self, name: str) -> None:
        await self.service.revoke_egress(name)


# gazelle:include_dep @pypi//protobuf

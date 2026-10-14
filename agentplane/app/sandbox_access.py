"""The operator's writes of Sandboxes: create, suspend, resume and delete.

The app makes these Kubernetes calls for the operator who asked; the Sandbox Controller acts on
the objects without editing their spec.
"""

from collections.abc import AsyncIterator, Set

from agentplane.proto.sandboxes_pb2 import CreateSandboxRequest, Sandbox
from agentplane.sandboxes.inventory import SandboxInventory
from agentplane.subjects import ServiceAccountRef

# gazelle:include_dep @pypi//protobuf


class SandboxAccess:
    def __init__(
        self, inventory: SandboxInventory, *, default_policy_sets: Set[str], caller: ServiceAccountRef
    ) -> None:
        self._inventory = inventory
        self._default_policy_sets = default_policy_sets
        self._caller = caller

    async def list_sandboxes(self) -> list[Sandbox]:
        return await self._inventory.list_sandboxes()

    async def list_templates(self) -> list[str]:
        return await self._inventory.list_templates()

    async def get(self, name: str) -> Sandbox:
        return await self._inventory.get(name)

    async def create(self, spec: CreateSandboxRequest) -> Sandbox:
        return await self._inventory.create_validated(
            spec, default_policy_sets=self._default_policy_sets, caller=self._caller
        )

    async def suspend(self, name: str) -> None:
        await self._inventory.suspend(name, uid=(await self.get(name)).uid)

    async def resume(self, name: str) -> None:
        await self._inventory.resume(name, uid=(await self.get(name)).uid)

    async def delete(self, name: str) -> None:
        await self._inventory.begin_teardown(name, uid=(await self.get(name)).uid)

    async def stream_console(self, name: str) -> AsyncIterator[bytes]:
        async for output in self._inventory.console(await self.get(name)):
            yield output

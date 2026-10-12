"""DeleteSandbox's teardown, driven by the Sandbox Service's reconciler: seal every Session, then delete.

`SandboxInventory.begin_teardown` runs the suspended Sandbox again. Here the Runner Gateway has its runner seal
every Session, and the runner reports each seal flushed once its ingress has acknowledged it. The Sandbox
is deleted once every seal is flushed, and sealed again every `FLUSH_RETRY_S` until then. While the runner
is unreachable, the Sandbox stays in `TEARDOWN_PHASE_SEALING` for the operator: nothing deletes an
unsealed Sandbox.
"""

from dataclasses import dataclass

from agentplane.proto import runner_pb2
from agentplane.proto.sandbox_service_pb2 import Sandbox, SandboxDestination
from agentplane.runner_gateway.client import SandboxControlClient
from agentplane.sandbox_service.inventory import SandboxInventory

# gazelle:include_dep @pypi//protobuf

FLUSH_RETRY_S = 10


@dataclass(frozen=True)
class GatewaySealing:
    """Seals through the Runner Gateway replica holding the Sandbox's runner connection."""

    control: SandboxControlClient

    async def seal(self, sandbox: Sandbox) -> runner_pb2.SealResponse:
        destination = SandboxDestination(owner=sandbox.service_account, sandbox=sandbox.name, sandbox_uid=sandbox.uid)
        return await self.control.runner(destination).seal()


class SealNotFlushedError(Exception):
    """The runner sealed every Session, but its ingress has not acknowledged all of the seals yet."""


@dataclass(frozen=True)
class Teardown:
    inventory: SandboxInventory
    sealing: GatewaySealing

    async def advance(self, sandbox: Sandbox) -> None:
        seal = await self.sealing.seal(sandbox)
        unflushed = [s.session_id for s in seal.sessions if not s.flushed]
        if unflushed:
            raise SealNotFlushedError(f"{sandbox.name}: ingress has not acknowledged {unflushed=}")
        await self.inventory.delete_flushed(sandbox.name, uid=sandbox.uid)

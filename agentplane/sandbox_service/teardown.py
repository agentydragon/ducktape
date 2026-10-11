"""DeleteSandbox's teardown, driven by the Sandbox Service's reconciler: seal every Session, then delete.

`SandboxInventory.begin_teardown` runs the suspended Sandbox again. Here the Runner Gateway has its runner seal
every Session; the seal cursors go into the retention holds and the Sandbox is suspended again; it
is deleted once every hold is confirmed through its seal. While the runner is unreachable, the
Sandbox stays in `TEARDOWN_PHASE_SEALING` for the operator: nothing deletes an unsealed Sandbox.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from agentplane.sandbox_service.inventory import SandboxInventory
from agentplane.sandbox_service.protocol_pb2 import Sandbox, TeardownPhase

# gazelle:include_dep @pypi//protobuf

# Each Session's final cursor, keyed as retention holders name the Session (`Sealing.seal`).
type Seal = Callable[[Sandbox], Awaitable[dict[str, int]]]


@dataclass(frozen=True)
class Teardown:
    inventory: SandboxInventory
    seal: Seal

    async def advance(self, sandbox: Sandbox) -> None:
        match sandbox.teardown:
            case TeardownPhase.TEARDOWN_PHASE_SEALING:
                await self.inventory.record_seal(sandbox.name, uid=sandbox.uid, cursors=await self.seal(sandbox))
            case TeardownPhase.TEARDOWN_PHASE_SEALED:
                # Otherwise a hold change reconciles it again.
                await self.inventory.delete_if_released(sandbox.name, uid=sandbox.uid)

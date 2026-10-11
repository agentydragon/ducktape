"""DeleteSandbox's teardown, driven by the Sandbox Service's reconciler: seal every Session, then delete.

`SandboxInventory.begin_teardown` runs the suspended Sandbox again. Here the Runner Gateway has its runner seal
every Session; the seal cursors go into the retention holds, and the Sandbox runs on until every hold
is confirmed through its seal, since a holder confirms by following its Session to the seal; then it is
deleted. While the runner is unreachable, the Sandbox stays in `TEARDOWN_PHASE_SEALING` for the
operator: nothing deletes an unsealed Sandbox.

A runner that pushes its Events to an ingress reports each seal flushed once that ingress has
acknowledged it. Its Sandbox needs no holds: it is deleted straight from `TEARDOWN_PHASE_SEALING` once
every seal is flushed, and sealed again every `FLUSH_RETRY_S` until then.
"""

import logging
from dataclasses import dataclass

from agentplane.proto import runner_pb2
from agentplane.runner_gateway.sealing import Sealing
from agentplane.sandbox_service.client import SandboxServiceClient
from agentplane.sandbox_service.inventory import SandboxInventory
from agentplane.sandbox_service.protocol_pb2 import Sandbox, SandboxDestination, TeardownPhase

# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)

FLUSH_RETRY_S = 10


@dataclass(frozen=True)
class GatewaySealing:
    """Seals through the Runner Gateway, which reaches the Sandbox's runner. Each Session in the
    result is named as retention holders name it."""

    gateway: SandboxServiceClient

    async def seal(self, sandbox: Sandbox) -> runner_pb2.SealResponse:
        destination = SandboxDestination(owner=sandbox.service_account, sandbox=sandbox.name, sandbox_uid=sandbox.uid)
        return await self.gateway.runner(destination).seal()


class SealNotFlushedError(Exception):
    """The runner sealed every Session, but its ingress has not acknowledged all of the seals yet."""


@dataclass(frozen=True)
class Teardown:
    inventory: SandboxInventory
    # CLEANUP(added 2026-10-11): narrow to `GatewaySealing` once combined mode, which seals in-process,
    #   is deleted (SANDBOX_SERVICE_DEPLOYMENT_SPLIT's manifest PR).
    sealing: GatewaySealing | Sealing

    async def advance(self, sandbox: Sandbox) -> None:
        match sandbox.teardown:
            case TeardownPhase.TEARDOWN_PHASE_SEALING:
                seal = await self.sealing.seal(sandbox)
                if not seal.pushes:
                    # CLEANUP(added 2026-10-11): FOLLOW_PATH_RETIRE deletes this branch, the holds and
                    #   SealResponse.pushes once every runner pushes (agentplane/plans/task_dag.md).
                    await self.inventory.record_seal(
                        sandbox.name, uid=sandbox.uid, cursors={s.session_id: s.cursor for s in seal.sessions}
                    )
                    return
                unflushed = [s.session_id for s in seal.sessions if not s.flushed]
                if unflushed:
                    raise SealNotFlushedError(f"{sandbox.name}: ingress has not acknowledged {unflushed=}")
                await self.inventory.delete_flushed(sandbox.name, uid=sandbox.uid)
            case TeardownPhase.TEARDOWN_PHASE_SEALED:
                if not await self.inventory.delete_if_released(sandbox.name, uid=sandbox.uid):
                    # A hold change reconciles it again.
                    logger.info("%s is sealed; waiting for its retention holds to confirm the seal", sandbox.name)

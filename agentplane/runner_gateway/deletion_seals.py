"""Teardown's seal for Agentplane's own `Sandbox`: the replica holding a deleting Sandbox's runner
connection seals every Session and, once the ingress has acknowledged each seal, sets the Sandbox's
`Quiesced` condition, which the Sandbox Controller waits on before deleting its Pod and PVC.
A Sandbox no runner ever connected to has no Session to seal, so any replica marks it."""

import asyncio
import logging
from datetime import UTC, datetime

from tenacity import AsyncRetrying, before_sleep_log, retry_if_not_exception_type, wait_exponential

from agentplane import sandbox_resource
from agentplane.crd_group import GROUP, VERSION
from agentplane.proto import runner_pb2
from agentplane.runner_gateway.destinations import DestinationUnavailableError
from agentplane.runner_gateway.sealing import Sealing
from agentplane.sandbox_resource import Sandbox
from util.kubernetes import CustomObjectsClient
from util.kubernetes_watch import ListWatch, WatchedKind, apply_to

logger = logging.getLogger(__name__)

FIELD_MANAGER = "agentplane-runner-gateway"


class SealNotFlushedError(Exception):
    """The runner sealed every Session, but its ingress has not acknowledged all of the seals yet."""


class DeletionSeals:
    def __init__(self, custom_objects: CustomObjectsClient, *, namespace: str, sealing: Sealing) -> None:
        self._custom_objects = custom_objects
        self._namespace = namespace
        self._sealing = sealing
        # Deleting Sandboxes not yet sealed, by name.
        self._unsealed: dict[str, Sandbox] = {}
        self._running: set[str] = set()

    def _apply(self, name: str, sandbox: Sandbox | None) -> None:
        unsealed = (
            sandbox is not None and sandbox.metadata.deletion_timestamp is not None and not sandbox.sessions_sealed
        )
        apply_to(self._unsealed, name, sandbox if unsealed else None)

    def _never_connected(self, sandbox: Sandbox) -> bool:
        leases = self._sealing.control.leases
        return leases.synced and leases.read_epoch(sandbox.metadata.uid) is None

    def _startable(self) -> list[Sandbox]:
        control = self._sealing.control
        return [
            sandbox
            for name, sandbox in self._unsealed.items()
            if name not in self._running
            and (control.holder(sandbox.metadata.uid) is not None or self._never_connected(sandbox))
        ]

    async def _notify(self, *_: object) -> None:
        changed = self._sealing.control.leases.changed
        async with changed:
            changed.notify_all()

    async def run(self, *, resync_seconds: int) -> None:
        """Seal until cancelled. A Sandbox is retried when its watch or the runner connection Leases
        change: a new connection's Lease renewals re-check it once this replica registers it."""

        async def ignore(*_: object) -> None:
            pass

        watch = ListWatch(
            kinds=(
                WatchedKind(
                    name="sandboxes",
                    list=self._custom_objects.list_namespaced_custom_object,
                    args=(GROUP, VERSION, self._namespace, sandbox_resource.SANDBOXES_PLURAL),
                    parse=Sandbox.model_validate,
                    key=lambda sandbox: sandbox.metadata.name,
                    names=lambda: set(self._unsealed),
                    apply=self._apply,
                ),
            ),
            resync_seconds=resync_seconds,
            on_change=self._notify,
            on_cycle=ignore,
        )
        async with asyncio.TaskGroup() as group:
            group.create_task(watch.run(), name="deletion-seals-watch")
            while True:
                await self._sealing.control.leases.wait_for(lambda: bool(self._startable()))
                for sandbox in self._startable():
                    self._running.add(sandbox.metadata.name)
                    group.create_task(self._seal(sandbox), name=f"seal-{sandbox.metadata.name}")

    def _still_unsealed(self, sandbox: Sandbox) -> bool:
        current = self._unsealed.get(sandbox.metadata.name)
        return current is not None and current.metadata.uid == sandbox.metadata.uid

    async def _report(self, sandbox: Sandbox, message: str) -> None:
        await sandbox_resource.report_quiesced(
            self._custom_objects, sandbox, message=message, field_manager=FIELD_MANAGER, now=datetime.now(UTC)
        )

    async def _seal(self, sandbox: Sandbox) -> None:
        metadata = sandbox.metadata
        try:
            # A seal not yet flushed is asked again: the runner reports each seal flushed once its
            # ingress has acknowledged it. Any other failure is retried too, as the Sandbox stays
            # undeleted until it is sealed.
            async for attempt in AsyncRetrying(
                retry=retry_if_not_exception_type(DestinationUnavailableError),
                wait=wait_exponential(max=30),
                before_sleep=before_sleep_log(logger, logging.WARNING),
                reraise=True,
            ):
                with attempt:
                    if not self._still_unsealed(sandbox):
                        return
                    if self._never_connected(sandbox):
                        await self._report(sandbox, "no runner ever connected")
                        return
                    sealed = await self._sealing.seal(
                        namespace=metadata.namespace, name=metadata.name, sandbox_uid=metadata.uid
                    )
                    if sealed.HasField("stage") and sealed.stage != runner_pb2.QUIESCE_STAGE_QUIESCED:
                        raise SealNotFlushedError(
                            f"{metadata.name}: runner is {runner_pb2.QuiesceStage.Name(sealed.stage)}"
                        )
                    if unflushed := [s.session_id for s in sealed.sessions if not s.flushed]:
                        raise SealNotFlushedError(f"{metadata.name}: ingress has not acknowledged {unflushed=}")
                    await self._report(sandbox, f"{len(sealed.sessions)} Sessions sealed")
        except DestinationUnavailableError as error:
            # Another replica holds the connection now, or none does; whichever holds it next seals.
            logger.info("Stopped sealing deleted Sandbox %s: %s", metadata.name, error)
        finally:
            self._running.discard(metadata.name)
            await self._notify()

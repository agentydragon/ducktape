"""Event-driven reconciliation of Agentplane-owned Sandbox initialization, grants and teardown.

Kubernetes owns the Sandbox CR status; this controller owns only its Agentplane annotations,
ServiceAccount, and bindings. A full sweep still repairs missed watch events and orphaned
cross-namespace/cluster bindings (which cannot have a Sandbox owner reference).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from agentplane.sandbox_service.kubernetes_views import MANAGED_LABEL
from agentplane.sandbox_service.models import SandboxNotFoundError
from agentplane.sandbox_service.protocol_pb2 import TeardownPhase
from agentplane.sandbox_service.provisioning import Provisioning
from agentplane.sandbox_service.teardown import Teardown
from util.agent_sandbox import SANDBOX_API, SANDBOXES_PLURAL
from util.kubernetes_watch import ListWatch, WatchedKind

# Generated stubs require the protobuf runtime as a direct mypy dependency.
# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)


_SWEEP = ""  # Kubernetes Sandbox names cannot be empty.


class SandboxController:
    def __init__(self, provisioning: Provisioning, teardown: Teardown, *, resync_seconds: int = 300) -> None:
        self._provisioning = provisioning
        self._teardown = teardown
        self._resync_seconds = resync_seconds
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._queued: set[str] = set()
        self._seen: set[str] = set()
        self._uids: dict[str, str] = {}
        self._attempts: dict[str, int] = {}
        self._retries: dict[str, asyncio.Task[None]] = {}
        self._watch = ListWatch(
            kinds=(
                WatchedKind(
                    name=SANDBOXES_PLURAL,
                    list=provisioning.inventory.sandbox_list,
                    args=(*SANDBOX_API, provisioning.inventory.namespace, SANDBOXES_PLURAL),
                    kwargs={"label_selector": f"{MANAGED_LABEL}=true"},
                    key=lambda obj: str(obj["metadata"]["name"]),
                    names=self._names,
                    apply=self._apply,
                ),
            ),
            resync_seconds=resync_seconds,
            on_change=self._on_change,
            on_cycle=self._on_cycle,
        )

    def _enqueue(self, name: str) -> None:
        if name not in self._queued:
            self._queued.add(name)
            self._queue.put_nowait(name)

    def _names(self) -> set[str]:
        return set(self._seen)

    def _apply(self, name: str, obj: dict[str, Any] | None) -> None:
        if obj is None:
            self._seen.discard(name)
            self._uids.pop(name, None)
            retry = self._retries.pop(name, None)
            if retry is not None:
                retry.cancel()
            self._attempts.pop(name, None)
            # Deletion (including one missed during a watch gap) needs orphan cleanup.
            self._enqueue(_SWEEP)
        else:
            self._seen.add(name)
            uid = str(obj["metadata"].get("uid", ""))
            if self._uids.get(name) != uid:
                # Name reuse starts fresh, never inheriting an older incarnation's backoff.
                self._uids[name] = uid
                retry = self._retries.pop(name, None)
                if retry is not None:
                    retry.cancel()
                self._attempts.pop(name, None)
            self._enqueue(name)

    async def _on_change(self, kind: WatchedKind) -> None:
        pass  # WatchedKind.apply enqueues the affected name, not the entire kind.

    async def _on_cycle(self, kind: WatchedKind, timestamp: object) -> None:
        pass  # The independent safety sweep also runs when watch reconnects fail.

    async def _sweep(self) -> None:
        await self._provisioning.reconcile_once()

    async def _reconcile(self, name: str) -> None:
        try:
            sandbox = await self._provisioning.inventory.get(name)
        except SandboxNotFoundError:
            # The watch may have queued a deleted incarnation. Orphans are swept separately.
            return
        if sandbox.deleting:
            await self._provisioning.bindings.cleanup(sandbox)
        else:
            # Repair existing bindings, then complete any persisted Create intent.
            await self._provisioning.bindings.ensure(sandbox)
            await self._provisioning.ensure(sandbox)
            if sandbox.teardown != TeardownPhase.TEARDOWN_PHASE_UNSPECIFIED:
                # Nothing watches the runner Pod: until it is ready, the backoff retries this.
                await self._teardown.advance(sandbox)

    async def _retry(self, name: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            if self._retries.get(name) is asyncio.current_task():
                self._retries.pop(name, None)
                self._enqueue(name)
        finally:
            if self._retries.get(name) is asyncio.current_task():
                self._retries.pop(name, None)

    async def _worker(self) -> None:
        while True:
            name = await self._queue.get()
            self._queued.discard(name)  # Events arriving during reconciliation need a second pass.
            try:
                # An event observed during backoff is retained by the delayed requeue. This
                # avoids a tight loop when our own failed reconciliation wrote an annotation.
                if name in self._retries:
                    continue
                await (self._sweep() if name == _SWEEP else self._reconcile(name))
            except asyncio.CancelledError:
                raise
            except Exception:
                attempts = self._attempts.get(name, 0) + 1
                self._attempts[name] = attempts
                delay = min(2 ** (attempts - 1), 60)
                logger.exception("Sandbox reconciliation failed for %s; retrying in %ss", name or "sweep", delay)
                self._retries[name] = asyncio.create_task(self._retry(name, delay), name=f"sandbox-retry-{name}")
            else:
                self._attempts.pop(name, None)
                retry = self._retries.pop(name, None)
                if retry is not None:
                    retry.cancel()
            finally:
                self._queue.task_done()

    async def _periodic_sweep(self) -> None:
        while True:
            await asyncio.sleep(self._resync_seconds)
            self._enqueue(_SWEEP)

    async def run(self) -> None:
        self._enqueue(_SWEEP)  # Startup recovery, including grants whose CR has already vanished.
        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self._worker(), name="sandbox-reconcile-worker")
                tasks.create_task(self._watch.run(), name="sandbox-watch")
                tasks.create_task(self._periodic_sweep(), name="sandbox-safety-sweep")
        finally:
            for retry in self._retries.values():
                retry.cancel()
            await asyncio.gather(*self._retries.values(), return_exceptions=True)

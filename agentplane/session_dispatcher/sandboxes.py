"""The Dispatcher's view of Sandboxes: a watch on the Sandbox CR, which says whether a command can be
delivered and when a Sandbox is gone."""

import asyncio
from uuid import UUID

from agentplane import sandbox_resource
from agentplane.changes import ChangeSignal
from agentplane.crd_group import GROUP, VERSION
from agentplane.sandbox_resource import Sandbox
from agentplane.session_dispatcher.models import Reason
from util.kubernetes import CustomObjectsClient
from util.kubernetes_watch import ListWatch, WatchedKind, apply_to


def delivery_blocker(sandbox: Sandbox) -> Reason | None:
    """Why nothing can be delivered to `sandbox` now, or None when its runner is `Ready`."""
    if sandbox.ready:
        return None
    if sandbox.suspended:
        return Reason.SANDBOX_SUSPENDED
    return Reason.SANDBOX_NOT_READY


class SandboxWatch:
    def __init__(self, custom_objects: CustomObjectsClient, *, namespace: str, resync_seconds: int) -> None:
        self.changed = ChangeSignal()
        self._sandboxes: dict[str, Sandbox] = {}
        # UIDs of Sandboxes that left, by deletion or by being recreated under their name; the
        # Dispatcher ends their commands.
        self.gone: set[UUID] = set()
        self._synced = asyncio.Event()
        self._watch = ListWatch(
            kinds=(
                WatchedKind(
                    name="sandboxes",
                    list=custom_objects.list_namespaced_custom_object,
                    args=(GROUP, VERSION, namespace, sandbox_resource.SANDBOXES_PLURAL),
                    parse=Sandbox.model_validate,
                    key=lambda sandbox: sandbox.metadata.name,
                    names=lambda: set(self._sandboxes),
                    apply=self._apply,
                ),
            ),
            resync_seconds=resync_seconds,
            on_change=self._notify,
            on_cycle=self._ignore,
        )

    def get_sandbox(self, name: str) -> Sandbox | None:
        return self._sandboxes.get(name)

    def live_uids(self) -> set[UUID]:
        return {sandbox.metadata.uid for sandbox in self._sandboxes.values()}

    async def wait_synced(self) -> None:
        await self._synced.wait()

    def _apply(self, name: str, sandbox: Sandbox | None) -> None:
        previous = self._sandboxes.get(name)
        if previous is not None and (sandbox is None or sandbox.metadata.uid != previous.metadata.uid):
            self.gone.add(previous.metadata.uid)
        apply_to(self._sandboxes, name, sandbox)

    async def _notify(self, *_: object) -> None:
        if self._watch.synced:
            self._synced.set()
        self.changed.notify()

    @staticmethod
    async def _ignore(*_: object) -> None:
        pass

    async def run(self) -> None:
        await self._watch.run()

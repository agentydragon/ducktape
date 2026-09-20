"""Read-only list-and-watch of enforcement resources into this replica's Index."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from kubernetes_asyncio.client import CoreV1Api

from agentplane.crd_group import GROUP, VERSION
from agentplane.egress.policy import Index
from agentplane.egress.resources import (
    BINDINGS_PLURAL,
    CREDENTIALS_PLURAL,
    POLICIES_PLURAL,
    EgressBinding,
    EgressCredential,
    EgressPolicy,
    Secret,
)
from agentplane.kubernetes_watch import ListWatch, WatchedKind, apply_to
from util.kubernetes import CustomObjectsClient


def _name(obj: EgressPolicy | EgressBinding | EgressCredential) -> str:
    return obj.metadata.name


class Informer:
    def __init__(
        self,
        *,
        index: Index,
        custom_objects: CustomObjectsClient,
        core_v1: CoreV1Api,
        namespace: str,
        credentials_namespace: str,
        resync_seconds: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._index = index
        self._watch = ListWatch(
            kinds=(
                WatchedKind(
                    name=POLICIES_PLURAL,
                    list=custom_objects.list_namespaced_custom_object,
                    args=(GROUP, VERSION, namespace, POLICIES_PLURAL),
                    parse=EgressPolicy.model_validate,
                    key=_name,
                    names=lambda: set(index.policies),
                    apply=lambda name, obj: apply_to(index.policies, name, obj),
                ),
                WatchedKind(
                    name=BINDINGS_PLURAL,
                    list=custom_objects.list_namespaced_custom_object,
                    args=(GROUP, VERSION, namespace, BINDINGS_PLURAL),
                    parse=EgressBinding.model_validate,
                    key=_name,
                    names=lambda: set(index.bindings),
                    apply=lambda name, obj: apply_to(index.bindings, name, obj),
                ),
                WatchedKind(
                    name=CREDENTIALS_PLURAL,
                    list=custom_objects.list_namespaced_custom_object,
                    args=(GROUP, VERSION, namespace, CREDENTIALS_PLURAL),
                    parse=EgressCredential.model_validate,
                    key=_name,
                    names=lambda: set(index.credentials),
                    apply=lambda name, obj: apply_to(index.credentials, name, obj),
                ),
                WatchedKind(
                    name="secrets",
                    list=core_v1.list_namespaced_secret,
                    args=(credentials_namespace,),
                    parse=Secret.from_v1,
                    key=lambda secret: secret.name,
                    names=lambda: set(index.secrets),
                    apply=lambda name, obj: apply_to(index.secrets, name, obj),
                ),
            ),
            resync_seconds=resync_seconds,
            on_change=self._changed,
            on_cycle=self._completed,
            clock=clock,
        )

    async def run(self) -> None:
        """Watch until cancelled."""
        await self._watch.run()

    async def _changed(self, kind: WatchedKind) -> None:
        self._index.synced = self._watch.synced
        await self._index.notify()

    async def _completed(self, kind: WatchedKind, at: datetime) -> None:
        self._index.refreshed[kind.name] = at
        await self._index.notify()

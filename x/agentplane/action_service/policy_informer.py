"""Read-only list-and-watch of policy sets, bindings and caller ServiceAccounts into this replica's index.

The one write is each set's and binding's `Ready` condition: `True` once the spec parsed, `False`
with the validation report otherwise, stamped with the generation it judged so `kubectl get`
shows a bad runtime edit and a watcher can tell when a spec change has been seen.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import CoreV1Api

from util.kubernetes import CustomObjectsClient
from x.agentplane.action_service.models import ServiceAccountRef
from x.agentplane.action_service.policies.resources import (
    BINDINGS_PLURAL,
    CALLER_LABEL_SELECTOR,
    GROUP,
    POLICY_SETS_PLURAL,
    READY_CONDITION,
    SERVICE_ACCOUNTS_PLURAL,
    VERSION,
    ActionPolicyBinding,
    ActionPolicySet,
    Condition,
    InvalidResource,
    NamespacedName,
    ObjectMeta,
    parse_binding,
    parse_policy_set,
)
from x.agentplane.kubernetes_watch import ListWatch, WatchedKind, apply_to

logger = logging.getLogger(__name__)

_MERGE_PATCH = "application/merge-patch+json"


@dataclass
class PolicyIndex:
    """Everything the policy decision reads, keyed by `NamespacedName`. Mutated only by the informer;
    `changed` pulses on every mutation so readers can wait for a state rather than a duration."""

    policy_sets: dict[NamespacedName, ActionPolicySet | InvalidResource] = field(default_factory=dict)
    bindings: dict[NamespacedName, ActionPolicyBinding | InvalidResource] = field(default_factory=dict)
    service_accounts: dict[NamespacedName, ServiceAccountRef] = field(default_factory=dict)
    synced: bool = False
    changed: asyncio.Condition = field(default_factory=asyncio.Condition, repr=False)

    def eligible(self, ref: ServiceAccountRef) -> bool:
        """Whether this ServiceAccount currently carries the caller label; nothing is eligible before sync."""
        return self.synced and NamespacedName(ref.namespace, ref.name) in self.service_accounts

    def caller_service_accounts(self) -> list[ServiceAccountRef]:
        return [self.service_accounts[key] for key in sorted(self.service_accounts)]

    async def notify(self) -> None:
        async with self.changed:
            self.changed.notify_all()

    async def wait_for(self, predicate: Callable[[], bool]) -> None:
        async with self.changed:
            await self.changed.wait_for(predicate)


def _namespaced(obj: ActionPolicySet | ActionPolicyBinding | InvalidResource) -> NamespacedName:
    return NamespacedName(obj.metadata.namespace, obj.metadata.name)


def _service_account(raw: k8s_client.V1ServiceAccount) -> ServiceAccountRef:
    return ServiceAccountRef(namespace=raw.metadata.namespace, name=raw.metadata.name)


def _service_account_key(ref: ServiceAccountRef) -> NamespacedName:
    return NamespacedName(ref.namespace, ref.name)


def _keys_in(store: Mapping[NamespacedName, object], namespace: str) -> set[NamespacedName]:
    return {key for key in store if key.namespace == namespace}


def ready_condition(obj: ActionPolicySet | ActionPolicyBinding | InvalidResource, now: datetime) -> Condition:
    """The Ready condition this replica wants on the object, keeping the transition time when only
    the generation moved."""
    observed = obj.status.ready()
    if isinstance(obj, InvalidResource):
        status, reason, message = "False", "Invalid", obj.message
    else:
        status, reason, message = "True", "Valid", "spec accepted"
    transitioned = observed.last_transition_time if observed is not None and observed.status == status else now
    return Condition(
        type=READY_CONDITION,
        status=status,
        reason=reason,
        message=message,
        observed_generation=obj.metadata.generation,
        last_transition_time=transitioned,
    )


class PolicyInformer:
    def __init__(
        self,
        *,
        index: PolicyIndex,
        custom_objects: CustomObjectsClient,
        core_v1: CoreV1Api,
        namespaces: Collection[str],
        resync_seconds: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._index = index
        self._custom_objects = custom_objects
        self._clock = clock
        # What this replica last wrote, by object UID, so a write is not repeated while its own MODIFIED
        # event is in flight and a recreated object (same name, new UID) is judged afresh.
        self._written: dict[str, Condition] = {}
        kinds: list[WatchedKind] = []
        for namespace in sorted(namespaces):
            kinds += [
                WatchedKind(
                    name=f"{namespace}/{POLICY_SETS_PLURAL}",
                    list=custom_objects.list_namespaced_custom_object,
                    args=(GROUP, VERSION, namespace, POLICY_SETS_PLURAL),
                    parse=parse_policy_set,
                    key=_namespaced,
                    names=partial(_keys_in, index.policy_sets, namespace),
                    apply=lambda key, obj: apply_to(index.policy_sets, key, obj),
                ),
                WatchedKind(
                    name=f"{namespace}/{BINDINGS_PLURAL}",
                    list=custom_objects.list_namespaced_custom_object,
                    args=(GROUP, VERSION, namespace, BINDINGS_PLURAL),
                    parse=parse_binding,
                    key=_namespaced,
                    names=partial(_keys_in, index.bindings, namespace),
                    apply=lambda key, obj: apply_to(index.bindings, key, obj),
                ),
                WatchedKind(
                    name=f"{namespace}/{SERVICE_ACCOUNTS_PLURAL}",
                    list=core_v1.list_namespaced_service_account,
                    args=(namespace,),
                    kwargs={"label_selector": CALLER_LABEL_SELECTOR},
                    parse=_service_account,
                    key=_service_account_key,
                    names=partial(_keys_in, index.service_accounts, namespace),
                    apply=lambda key, obj: apply_to(index.service_accounts, key, obj),
                ),
            ]
        self._watch = ListWatch(
            kinds=kinds, resync_seconds=resync_seconds, on_change=self._changed, on_cycle=self._completed, clock=clock
        )

    async def run(self) -> None:
        """Watch until cancelled."""
        await self._watch.run()

    async def _changed(self, kind: WatchedKind) -> None:
        self._index.synced = self._watch.synced
        await self._reconcile_status()
        await self._index.notify()

    async def _completed(self, kind: WatchedKind, at: datetime) -> None:
        del kind, at

    async def _reconcile_status(self) -> None:
        now = self._clock()
        for plural, store in self._stores():
            for obj in list(store.values()):
                desired = ready_condition(obj, now)
                if _same(obj.status.ready(), desired) or _same(self._written.get(obj.metadata.uid), desired):
                    continue
                self._written[obj.metadata.uid] = desired
                await self._write(plural, obj.metadata, desired)
        # Forget objects that are gone, judged against the stores as they stand after the awaits above:
        # a snapshot taken before them would forget another kind's write still in flight, and the next
        # event would repeat it.
        held = {obj.metadata.uid for _, store in self._stores() for obj in store.values()}
        for uid in set(self._written) - held:
            del self._written[uid]

    def _stores(
        self,
    ) -> tuple[tuple[str, Mapping[NamespacedName, ActionPolicySet | ActionPolicyBinding | InvalidResource]], ...]:
        return ((POLICY_SETS_PLURAL, self._index.policy_sets), (BINDINGS_PLURAL, self._index.bindings))

    async def _write(self, plural: str, metadata: ObjectMeta, condition: Condition) -> None:
        body = {"status": {"conditions": [condition.model_dump(mode="json", by_alias=True, exclude_none=True)]}}
        try:
            await self._custom_objects.patch_namespaced_custom_object_status(
                GROUP, VERSION, metadata.namespace, plural, metadata.name, body, _content_type=_MERGE_PATCH
            )
        except k8s_client.ApiException as error:
            # The object may be gone or edited meanwhile; the next event re-judges it. Nothing else
            # depends on the write, so an outage here is loud but not fatal.
            logger.warning("Ready status write for %s/%s failed: %s", plural, metadata.name, error.reason)
            self._written.pop(metadata.uid, None)


def _same(observed: Condition | None, desired: Condition) -> bool:
    return observed is not None and (
        observed.status,
        observed.reason,
        observed.message,
        observed.observed_generation,
    ) == (desired.status, desired.reason, desired.message, desired.observed_generation)

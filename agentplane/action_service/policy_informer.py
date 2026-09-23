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

from agentplane.action_service.models import CallerPrincipal, NamespacedName, service_account_key
from agentplane.action_service.policies.resources import (
    BINDINGS_PLURAL,
    CALLER_LABEL,
    CALLER_LABEL_SELECTOR,
    POLICY_SETS_PLURAL,
    READY_CONDITION,
    SERVICE_ACCOUNTS_PLURAL,
    ActionPolicyBinding,
    ActionPolicySet,
    Condition,
    InvalidResource,
    ObjectMeta,
    parse_binding,
    parse_policy_set,
)
from agentplane.crd_group import GROUP, VERSION
from agentplane.kubernetes_watch import Freshness, ListWatch, WatchedKind, apply_to
from agentplane.subjects import ServiceAccountRef
from util.kubernetes import CustomObjectsClient

logger = logging.getLogger(__name__)

_MERGE_PATCH = "application/merge-patch+json"


@dataclass
class PolicyIndex:
    """Everything the policy decision reads, keyed by `NamespacedName`. Mutated only by the informer;
    `changed` pulses on every mutation so readers can wait for a state rather than a duration."""

    freshness: Freshness
    policy_sets: dict[NamespacedName, ActionPolicySet | InvalidResource] = field(default_factory=dict)
    bindings: dict[NamespacedName, ActionPolicyBinding | InvalidResource] = field(default_factory=dict)
    service_accounts: dict[NamespacedName, ServiceAccountRef] = field(default_factory=dict)
    # Every kind listed at least once. A latch: true from the first full list and never false again,
    # which is why it is not on its own the question `synced` answers.
    listed: bool = False
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    changed: asyncio.Condition = field(default_factory=asyncio.Condition, repr=False)

    @property
    def synced(self) -> bool:
        """Whether this replica's copy is complete *and* still moving, which is what acting on it needs.

        Listing every kind once is the weaker fact, and taking it for this one is how a replica whose
        watches have wedged goes on auto-deciding from a snapshot it took hours ago: the bindings it
        holds are the bindings an operator has since revoked, and nothing about the answers looks
        wrong. A watch the server keeps refusing stops advancing `freshness` instead.
        """
        return self.listed and self.freshness.fresh(self.clock())

    def admits(self, ref: ServiceAccountRef) -> bool:
        """Whether this ServiceAccount may call the service at all: it carries the caller label now.

        Nothing is admitted while the index is out of sync, so an informer that cannot reach the API
        server refuses every caller rather than serving a picture it cannot vouch for.
        """
        return self.synced and service_account_key(ref) in self.service_accounts

    def admit(self, ref: ServiceAccountRef) -> CallerPrincipal | None:
        """The caller this account is, or None where the label does not admit it.

        Both transports decide admission here so the rule cannot be tightened on one door and not
        the other, and so the refusal is logged once, in the same words, wherever it happens. What
        the caller is told is each surface's own business: which account was presented is not
        something an attacker should learn from the difference between two refusals.
        """
        if not self.admits(ref):
            logger.warning("workload bearer refused: %s/%s does not carry %s", ref.namespace, ref.name, CALLER_LABEL)
            return None
        return CallerPrincipal(account=ref)

    def caller_service_accounts(self) -> list[ServiceAccountRef]:
        return [self.service_accounts[key] for key in sorted(self.service_accounts)]

    async def notify(self) -> None:
        async with self.changed:
            self.changed.notify_all()

    async def wait_for(self, predicate: Callable[[], bool]) -> None:
        async with self.changed:
            await self.changed.wait_for(predicate)


def _service_account(raw: k8s_client.V1ServiceAccount) -> ServiceAccountRef:
    return ServiceAccountRef(namespace=raw.metadata.namespace, name=raw.metadata.name)


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
    ) -> None:
        self._index = index
        self._custom_objects = custom_objects
        # The index's, not one of its own: it stamps the cycle times the index reads back as
        # freshness, and a second clock would be one more thing that has to agree.
        self._clock = index.clock
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
                    key=lambda obj: obj.namespaced_name,
                    names=partial(_keys_in, index.policy_sets, namespace),
                    apply=lambda key, obj: apply_to(index.policy_sets, key, obj),
                ),
                WatchedKind(
                    name=f"{namespace}/{BINDINGS_PLURAL}",
                    list=custom_objects.list_namespaced_custom_object,
                    args=(GROUP, VERSION, namespace, BINDINGS_PLURAL),
                    parse=parse_binding,
                    key=lambda obj: obj.namespaced_name,
                    names=partial(_keys_in, index.bindings, namespace),
                    apply=lambda key, obj: apply_to(index.bindings, key, obj),
                ),
                WatchedKind(
                    name=f"{namespace}/{SERVICE_ACCOUNTS_PLURAL}",
                    list=core_v1.list_namespaced_service_account,
                    args=(namespace,),
                    kwargs={"label_selector": CALLER_LABEL_SELECTOR},
                    parse=_service_account,
                    key=service_account_key,
                    names=partial(_keys_in, index.service_accounts, namespace),
                    apply=lambda key, obj: apply_to(index.service_accounts, key, obj),
                ),
            ]
        self._watch = ListWatch(
            kinds=kinds,
            resync_seconds=resync_seconds,
            on_change=self._changed,
            on_cycle=self._completed,
            clock=index.clock,
        )

    async def run(self) -> None:
        """Watch until cancelled."""
        await self._watch.run()

    async def _changed(self, kind: WatchedKind) -> None:
        self._index.listed = self._watch.synced
        await self._reconcile_status()
        await self._index.notify()

    async def _completed(self, kind: WatchedKind, at: datetime) -> None:
        self._index.freshness.record(kind.name, at)
        await self._index.notify()

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

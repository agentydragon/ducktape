"""List-and-watch of enforcement resources into this replica's Index, and the `EgressReady`
condition it keeps on each PolicyBinding: the one write the proxy makes to the API server."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial

from kubernetes_asyncio.client import CoreV1Api

from agentplane.crd_group import GROUP, VERSION
from agentplane.egress.policy import Index
from agentplane.egress.resources import (
    BINDINGS_PLURAL,
    CREDENTIALS_PLURAL,
    EGRESS_RULE_TYPE,
    POLICIES_PLURAL,
    EgressBinding,
    EgressCredential,
    EgressPolicy,
    Rule,
    Secret,
)
from agentplane.policy_sets import (
    POLICY_BINDINGS_PLURAL,
    POLICY_SET_UNAVAILABLE,
    POLICY_SETS_PLURAL,
    Condition,
    ConditionReporter,
    PolicyBinding,
    parse_policy_set,
    resolve_binding,
    set_condition,
)
from util.kubernetes import CustomObjectsClient
from util.kubernetes_watch import ListWatch, WatchedKind, apply_to

EGRESS_READY = "EgressReady"
FIELD_MANAGER = "agentplane-egress"


def _name(obj: EgressPolicy | EgressBinding | EgressCredential | PolicyBinding) -> str:
    return obj.metadata.name


def egress_ready(index: Index, binding: PolicyBinding, now: datetime) -> Condition:
    """Whether every set the binding names exists; the sets that do grant either way."""
    resolution = resolve_binding(index.policy_sets, binding)
    if resolution.missing:
        return set_condition(
            binding,
            EGRESS_READY,
            ready=False,
            reason=POLICY_SET_UNAVAILABLE,
            message=f"missing PolicySets: {', '.join(resolution.missing)}",
            now=now,
        )
    rules = sum(len(policy_set.rules) for policy_set in resolution.sets)
    return set_condition(
        binding, EGRESS_READY, ready=True, reason="Enforced", message=f"egress rules: {rules}", now=now
    )


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
        self._clock = clock
        self._reporter = ConditionReporter(custom_objects, field_manager=FIELD_MANAGER)
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
                    name=POLICY_SETS_PLURAL,
                    list=custom_objects.list_namespaced_custom_object,
                    args=(GROUP, VERSION, namespace, POLICY_SETS_PLURAL),
                    parse=partial(parse_policy_set, types={EGRESS_RULE_TYPE}, parse_rule=Rule.model_validate),
                    key=lambda policy_set: policy_set.name,
                    names=lambda: set(index.policy_sets),
                    apply=lambda name, obj: apply_to(index.policy_sets, name, obj),
                ),
                WatchedKind(
                    name=POLICY_BINDINGS_PLURAL,
                    list=custom_objects.list_namespaced_custom_object,
                    args=(GROUP, VERSION, namespace, POLICY_BINDINGS_PLURAL),
                    parse=PolicyBinding.model_validate,
                    key=_name,
                    names=lambda: set(index.policy_bindings),
                    apply=lambda name, obj: apply_to(index.policy_bindings, name, obj),
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
        # Until every kind has listed, a set not yet seen would read as missing.
        if self._index.synced:
            now = self._clock()
            await self._reporter.report(
                (binding, egress_ready(self._index, binding, now))
                for binding in list(self._index.policy_bindings.values())
            )
        await self._index.notify()

    async def _completed(self, kind: WatchedKind, at: datetime) -> None:
        self._index.freshness.record(kind.name, at)
        await self._index.notify()

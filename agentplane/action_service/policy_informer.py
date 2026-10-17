"""Read-only list-and-watch of policy sets, bindings and caller ServiceAccounts into this replica's index.

The one write is each `PolicyBinding`'s `ActionsReady` condition, `False` while a set it names is
missing or holds an Action rule this service refuses; a `PolicySet` has no status of its own.
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
from agentplane.action_service.policies.registry import Policy
from agentplane.action_service.policies.resources import (
    ACTIONS_FIELD_MANAGER,
    ACTIONS_READY_CONDITION,
    CALLER_LABEL,
    CALLER_LABEL_SELECTOR,
    SERVICE_ACCOUNTS_PLURAL,
    parse_action_rule,
)
from agentplane.kubernetes_watch import Freshness
from agentplane.policy_sets import PolicyBinding, PolicySet, RefusedPolicySet
from agentplane.policy_watch import PolicyWatch, ReadyCondition
from agentplane.subjects import ServiceAccountRef
from util.kubernetes import CustomObjectsClient
from util.kubernetes_watch import WatchedKind, apply_to

logger = logging.getLogger(__name__)


@dataclass
class PolicyIndex:
    """Everything the policy decision reads, keyed by `NamespacedName`. Mutated only by the informer;
    `changed` pulses on every mutation so readers can wait for a state rather than a duration."""

    freshness: Freshness
    # The policy namespace's kinds, of which this service reads the Action rules.
    rule_sets: dict[str, PolicySet[Policy] | RefusedPolicySet] = field(default_factory=dict)
    policy_bindings: dict[str, PolicyBinding] = field(default_factory=dict)
    service_accounts: dict[NamespacedName, ServiceAccountRef] = field(default_factory=dict)
    # Every kind listed at least once. A latch: true from the first full list and never false again,
    # which is why it is not on its own the question `synced` answers.
    listed: bool = False
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    changed: asyncio.Condition = field(default_factory=asyncio.Condition, repr=False)

    @property
    def synced(self) -> bool:
        """Listed once **and** watches still advancing. Listing alone would let a wedged replica
        auto-decide from a stale snapshot."""
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


class PolicyInformer:
    def __init__(
        self,
        *,
        index: PolicyIndex,
        custom_objects: CustomObjectsClient,
        core_v1: CoreV1Api,
        policy_namespace: str,
        caller_service_account_namespaces: Collection[str],
        resync_seconds: int,
    ) -> None:
        self._index = index
        self._watch = PolicyWatch(
            custom_objects=custom_objects,
            namespace=policy_namespace,
            parse_rule=parse_action_rule,
            policy_sets=index.rule_sets,
            policy_bindings=index.policy_bindings,
            condition=ReadyCondition(
                type=ACTIONS_READY_CONDITION,
                field_manager=ACTIONS_FIELD_MANAGER,
                applied_reason="Applied",
                rules_label="Action rules",
            ),
            freshness=index.freshness,
            other_kinds=[
                WatchedKind(
                    name=f"{namespace}/{SERVICE_ACCOUNTS_PLURAL}",
                    list=core_v1.list_namespaced_service_account,
                    args=(namespace,),
                    kwargs={"label_selector": CALLER_LABEL_SELECTOR},
                    parse=_service_account,
                    key=service_account_key,
                    names=partial(_keys_in, index.service_accounts, namespace),
                    apply=lambda key, obj: apply_to(index.service_accounts, key, obj),
                )
                for namespace in sorted(caller_service_account_namespaces)
            ],
            resync_seconds=resync_seconds,
            # The index's, not one of its own: it stamps the cycle times the index reads back as
            # freshness, and a second clock would be one more thing that has to agree.
            clock=index.clock,
            mark_synced=self._mark_listed,
            notify=index.notify,
        )

    async def run(self) -> None:
        """Watch until cancelled."""
        await self._watch.run()

    def _mark_listed(self, listed: bool) -> None:
        self._index.listed = listed

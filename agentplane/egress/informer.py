"""List-and-watch of enforcement resources into this replica's Index, and the `EgressReady`
condition it keeps on each PolicyBinding: the one write the proxy makes to the API server."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from kubernetes_asyncio.client import CoreV1Api

from agentplane.crd_group import GROUP, VERSION
from agentplane.egress.policy import Index
from agentplane.egress.resources import CREDENTIALS_PLURAL, EGRESS_RULE_TYPE, EgressCredential, Rule, Secret
from agentplane.policy_watch import PolicyWatch, ReadyCondition
from util.kubernetes import CustomObjectsClient
from util.kubernetes_watch import WatchedKind, apply_to

EGRESS_READY = "EgressReady"
FIELD_MANAGER = "agentplane-egress"


def _egress_rule(raw: dict[str, Any]) -> Rule | None:
    return Rule.model_validate(raw) if raw["type"] == EGRESS_RULE_TYPE else None


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
        self._watch = PolicyWatch(
            custom_objects=custom_objects,
            namespace=namespace,
            parse_rule=_egress_rule,
            policy_sets=index.policy_sets,
            policy_bindings=index.policy_bindings,
            condition=ReadyCondition(
                type=EGRESS_READY, field_manager=FIELD_MANAGER, applied_reason="Enforced", rules_label="egress rules"
            ),
            freshness=index.freshness,
            other_kinds=[
                WatchedKind(
                    name=CREDENTIALS_PLURAL,
                    list=custom_objects.list_namespaced_custom_object,
                    args=(GROUP, VERSION, namespace, CREDENTIALS_PLURAL),
                    parse=EgressCredential.model_validate,
                    key=lambda credential: credential.metadata.name,
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
            ],
            resync_seconds=resync_seconds,
            clock=clock,
            mark_synced=self._mark_synced,
            notify=index.notify,
        )

    async def run(self) -> None:
        """Watch until cancelled."""
        await self._watch.run()

    def _mark_synced(self, synced: bool) -> None:
        self._index.synced = synced

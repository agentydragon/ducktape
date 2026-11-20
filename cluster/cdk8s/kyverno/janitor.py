"""The janitor: the Kyverno NamespacedDeletingPolicy that reaps what an ephemeral namespace's users
leave behind. Kyverno admits a deleting policy only while its cleanup controller may delete every
kind it matches; `policies.py`'s cleanup-controller ClusterRoles grant that.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from cdk8s import ApiObjectMetadata
from constructs import Construct
from kyverno_namespaceddeletingpolicy_crds.io.kyverno.policies import (
    NamespacedDeletingPolicy,
    NamespacedDeletingPolicySpec,
    NamespacedDeletingPolicySpecConditions,
    NamespacedDeletingPolicySpecMatchConstraints,
    NamespacedDeletingPolicySpecMatchConstraintsResourceRules,
)


@dataclass(frozen=True)
class ApiResource:
    group: str
    version: str
    resource: str


def janitor(
    scope: Construct, id: str, *, name: str, namespace: str, schedule: str, resources: Sequence[ApiResource]
) -> NamespacedDeletingPolicy:
    """Deletes the `resources` objects in `namespace` created more than 7 days ago, checked on the
    cron `schedule`. Our policy: 7 days is every ephemeral namespace's backstop."""
    return NamespacedDeletingPolicy(
        scope,
        id,
        metadata=ApiObjectMetadata(name=name, namespace=namespace),
        spec=NamespacedDeletingPolicySpec(
            schedule=schedule,
            match_constraints=NamespacedDeletingPolicySpecMatchConstraints(
                resource_rules=[
                    NamespacedDeletingPolicySpecMatchConstraintsResourceRules(
                        api_groups=[resource.group], api_versions=[resource.version], resources=[resource.resource]
                    )
                    for resource in resources
                ]
            ),
            conditions=[
                NamespacedDeletingPolicySpecConditions(
                    name="older-than-7-days",
                    expression="time.now() - timestamp(object.metadata.creationTimestamp) > duration('168h')",
                )
            ],
        ),
    )

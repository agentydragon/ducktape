"""The janitor: the Kyverno CleanupPolicy that reaps what an ephemeral namespace's users leave
behind. Kyverno admits a CleanupPolicy only while its cleanup controller may delete every kind it
matches; `policies.py`'s cleanup-controller ClusterRoles grant that.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from constructs import Construct
from kyverno_cleanuppolicy_crds.io.kyverno import (
    CleanupPolicySpecConditions,
    CleanupPolicySpecConditionsAll,
    CleanupPolicySpecConditionsAllOperator,
    CleanupPolicySpecMatch,
    CleanupPolicySpecMatchAny,
    CleanupPolicySpecMatchAnyResources,
)

from cluster.cdk8s.providers.kyverno.cleanup_policy import CleanupPolicy


def janitor(
    scope: Construct, id: str, *, name: str, namespace: str, schedule: str, kinds: Sequence[str]
) -> CleanupPolicy:
    """Deletes the `kinds` objects in `namespace` created more than 7 days ago, checked on the cron
    `schedule`. Our policy: 7 days is every ephemeral namespace's backstop."""
    return CleanupPolicy(
        scope,
        id,
        metadata=ApiObjectMetadata(name=name, namespace=namespace),
        schedule=schedule,
        match=CleanupPolicySpecMatch(
            any=[CleanupPolicySpecMatchAny(resources=CleanupPolicySpecMatchAnyResources(kinds=list(kinds)))]
        ),
        conditions=CleanupPolicySpecConditions(
            all=[
                CleanupPolicySpecConditionsAll(
                    key="{{ time_since('', '{{ target.metadata.creationTimestamp }}', '') }}",
                    operator=CleanupPolicySpecConditionsAllOperator.GREATER_THAN,
                    value="168h",
                )
            ]
        ),
    )

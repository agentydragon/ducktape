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

# agent-sandbox's CRs. A Sandbox whose owner forgot shutdownTime (the default shutdownPolicy is
# Retain) otherwise pins quota forever. Reaped at the CR level: the controller recreates a
# Sandbox's pod, so a pod-level janitor only churns. A warm pool recreates the sandboxes reaped
# from it, a harmless periodic refresh. Delete RBAC: `policies.cleanup_controller_sandboxes_chart`.
SANDBOX_KINDS = ("agents.x-k8s.io/v1beta1/Sandbox", "extensions.agents.x-k8s.io/v1beta1/SandboxClaim")


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

"""Ergonomic wrapper for Kyverno's namespaced `CleanupPolicy`, in `cluster_policy.ClusterPolicy`'s
shape: a class named after the kind whose keywords are `CleanupPolicySpec` fields under their own
names.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata
from constructs import Construct
from kyverno_cleanuppolicy_crds.io.kyverno import (
    CleanupPolicy as _CleanupPolicy,
    CleanupPolicySpec,
    CleanupPolicySpecConditions,
    CleanupPolicySpecMatch,
)


class CleanupPolicy(_CleanupPolicy):
    """Kyverno's `CleanupPolicy`: on each cron `schedule`, deletes the resources in its namespace
    that `match` selects and `conditions` admits. `schedule` and `match` are the fields the CRD
    requires; `None` leaves `conditions` unset, so every matched resource is deleted."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        schedule: str,
        match: CleanupPolicySpecMatch,
        conditions: CleanupPolicySpecConditions | None = None,
    ) -> None:
        super().__init__(
            scope, id, metadata=metadata, spec=CleanupPolicySpec(schedule=schedule, match=match, conditions=conditions)
        )

"""Ergonomic wrapper for Agentplane's own `ServiceAccountGrant` CRD
(agentplane/crds/manifests/crd-serviceaccountgrants.yaml), following cdk8s-plus's own construction
pattern: a class named after the kind.
"""

from __future__ import annotations

from collections.abc import Sequence

from agentplane_serviceaccountgrant_crds.works.allegedly.agentplane import (
    ServiceAccountGrant as _ServiceAccountGrant,
    ServiceAccountGrantSpec,
    ServiceAccountGrantSpecServiceAccount,
)
from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import ServiceAccount
from constructs import Construct

from cluster.cdk8s.providers.agentplane.kubernetes_role_set import KubernetesRoleSet


class ServiceAccountGrant(_ServiceAccountGrant):
    """Agentplane's `ServiceAccountGrant`: attaches `role_sets`, which must be in the grant's
    namespace, to `service_account`. No `expiresAt`: a Flux-owned grant would be recreated."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        service_account: ServiceAccount,
        role_sets: Sequence[KubernetesRoleSet],
    ) -> None:
        namespace = service_account.metadata.namespace
        assert namespace is not None, f"{service_account.name=} has no namespace"
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=ServiceAccountGrantSpec(
                service_account=ServiceAccountGrantSpecServiceAccount(namespace=namespace, name=service_account.name),
                role_sets=[role_set.name for role_set in role_sets],
            ),
        )

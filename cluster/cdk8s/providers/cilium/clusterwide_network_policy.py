"""Ergonomic wrapper for Cilium's `CiliumClusterwideNetworkPolicy`, in `network_policy.NetworkPolicy`'s
shape. `cdk8s_import` generates the clusterwide CRD's structs under their own names, so
`network_policy`'s `EgressRule` factories do not fit its slots.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from cilium_clusterwide_crds.io.cilium import (
    CiliumClusterwideNetworkPolicy as _CiliumClusterwideNetworkPolicy,
    CiliumClusterwideNetworkPolicySpec,
    CiliumClusterwideNetworkPolicySpecEgress,
    CiliumClusterwideNetworkPolicySpecEndpointSelector,
)
from constructs import Construct


class ClusterwideNetworkPolicy(_CiliumClusterwideNetworkPolicy):
    """Cilium's `CiliumClusterwideNetworkPolicy`, a cluster-scoped `CiliumNetworkPolicy`: its
    `endpoint_selector` selects Pods in every namespace. `None` leaves `egress` unset."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        endpoint_selector: CiliumClusterwideNetworkPolicySpecEndpointSelector,
        egress: Sequence[CiliumClusterwideNetworkPolicySpecEgress] | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=CiliumClusterwideNetworkPolicySpec(
                endpoint_selector=endpoint_selector, egress=list(egress) if egress is not None else None
            ),
        )

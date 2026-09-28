"""Ergonomic wrapper for Gateway API's `Gateway`, following cdk8s-plus's own construction
pattern: a class named after the kind, constructed as `Gateway(scope, id, ...)`. Every keyword is
a `GatewaySpec` field under its own name and type; `None` leaves it unset, so the CRD's own
default applies. No GatewayClass, hostname or certificate fact lives here -- those are this
cluster's own values, built in `cluster.cdk8s.gateway`.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from constructs import Construct
from gateway_api_gateway_crds.io.k8s.networking.gateway import (
    Gateway as _Gateway,
    GatewaySpec,
    GatewaySpecAddresses,
    GatewaySpecAllowedListeners,
    GatewaySpecDefaultScope,
    GatewaySpecInfrastructure,
    GatewaySpecListeners,
    GatewaySpecTls,
)


class Gateway(_Gateway):
    """Gateway API's `Gateway`. `gateway_class_name` and `listeners` are the only fields the CRD
    itself requires."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        gateway_class_name: str,
        listeners: Sequence[GatewaySpecListeners],
        addresses: Sequence[GatewaySpecAddresses] | None = None,
        allowed_listeners: GatewaySpecAllowedListeners | None = None,
        default_scope: GatewaySpecDefaultScope | None = None,
        infrastructure: GatewaySpecInfrastructure | None = None,
        tls: GatewaySpecTls | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=GatewaySpec(
                gateway_class_name=gateway_class_name,
                listeners=list(listeners),
                addresses=list(addresses) if addresses is not None else None,
                allowed_listeners=allowed_listeners,
                default_scope=default_scope,
                infrastructure=infrastructure,
                tls=tls,
            ),
        )

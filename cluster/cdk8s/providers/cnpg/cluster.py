"""Ergonomic wrapper for CloudNativePG's `Cluster`, following cdk8s-plus's own
construction pattern: a class named after the kind, constructed as
`Cluster(scope, id, props)`. No image pin, instance count, topology, or other deployment
policy lives here -- every field is required or `None`-defaults to the CRD's own default.
"""

from __future__ import annotations

from collections.abc import Sequence

from cnpg_cluster_crds.io.cnpg.postgresql import (
    Cluster as _Cluster,
    ClusterSpec,
    ClusterSpecAffinity,
    ClusterSpecBootstrap,
    ClusterSpecBootstrapInitdb,
    ClusterSpecManaged,
    ClusterSpecMonitoring,
    ClusterSpecPlugins,
    ClusterSpecPostgresql,
    ClusterSpecProbes,
    ClusterSpecResources,
    ClusterSpecStorage,
)
from constructs import Construct

from cluster.cdk8s.metadata import metadata


class Cluster(_Cluster):
    """CloudNativePG's `Cluster`. `storage_class`/`size` are `ClusterSpec.storage`'s two
    fields under their own names; `initdb` is `bootstrap.initdb` (the only bootstrap mode
    wrapped here -- pass a full `ClusterSpec` via the generated binding directly for any
    other). Every other keyword is a `ClusterSpec` field under the same name and type;
    `None` leaves it unset, so CNPG's own default applies.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        name: str,
        namespace: str,
        storage_class: str,
        size: str,
        instances: int,
        image_name: str | None = None,
        initdb: ClusterSpecBootstrapInitdb | None = None,
        affinity: ClusterSpecAffinity | None = None,
        managed: ClusterSpecManaged | None = None,
        postgresql: ClusterSpecPostgresql | None = None,
        plugins: Sequence[ClusterSpecPlugins] | None = None,
        resources: ClusterSpecResources | None = None,
        probes: ClusterSpecProbes | None = None,
        monitoring: ClusterSpecMonitoring | None = None,
        annotations: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata(name, namespace, annotations=annotations),
            spec=ClusterSpec(
                instances=instances,
                image_name=image_name,
                affinity=affinity,
                storage=ClusterSpecStorage(storage_class=storage_class, size=size),
                postgresql=postgresql,
                plugins=plugins,
                resources=resources,
                probes=probes,
                monitoring=monitoring,
                bootstrap=None if initdb is None else ClusterSpecBootstrap(initdb=initdb),
                managed=managed,
            ),
        )

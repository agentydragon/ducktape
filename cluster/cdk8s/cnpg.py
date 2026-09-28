"""CNPG `Cluster`s: the shape every generated Postgres shares, and its placement."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from cdk8s import ApiObjectMetadata
from cnpg_cluster_crds.io.cnpg.postgresql import (
    ClusterSpecAffinity,
    ClusterSpecAffinityTolerations,
    ClusterSpecBootstrap,
    ClusterSpecBootstrapInitdb,
    ClusterSpecManaged,
    ClusterSpecMonitoring,
    ClusterSpecPlugins,
    ClusterSpecPostgresql,
    ClusterSpecProbes,
    ClusterSpecProbesLiveness,
    ClusterSpecProbesLivenessIsolationCheck,
    ClusterSpecResources,
    ClusterSpecStorage,
)
from constructs import Construct

from cluster.cdk8s import node_scheduling
from cluster.cdk8s.local_path_provisioner import SSD_STORAGE_CLASSES
from cluster.cdk8s.providers.cnpg.cluster import Cluster

# renovate: datasource=docker
POSTGRES_IMAGE = "ghcr.io/cloudnative-pg/postgresql:18.1-system-trixie"

_CONTROL_PLANE_TOLERATION = ClusterSpecAffinityTolerations(
    key=node_scheduling.CONTROL_PLANE_TAINT_KEY, operator="Exists", effect="NoSchedule"
)


def _affinity(*, node_selector: Mapping[str, str], storage_class: str) -> ClusterSpecAffinity:
    """One instance per node within `node_selector`, required: instances sharing a node
    share its failure, and preferred anti-affinity lets the scheduler co-locate them. A
    Cluster tolerates control-plane nodes exactly when its storage is SSD, since OVH's
    SSD nodes are its control planes."""
    return ClusterSpecAffinity(
        node_selector=node_selector,
        tolerations=[_CONTROL_PLANE_TOLERATION] if storage_class in SSD_STORAGE_CLASSES else None,
        topology_key="kubernetes.io/hostname",
        pod_anti_affinity_type="required",
    )


def same_owner_initdb(
    name: str, *, locale_c_type: str | None = None, locale_collate: str | None = None
) -> ClusterSpecBootstrapInitdb:
    """`ClusterSpecBootstrapInitdb` for the common case where the app's database and its
    owning role both take the app's own name."""
    return ClusterSpecBootstrapInitdb(
        database=name, owner=name, locale_c_type=locale_c_type, locale_collate=locale_collate
    )


def cluster(
    scope: Construct,
    id: str,
    *,
    name: str,
    namespace: str,
    storage_class: str,
    size: str,
    node_selector: Mapping[str, str],
    initdb: ClusterSpecBootstrapInitdb | None = None,
    instances: int = 2,
    image_name: str | None = POSTGRES_IMAGE,
    annotations: dict[str, str] | None = None,
    managed: ClusterSpecManaged | None = None,
    postgresql: ClusterSpecPostgresql | None = None,
    plugins: Sequence[ClusterSpecPlugins] | None = None,
    resources: ClusterSpecResources | None = None,
) -> Cluster:
    """Add a CNPG `Cluster`. Keywords are `ClusterSpec` fields under the same names and
    types, except `initdb` (`bootstrap.initdb`), `storage_class`/`size` (`storage`) and
    `node_selector` (the zone or region pin), from which `_affinity` derives placement.
    Our policy: 2 instances (cluster/docs/cnpg_conventions.md R2) on `POSTGRES_IMAGE`;
    `image_name=None` runs the operator's default image. Every Cluster disables the
    liveness isolation check -- CNPG 1.27+ otherwise kills a primary it considers isolated
    on a transient network blip -- and enables the PodMonitor.
    """
    return Cluster(
        scope,
        id,
        metadata=ApiObjectMetadata(name=name, namespace=namespace, annotations=annotations),
        storage=ClusterSpecStorage(storage_class=storage_class, size=size),
        instances=instances,
        image_name=image_name,
        bootstrap=None if initdb is None else ClusterSpecBootstrap(initdb=initdb),
        affinity=_affinity(node_selector=node_selector, storage_class=storage_class),
        managed=managed,
        postgresql=postgresql,
        plugins=plugins,
        resources=resources,
        probes=ClusterSpecProbes(
            liveness=ClusterSpecProbesLiveness(isolation_check=ClusterSpecProbesLivenessIsolationCheck(enabled=False))
        ),
        # TODO: Migrate to manually managed PodMonitors (enablePodMonitor is deprecated).
        monitoring=ClusterSpecMonitoring(enable_pod_monitor=True),
    )

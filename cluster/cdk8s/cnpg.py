"""CNPG `Cluster`s: the shape every generated Postgres shares, and its placement."""

from __future__ import annotations

from collections.abc import Sequence

from cnpg_cluster_crds.io.cnpg.postgresql import (
    Cluster,
    ClusterSpec,
    ClusterSpecAffinity,
    ClusterSpecAffinityNodeAffinity,
    ClusterSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecution,
    ClusterSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreference,
    ClusterSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreferenceMatchExpressions,
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

from cluster.cdk8s.metadata import metadata

POSTGRES_IMAGE = "ghcr.io/cloudnative-pg/postgresql:18.1-system-trixie"

# Soft anti-affinity off control-plane nodes, whose etcd is on a rotational HDD that
# co-located I/O starves (2026-06-28 outage).
OFF_CONTROL_PLANE_NODE_AFFINITY = ClusterSpecAffinityNodeAffinity(
    preferred_during_scheduling_ignored_during_execution=[
        ClusterSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecution(
            weight=100,
            preference=ClusterSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreference(
                match_expressions=[
                    ClusterSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreferenceMatchExpressions(
                        key="node-role.kubernetes.io/control-plane", operator="DoesNotExist"
                    )
                ]
            ),
        )
    ]
)

CONTROL_PLANE_TOLERATION = ClusterSpecAffinityTolerations(
    key="node-role.kubernetes.io/control-plane", operator="Exists", effect="NoSchedule"
)


def affinity(*, node_selector: dict[str, str], tolerate_control_plane: bool) -> ClusterSpecAffinity:
    """One instance per node within `node_selector`, preferring workers over control planes;
    `tolerate_control_plane` lets an instance still land on one."""
    return ClusterSpecAffinity(
        node_selector=node_selector,
        tolerations=[CONTROL_PLANE_TOLERATION] if tolerate_control_plane else None,
        topology_key="kubernetes.io/hostname",
        node_affinity=OFF_CONTROL_PLANE_NODE_AFFINITY,
    )


def cluster(
    scope: Construct,
    id: str,
    *,
    name: str,
    namespace: str,
    storage_class: str,
    size: str,
    affinity: ClusterSpecAffinity,
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
    types, except `initdb` (`bootstrap.initdb`) and `storage_class`/`size` (`storage`).
    Our policy: 2 instances (cluster/docs/cnpg_conventions.md R2) on `POSTGRES_IMAGE`;
    `image_name=None` runs the operator's default image. Every Cluster disables the
    liveness isolation check -- CNPG 1.27+ otherwise kills a primary it considers isolated
    on a transient network blip -- and enables the PodMonitor.
    """
    return Cluster(
        scope,
        id,
        metadata=metadata(name, namespace, annotations=annotations),
        spec=ClusterSpec(
            instances=instances,
            image_name=image_name,
            probes=ClusterSpecProbes(
                liveness=ClusterSpecProbesLiveness(
                    isolation_check=ClusterSpecProbesLivenessIsolationCheck(enabled=False)
                )
            ),
            affinity=affinity,
            storage=ClusterSpecStorage(storage_class=storage_class, size=size),
            postgresql=postgresql,
            plugins=plugins,
            resources=resources,
            # TODO: Migrate to manually managed PodMonitors (enablePodMonitor is deprecated).
            monitoring=ClusterSpecMonitoring(enable_pod_monitor=True),
            bootstrap=None if initdb is None else ClusterSpecBootstrap(initdb=initdb),
            managed=managed,
        ),
    )

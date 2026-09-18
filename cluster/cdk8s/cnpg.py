"""CNPG `Cluster` affinity shared by every generated Postgres: soft anti-affinity off
control-plane nodes, whose etcd is on a rotational HDD that co-located I/O starves
(2026-06-28 outage)."""

from __future__ import annotations

from cnpg_cluster_crds.io.cnpg.postgresql import (
    ClusterSpecAffinityNodeAffinity,
    ClusterSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecution,
    ClusterSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreference,
    ClusterSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreferenceMatchExpressions,
)

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

"""Node-scheduling facts shared across cluster/cdk8s: the OVH zone pinned workloads run
in, the control-plane taint, and the affinity that keeps a Pod tolerating that taint on
workers. `attract_to_zone` and `tolerate_control_plane_taint` apply them to a
cdk8s-plus workload.

Helm values and other `any`-typed fields take the typed structs as they are: jsii passes
a struct there as a plain object keyed by its property names, which for these structs
are the Kubernetes field names.
"""

from __future__ import annotations

from cdk8s_plus_34 import Deployment, Node, NodeLabelQuery, NodeTaintQuery, TaintEffect, Workload, k8s

ZONE_LABEL = "topology.kubernetes.io/zone"
ZONE = "hil-ovh"
# Control-plane nodes also carry this key as a label, which node affinities and selectors match.
CONTROL_PLANE_TAINT_KEY = "node-role.kubernetes.io/control-plane"
CONTROL_PLANE_TOLERATION = k8s.Toleration(key=CONTROL_PLANE_TAINT_KEY, operator="Exists", effect="NoSchedule")
# Keeps a Pod that tolerates the control plane on ordinary workers unless they are full.
PREFER_WORKERS = k8s.Affinity(
    node_affinity=k8s.NodeAffinity(
        preferred_during_scheduling_ignored_during_execution=[
            k8s.PreferredSchedulingTerm(
                weight=100,
                preference=k8s.NodeSelectorTerm(
                    match_expressions=[
                        k8s.NodeSelectorRequirement(key=CONTROL_PLANE_TAINT_KEY, operator="DoesNotExist")
                    ]
                ),
            )
        ]
    )
)


def attract_to_zone(workload: Workload) -> None:
    workload.scheduling.attract(Node.labeled(NodeLabelQuery.is_(ZONE_LABEL, ZONE)))


def tolerate_control_plane_taint(deployment: Deployment) -> None:
    deployment.scheduling.tolerate(
        Node.tainted(NodeTaintQuery.exists(CONTROL_PLANE_TAINT_KEY, effect=TaintEffect.NO_SCHEDULE))
    )

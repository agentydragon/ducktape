"""Node-scheduling facts shared across cluster/cdk8s: the OVH zone and its node selector,
the control-plane taint, and the affinity that keeps a Pod tolerating that taint on
workers. `pod_policy.place` applies a `Placement` to a generated workload.

Helm values and other `any`-typed fields take the typed structs as they are: jsii passes
a struct there as a plain object keyed by its property names, which for these structs
are the Kubernetes field names.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from cdk8s_plus_34 import k8s

ZONE_LABEL = "topology.kubernetes.io/zone"
# The OVH nodes. Pinning to it keeps a workload off the home nodes (wyrm2, optiplex).
HIL_OVH_ZONE = "hil-ovh"
# Consolidate the two home zones and update replica counts with #11643.
HOME_AND_OVH_ZONES = (HIL_OVH_ZONE, "home-lan", "atlas")
REGION_LABEL = "topology.kubernetes.io/region"
# The laptops (iguana, rugged): they join and leave the cluster, so nothing that must keep running
# belongs there.
ROAMING_REGION = "roaming"
# A dict typed read-only: jsii serializes only `dict` instances as maps.
HIL_OVH_NODE_SELECTOR: Mapping[str, str] = {ZONE_LABEL: HIL_OVH_ZONE}
# Control-plane nodes also carry this key as a label, which node affinities and selectors match.
CONTROL_PLANE_TAINT_KEY = "node-role.kubernetes.io/control-plane"
CONTROL_PLANE_TOLERATION = k8s.Toleration(key=CONTROL_PLANE_TAINT_KEY, operator="Exists", effect="NoSchedule")
# The laptops' taint (their NixOS `nodeTaints`): only Pods that may vanish with the laptop tolerate it.
ROAMING_TOLERATION = k8s.Toleration(
    key="node-role.kubernetes.io/roaming", operator="Equal", value="true", effect="NoSchedule"
)
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


@dataclass(frozen=True)
class Placement:
    """The nodes a Pod may run on: those carrying every label in `node_selector`."""

    node_selector: Mapping[str, str]


HIL_OVH = Placement(node_selector=HIL_OVH_NODE_SELECTOR)
OPTIPLEX = Placement(node_selector={"kubernetes.io/hostname": "optiplex"})


def zone_affinity(zones: Sequence[str]) -> k8s.NodeAffinity:
    """Require a node in one of the explicitly supported zones."""
    return k8s.NodeAffinity(
        required_during_scheduling_ignored_during_execution=k8s.NodeSelector(
            node_selector_terms=[
                k8s.NodeSelectorTerm(
                    match_expressions=[k8s.NodeSelectorRequirement(key=ZONE_LABEL, operator="In", values=list(zones))]
                )
            ]
        )
    )


def zone_spread(zones: Sequence[str], labels: Mapping[str, str]) -> k8s.TopologySpreadConstraint:
    """Spread each Deployment revision evenly; a missing zone leaves its replica Pending."""
    return k8s.TopologySpreadConstraint(
        max_skew=1,
        min_domains=len(zones),
        topology_key=ZONE_LABEL,
        when_unsatisfiable="DoNotSchedule",
        label_selector=k8s.LabelSelector(match_labels=dict(labels)),
        match_label_keys=["pod-template-hash"],
    )

"""Node scheduling shared by every Agentplane staging/testing Deployment: all pin to
the same OVH zone, and the two central services (llm-ingress, egress) also tolerate the
control-plane taint to schedule there under pressure.
"""

from __future__ import annotations

from cdk8s_plus_34 import Deployment, Node, NodeLabelQuery, NodeTaintQuery, TaintEffect, Workload

ZONE = "hil-ovh"


def attract_to_zone(workload: Workload) -> None:
    workload.scheduling.attract(Node.labeled(NodeLabelQuery.is_("topology.kubernetes.io/zone", ZONE)))


def tolerate_control_plane_taint(deployment: Deployment) -> None:
    deployment.scheduling.tolerate(
        Node.tainted(NodeTaintQuery.exists("node-role.kubernetes.io/control-plane", effect=TaintEffect.NO_SCHEDULE))
    )

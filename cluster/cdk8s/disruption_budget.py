"""A PodDisruptionBudget over a set of Pods."""

from __future__ import annotations

from cdk8s_plus_34 import k8s
from constructs import Construct

from cluster.cdk8s.service_ref import Pods


def add_disruption_budget(scope: Construct, id: str, *, name: str, pods: Pods) -> k8s.KubePodDisruptionBudget:
    """Allow one voluntary eviction at a time (node drain, descheduler) across these Pods."""
    return k8s.KubePodDisruptionBudget(
        scope,
        id,
        metadata=k8s.ObjectMeta(name=name, namespace=pods.namespace),
        spec=k8s.PodDisruptionBudgetSpec(
            max_unavailable=k8s.IntOrString.from_number(1), selector=k8s.LabelSelector(match_labels=pods.selector)
        ),
    )

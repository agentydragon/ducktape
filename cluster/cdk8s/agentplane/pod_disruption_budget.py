"""Centralize generated PDB construction, which has no fluent cdk8s-plus builder."""

from __future__ import annotations

from cdk8s_plus_34 import k8s
from constructs import Construct


def add_pod_disruption_budget(
    scope: Construct, construct_id: str, *, name: str, namespace: str, min_available: int, selector: dict[str, str]
) -> None:
    k8s.KubePodDisruptionBudget(
        scope,
        construct_id,
        metadata=k8s.ObjectMeta(name=name, namespace=namespace),
        spec=k8s.PodDisruptionBudgetSpec(
            min_available=k8s.IntOrString.from_number(min_available), selector=k8s.LabelSelector(match_labels=selector)
        ),
    )

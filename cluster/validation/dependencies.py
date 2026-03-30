"""Dependency graph construction, cycle detection, and rule checking."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import networkx as nx

from cluster.validation.cluster import ParsedCluster
from cluster.validation.crd_layering import CRD_TO_OPERATOR


@dataclass
class _DependencyRule:
    prerequisite: str
    must_come_before: list[str]
    reason: str


_DEPENDENCY_RULES: list[_DependencyRule] = [
    # CRD-based operator ordering (ExternalSecret->external-secrets-config, ServiceMonitor->monitoring-stack, etc.)
    # is enforced dynamically by validate_operator_dependencies() using CRD_TO_OPERATOR from crd_layering.py.
    # vault -> external-secrets ordering is enforced by CRD layering checks in test_kustomize.py.
    _DependencyRule(
        prerequisite="cert-manager",
        must_come_before=["gateway", "authentik", "gitea", "harbor"],
        reason="TLS certificates required for gateway and applications",
    ),
    _DependencyRule(
        prerequisite="gateway",
        must_come_before=["authentik", "gitea", "harbor", "matrix"],
        reason="Applications need gateway for external access",
    ),
]


class CyclicDependencyError(Exception):
    """Raised when a circular dependency is detected in the Flux kustomization graph."""


def assert_no_cycles(g: nx.DiGraph) -> None:
    """Raise CyclicDependencyError if the graph contains any cycle."""
    cycle = next(nx.simple_cycles(g), None)
    if cycle is not None:
        raise CyclicDependencyError(f"Circular dependency: {' -> '.join([*cycle, cycle[0]])}")


def check_required_dependencies(cluster: ParsedCluster) -> list[str]:
    """Check that critical dependencies are correctly set up."""
    errors = []
    g = cluster.graph

    for rule in _DEPENDENCY_RULES:
        if rule.prerequisite not in cluster.flux_kustomizations:
            raise ValueError(f"Dependency rule references unknown kustomization: {rule.prerequisite}")
        for dependent in rule.must_come_before:
            if dependent not in cluster.flux_kustomizations:
                continue
            if cluster.flux_kustomizations[dependent].suspend:
                continue
            if not nx.has_path(g, dependent, rule.prerequisite):
                errors.append(f"{dependent} should depend on {rule.prerequisite} ({rule.reason})")

    return errors


def validate_operator_dependencies(
    cluster: ParsedCluster, k8s_dir: Path, crd_to_operator: dict[str, str] | None = None
) -> list[str]:
    """Validate that kustomizations using CRD instances transitively depend on the managing operator.

    Uses CRD_TO_OPERATOR from crd_layering.py as the source of truth for which CRD kinds
    require which operator prerequisite.
    """
    if crd_to_operator is None:
        crd_to_operator = CRD_TO_OPERATOR

    flux_resources = cluster.flux_kust_resources(k8s_dir)
    errors = []
    g = cluster.graph
    reported: set[tuple[str, str]] = set()

    for kust_name, resources in flux_resources.items():
        for resource in resources:
            operator = crd_to_operator.get(resource.kind)
            if operator is None:
                continue
            key = (kust_name, operator)
            if key in reported:
                continue
            if operator not in g or not nx.has_path(g, kust_name, operator):
                errors.append(
                    f"{kust_name} uses {resource.kind} resources but doesn't transitively depend on {operator}"
                )
                reported.add(key)

    return errors


def validate_dependencies(cluster: ParsedCluster, k8s_dir: Path) -> list[str]:
    """Validate GitOps dependency graph.

    Raises CyclicDependencyError if any circular dependency is detected.
    """
    if not cluster.flux_kustomizations:
        return ["No Flux kustomizations found"]

    assert_no_cycles(cluster.graph)

    errors = []
    errors.extend(check_required_dependencies(cluster))
    errors.extend(validate_operator_dependencies(cluster, k8s_dir))
    return errors

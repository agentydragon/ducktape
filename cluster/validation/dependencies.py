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
    _DependencyRule(
        prerequisite="cert-manager",
        must_come_before=["gateway", "authentik", "gitea"],
        reason="TLS certificates required for gateway and applications",
    ),
    _DependencyRule(
        prerequisite="gateway",
        must_come_before=["authentik", "gitea", "matrix"],
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


def check_cross_namespace_references(cluster: ParsedCluster) -> list[str]:
    """Fail dependsOn/sourceRef entries that cross namespaces without an explicit namespace.

    Flux resolves a bare entry (no ``namespace:``) in the Kustomization's own
    namespace; if the target lives in a different namespace the reference silently
    misses and the Kustomization stalls with ``DependencyNotReady`` — the
    PR #3759 outage class. Same-namespace bare refs are allowed (the intra-graph
    default-namespace case). References to names absent from this repo
    (cross-repo, e.g. gaffer-private/augur) are skipped — the validator can't see them.
    """
    ks_by_ns: set[tuple[str, str]] = {
        (spec.namespace, name) for name, spec in cluster.flux_kustomizations.items() if spec.namespace
    }
    ks_names = set(cluster.flux_kustomizations)
    sources = cluster.flux_sources
    source_names = {name for _, name in sources}

    errors: list[str] = []
    for name, spec in cluster.flux_kustomizations.items():
        consumer_ns = spec.namespace
        if not consumer_ns:
            continue
        for dep in spec.depends_on:
            target_ns = dep.namespace or consumer_ns
            if (target_ns, dep.name) in ks_by_ns:
                continue
            if dep.name not in ks_names:
                continue  # cross-repo / external — can't validate
            where = sorted({ns for ns, n in ks_by_ns if n == dep.name})
            errors.append(
                f"{name} (ns={consumer_ns}) dependsOn '{dep.name}' resolves to "
                f"ns={target_ns} but no Kustomization exists there; '{dep.name}' "
                f"is in {where}. Add 'namespace:' to the dependsOn entry."
            )
        sr = spec.source_ref
        if sr and sr.name:
            target_ns = sr.namespace or consumer_ns
            if (target_ns, sr.name) not in sources and sr.name in source_names:
                errors.append(
                    f"{name} (ns={consumer_ns}) sourceRef '{sr.name}' resolves to "
                    f"ns={target_ns} but no source exists there; add 'namespace:' "
                    f"to the sourceRef."
                )
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
    errors.extend(check_cross_namespace_references(cluster))
    return errors

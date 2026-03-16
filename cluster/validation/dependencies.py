"""Dependency graph construction, cycle detection, and rule checking."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from cluster.validation.cluster import ParsedCluster
from cluster.validation.flux import FluxKustomization


@dataclass
class _DependencyRule:
    prerequisite: str
    must_come_before: list[str]
    reason: str


_DEPENDENCY_RULES: list[_DependencyRule] = [
    # external-secrets-config ordering is enforced dynamically by validate_external_secrets_dependencies().
    # vault → external-secrets ordering is enforced by CRD layering checks in test_kustomize.py.
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


def build_dependency_graph(flux_kustomizations: dict[str, FluxKustomization]) -> dict[str, list[str]]:
    """Build dependency graph from flux kustomizations."""
    graph: dict[str, list[str]] = defaultdict(list)
    for name, kust in flux_kustomizations.items():
        for dep in kust.spec.depends_on:
            graph[dep.name].append(name)
    return dict(graph)


def find_cycles(graph: dict[str, list[str]], all_nodes: set[str]) -> list[list[str]]:
    """Find cycles in dependency graph using DFS."""
    unvisited, in_progress, done = 0, 1, 2
    color = dict.fromkeys(all_nodes, unvisited)
    cycles = []

    def dfs(node: str, path: list[str]) -> None:
        if color[node] == in_progress:
            cycle_start = path.index(node)
            cycles.append([*path[cycle_start:], node])
            return
        if color[node] == done:
            return

        color[node] = in_progress
        path.append(node)
        for neighbor in graph.get(node, []):
            dfs(neighbor, path)
        path.pop()
        color[node] = done

    for node in all_nodes:
        if color[node] == unvisited:
            dfs(node, [])

    return cycles


def check_required_dependencies(flux_kustomizations: dict[str, FluxKustomization]) -> list[str]:
    """Check that critical dependencies are correctly set up."""
    errors = []

    # Build dependency lookup
    depends_on_map: dict[str, list[str]] = {}
    for name, kust in flux_kustomizations.items():
        depends_on_map[name] = [dep.name for dep in kust.spec.depends_on]

    def has_dependency_path(from_kust: str, to_kust: str, visited: set[str] | None = None) -> bool:
        if visited is None:
            visited = set()
        if to_kust in visited:
            return False
        if from_kust == to_kust:
            return True
        visited.add(to_kust)
        return any(has_dependency_path(from_kust, dep, visited) for dep in depends_on_map.get(to_kust, []))

    for rule in _DEPENDENCY_RULES:
        if rule.prerequisite not in flux_kustomizations:
            raise ValueError(f"Dependency rule references unknown kustomization: {rule.prerequisite}")
        for dependent in rule.must_come_before:
            if dependent not in flux_kustomizations:
                continue
            if rule.prerequisite not in depends_on_map.get(dependent, []):
                has_transitive = any(
                    has_dependency_path(rule.prerequisite, dep) for dep in depends_on_map.get(dependent, [])
                )
                if not has_transitive:
                    errors.append(f"{dependent} should depend on {rule.prerequisite} ({rule.reason})")

    return errors


def validate_external_secrets_dependencies(cluster: ParsedCluster, k8s_dir: Path) -> list[str]:
    """Validate external-secrets specific dependency patterns."""
    errors = []
    services_with_external_secrets: set[str] = set()

    for file_path, resources in cluster.source_resources.items():
        for resource in resources:
            if resource.kind == "ExternalSecret" and resource.api_version.startswith("external-secrets.io"):
                relative = file_path.relative_to(k8s_dir)
                service_name = relative.parts[0] if relative.parts else None
                if service_name:
                    services_with_external_secrets.add(service_name)

    for service in services_with_external_secrets:
        if service in cluster.flux_kustomizations:
            deps = [dep.name for dep in cluster.flux_kustomizations[service].spec.depends_on]
            if "external-secrets-config" not in deps:
                errors.append(f"{service} uses ExternalSecret resources but doesn't depend on external-secrets-config")

    return errors


def validate_dependencies(cluster: ParsedCluster, k8s_dir: Path) -> list[str]:
    """Validate GitOps dependency graph."""
    errors = []

    if not cluster.flux_kustomizations:
        errors.append("No Flux kustomizations found")
        return errors

    graph = build_dependency_graph(cluster.flux_kustomizations)
    all_nodes = (
        set(cluster.flux_kustomizations.keys()) | set().union(*graph.values())
        if graph
        else set(cluster.flux_kustomizations.keys())
    )

    cycles = find_cycles(graph, all_nodes)
    for cycle in cycles:
        errors.append(f"Circular dependency: {' → '.join(cycle)}")

    errors.extend(check_required_dependencies(cluster.flux_kustomizations))
    errors.extend(validate_external_secrets_dependencies(cluster, k8s_dir))

    return errors

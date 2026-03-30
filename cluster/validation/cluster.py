"""Cluster-level aggregation: ParsedCluster model and parse_cluster orchestrator."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx

from cluster.validation.flux import FluxKustomizationSpec, parse_flux_kustomizations
from cluster.validation.k8s import K8sResource, parse_k8s_resource_file
from cluster.validation.kustomize import KustomizeBuildResult, KustomizeFile, parse_kustomize_file

_K8S_SUBPATH = Path("cluster/k8s")


@dataclass
class ParsedCluster:
    """All parsed data from the cluster directory - parsed once, used everywhere."""

    kustomize_files: dict[Path, KustomizeFile] = field(default_factory=dict)
    flux_kustomizations: dict[str, FluxKustomizationSpec] = field(default_factory=dict)
    all_yaml_files: set[Path] = field(default_factory=set)
    source_resources: dict[Path, list[K8sResource]] = field(default_factory=dict)
    build_results: list[KustomizeBuildResult] = field(default_factory=list)

    # Directed graph of Flux kustomization dependencies.
    # Edge A->B means kustomization A depends on B (A must start after B is ready).
    graph: nx.DiGraph = field(init=False)

    def __post_init__(self) -> None:
        g: nx.DiGraph = nx.DiGraph()
        g.add_nodes_from(self.flux_kustomizations)
        for name, spec in self.flux_kustomizations.items():
            for dep in spec.depends_on:
                g.add_edge(name, dep.name)
        self.graph = g

    @property
    def active_flux_kustomizations(self) -> dict[str, FluxKustomizationSpec]:
        """Flux kustomizations that are not suspended."""
        return {name: spec for name, spec in self.flux_kustomizations.items() if not spec.suspend}

    def flux_kust_resources(self, k8s_dir: Path) -> dict[str, list[K8sResource]]:
        """Map active flux kustomization name -> built resources from build_results."""
        build_by_dir: dict[Path, list[K8sResource]] = {
            r.kustomization_path.parent.resolve(): r.resources for r in self.build_results
        }
        result: dict[str, list[K8sResource]] = {}
        for name, spec in self.active_flux_kustomizations.items():
            if (kust_dir := spec.local_dir(k8s_dir)) and kust_dir in build_by_dir:
                result[name] = build_by_dir[kust_dir]
        return result


def parse_cluster(k8s_dir: Path) -> ParsedCluster:
    """Parse all files in the cluster directory once."""
    kustomize_files: dict[Path, KustomizeFile] = {}
    flux_kustomizations: dict[str, FluxKustomizationSpec] = {}
    all_yaml_files: set[Path] = set()
    source_resources: dict[Path, list[K8sResource]] = {}

    for yaml_file in k8s_dir.rglob("*.yaml"):
        # Skip flux-system (auto-generated)
        if "flux-system" in yaml_file.parts:
            continue

        # Skip charts directory (Helm templates)
        if "charts" in yaml_file.parts:
            continue

        # Skip blueprints directory (Authentik-specific YAML with !Env tags, not K8s resources)
        if "blueprints" in yaml_file.parts:
            continue

        all_yaml_files.add(yaml_file.resolve())

        if yaml_file.name == "kustomization.yaml":
            kust = parse_kustomize_file(yaml_file)
            if kust:
                kustomize_files[yaml_file] = kust

        elif yaml_file.name == "flux-kustomization.yaml":
            flux_kustomizations.update(parse_flux_kustomizations(yaml_file))

        else:
            resources = parse_k8s_resource_file(yaml_file)
            if resources:
                source_resources[yaml_file] = resources

    return ParsedCluster(
        kustomize_files=kustomize_files,
        flux_kustomizations=flux_kustomizations,
        all_yaml_files=all_yaml_files,
        source_resources=source_resources,
    )

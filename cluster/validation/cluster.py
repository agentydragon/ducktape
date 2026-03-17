"""Cluster-level aggregation: ParsedCluster model and parse_cluster orchestrator."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx

from cluster.validation.flux import FluxKustomization, parse_flux_kustomization
from cluster.validation.k8s import K8sResource, parse_k8s_resource_file
from cluster.validation.kustomize import KustomizeBuildResult, KustomizeFile, parse_kustomize_file

# Flux kustomization spec.path values are relative to the git repo root.
# k8s_dir sits at this subpath within the repo.
_K8S_SUBPATH = Path("cluster/k8s")


@dataclass
class ParsedCluster:
    """All parsed data from the cluster directory - parsed once, used everywhere."""

    kustomize_files: dict[Path, KustomizeFile] = field(default_factory=dict)
    flux_kustomizations: dict[str, FluxKustomization] = field(default_factory=dict)  # keyed by name
    all_yaml_files: set[Path] = field(default_factory=set)
    source_resources: dict[Path, list[K8sResource]] = field(default_factory=dict)
    build_results: list[KustomizeBuildResult] = field(default_factory=list)

    # Directed graph of Flux kustomization dependencies.
    # Edge A->B means kustomization A depends on B (A must start after B is ready).
    # Computed from flux_kustomizations in __post_init__; do not set manually.
    graph: nx.DiGraph = field(init=False)

    def __post_init__(self) -> None:
        g: nx.DiGraph = nx.DiGraph()
        g.add_nodes_from(self.flux_kustomizations)
        for name, kust in self.flux_kustomizations.items():
            for dep in kust.spec.depends_on:
                g.add_edge(name, dep.name)
        self.graph = g


def parse_cluster(k8s_dir: Path) -> ParsedCluster:
    """Parse all files in the cluster directory once."""
    kustomize_files: dict[Path, KustomizeFile] = {}
    flux_kustomizations: dict[str, FluxKustomization] = {}
    all_yaml_files: set[Path] = set()
    source_resources: dict[Path, list[K8sResource]] = {}

    for yaml_file in k8s_dir.rglob("*.yaml"):
        # Skip flux-system (auto-generated)
        if "flux-system" in yaml_file.parts:
            continue

        # Skip charts directory (Helm templates)
        if "charts" in yaml_file.parts:
            continue

        all_yaml_files.add(yaml_file.resolve())

        if yaml_file.name == "kustomization.yaml":
            kust = parse_kustomize_file(yaml_file)
            if kust:
                kustomize_files[yaml_file] = kust

        elif yaml_file.name == "flux-kustomization.yaml":
            for flux_kust in parse_flux_kustomization(yaml_file):
                flux_kustomizations[flux_kust.name] = flux_kust

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

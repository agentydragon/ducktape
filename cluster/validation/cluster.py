"""Cluster-level aggregation: ParsedCluster model and parse_cluster orchestrator."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from cluster.validation.flux import FluxKustomization, parse_flux_kustomization
from cluster.validation.k8s import K8sResource, parse_k8s_resource_file
from cluster.validation.kustomize import KustomizeBuildResult, KustomizeFile, parse_kustomize_file

# Flux kustomization spec.path values are relative to the git repo root.
# k8s_dir sits at this subpath within the repo.
_K8S_SUBPATH = Path("cluster/k8s")


class ParsedCluster(BaseModel):
    """All parsed data from the cluster directory - parsed once, used everywhere."""

    kustomize_files: dict[Path, KustomizeFile] = {}
    flux_kustomizations: dict[str, FluxKustomization] = {}  # keyed by name
    all_yaml_files: set[Path] = set()
    source_resources: dict[Path, list[K8sResource]] = {}
    build_results: list[KustomizeBuildResult] = []


def parse_cluster(k8s_dir: Path) -> ParsedCluster:
    """Parse all files in the cluster directory once."""
    cluster = ParsedCluster()

    for yaml_file in k8s_dir.rglob("*.yaml"):
        # Skip flux-system (auto-generated)
        if "flux-system" in yaml_file.parts:
            continue

        # Skip charts directory (Helm templates)
        if "charts" in yaml_file.parts:
            continue

        cluster.all_yaml_files.add(yaml_file.resolve())

        if yaml_file.name == "kustomization.yaml":
            kust = parse_kustomize_file(yaml_file)
            if kust:
                cluster.kustomize_files[yaml_file] = kust

        elif yaml_file.name == "flux-kustomization.yaml":
            for flux_kust in parse_flux_kustomization(yaml_file):
                cluster.flux_kustomizations[flux_kust.name] = flux_kust

        else:
            resources = parse_k8s_resource_file(yaml_file)
            if resources:
                cluster.source_resources[yaml_file] = resources

    return cluster

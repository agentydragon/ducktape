"""Health check validation for controller resources (HelmRelease, Terraform)."""

from __future__ import annotations

from pathlib import Path

from cluster.validation.cluster import ParsedCluster
from cluster.validation.k8s import K8sResource
from cluster.validation.kustomize import KustomizeFile

HEALTH_CHECK_REQUIRED_KINDS = ["HelmRelease", "Terraform"]


def kust_deploys_kind(kind: str, kust: KustomizeFile, source_resources: dict[Path, list[K8sResource]]) -> bool:
    return any(
        resource.kind == kind
        for resource_path in kust.resources
        if resource_path in source_resources
        for resource in source_resources[resource_path]
    )


def check_controller_health_checks(cluster: ParsedCluster, k8s_dir: Path, workspace: Path) -> list[str]:
    return [
        f"{name}: deploys a {kind} but has no healthChecks for it. "
        f"Add healthChecks with kind: {kind} to {flux_kust.file_path.relative_to(k8s_dir)}."
        for name, flux_kust in cluster.flux_kustomizations.items()
        if flux_kust.spec.path
        if (kust_dir := (workspace / flux_kust.spec.path.removeprefix("./")))
        if (kust := cluster.kustomize_files.get(kust_dir / "kustomization.yaml"))
        for kind in HEALTH_CHECK_REQUIRED_KINDS
        if kust_deploys_kind(kind, kust, cluster.source_resources)
        if not any(hc.kind == kind for hc in flux_kust.spec.health_checks)
    ]

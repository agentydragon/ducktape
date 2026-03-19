"""Health check validation for controller resources (HelmRelease, Terraform)."""

from __future__ import annotations

from pathlib import Path

from cluster.validation.cluster import ParsedCluster
from cluster.validation.k8s import K8sResource
from cluster.validation.kustomize import KustomizeFile

HEALTH_CHECK_REQUIRED_KINDS = ["HelmRelease", "Terraform"]

_ASYNC_HEALTH_CHECK_KINDS = {
    "HelmRelease",
    "Terraform",
    "Deployment",
    "StatefulSet",
    "DaemonSet",
    "ExternalSecret",
    "ClusterZone",
    "Certificate",
}


def _has_async_health_checks(cluster: ParsedCluster, name: str) -> bool:
    """Check if a kustomization has health checks for async resource kinds."""
    flux_kust = cluster.flux_kustomizations[name]
    return any(hc.kind in _ASYNC_HEALTH_CHECK_KINDS for hc in flux_kust.spec.health_checks)


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


def check_retry_policy(cluster: ParsedCluster, k8s_dir: Path) -> None:
    """Enforce semantically correct retry policies on Flux Kustomizations.

    Async kustomizations (those with async health check kinds or wait: true)
    require retries > 0 AND retryInterval. Retries without retryInterval is
    nearly useless — retries default to the interval cadence (10m).
    """
    errors: list[str] = []
    for name, flux_kust in cluster.flux_kustomizations.items():
        needs_retry = _has_async_health_checks(cluster, name) or flux_kust.spec.wait
        retries = flux_kust.spec.retries
        retry_interval = flux_kust.spec.retry_interval

        if not needs_retry:
            continue

        rel_path = flux_kust.file_path.relative_to(k8s_dir)
        if retries is None or retries <= 0:
            errors.append(
                f"{name}: has async health checks or wait: true but retries={retries}. Set retries > 0 in {rel_path}."
            )
        if not retry_interval:
            errors.append(
                f"{name}: has async health checks or wait: true but no retryInterval. "
                f"Set retryInterval (e.g. 1m) in {rel_path}."
            )

    assert not errors, "Retry policy violations:\n" + "\n".join(f"  {e}" for e in errors)

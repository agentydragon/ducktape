"""Health check validation for controller resources (HelmRelease, Terraform)."""

from __future__ import annotations

from pathlib import Path

from cluster.validation.cluster import ParsedCluster

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
    spec = cluster.flux_kustomizations[name]
    return any(hc.kind in _ASYNC_HEALTH_CHECK_KINDS for hc in spec.health_checks)


def check_controller_health_checks(cluster: ParsedCluster, k8s_dir: Path) -> list[str]:
    """Check that flux kustomizations deploying controller resources have health checks."""
    flux_resources = cluster.flux_kust_resources(k8s_dir)
    return [
        f"{name}: deploys a {kind} but has no healthChecks for it. "
        f"Add healthChecks with kind: {kind} to {spec.path}/flux-kustomization.yaml."
        for name, spec in cluster.flux_kustomizations.items()
        if name in flux_resources
        for kind in HEALTH_CHECK_REQUIRED_KINDS
        if any(r.kind == kind for r in flux_resources[name])
        if not any(hc.kind == kind for hc in spec.health_checks)
    ]


def check_retry_policy(cluster: ParsedCluster) -> None:
    """Enforce retryInterval on async Flux Kustomizations."""
    errors: list[str] = []
    for name, spec in cluster.active_flux_kustomizations.items():
        needs_retry = _has_async_health_checks(cluster, name) or spec.wait
        if not needs_retry:
            continue
        if not spec.retry_interval:
            errors.append(
                f"{name}: has async health checks or wait: true but no retryInterval. "
                f"Set retryInterval (e.g. 1m) in {spec.path}/flux-kustomization.yaml."
            )

    assert not errors, "Retry policy violations:\n" + "\n".join(f"  {e}" for e in errors)

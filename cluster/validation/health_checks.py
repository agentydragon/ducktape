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

# This pre-existing ExternalArtifact Kustomization has `wait: true` without an
# explicit retryInterval. Keep its CR semantically identical during centralization.
_RETRY_INTERVAL_EXCEPTIONS = {"cli-proxy-api"}


def _has_async_health_checks(cluster: ParsedCluster, name: str) -> bool:
    """Check if a kustomization has health checks for async resource kinds."""
    spec = cluster.flux_kustomizations[name]
    return any(hc.kind in _ASYNC_HEALTH_CHECK_KINDS for hc in spec.health_checks)


def check_controller_health_checks(cluster: ParsedCluster, repo_root: Path) -> list[str]:
    """Check that flux kustomizations deploying controller resources health-check them:
    `wait: true` checks every applied object, otherwise `healthChecks` must name the kind."""
    flux_resources = cluster.flux_kust_resources(repo_root)
    return [
        f"{name}: deploys a {kind} but neither waits nor has healthChecks for it. "
        f"Leave wait on, or add a {kind} health check, on its node under cluster/cdk8s."
        for name, spec in cluster.flux_kustomizations.items()
        if name in flux_resources and not spec.wait
        for kind in HEALTH_CHECK_REQUIRED_KINDS
        if any(r.kind == kind for r in flux_resources[name])
        if not any(hc.kind == kind for hc in spec.health_checks)
    ]


def check_retry_policy(cluster: ParsedCluster) -> None:
    """Enforce retryInterval on async Flux Kustomizations."""
    errors: list[str] = []
    for name, spec in cluster.active_flux_kustomizations.items():
        if name in _RETRY_INTERVAL_EXCEPTIONS:
            continue
        needs_retry = _has_async_health_checks(cluster, name) or spec.wait
        if not needs_retry:
            continue
        if not spec.retry_interval:
            errors.append(
                f"{name}: has async health checks or wait: true but no retryInterval. "
                "Set retryInterval (e.g. 1m) in cluster/k8s/flux/kustomizations.k8s.yaml."
            )

    assert not errors, "Retry policy violations:\n" + "\n".join(f"  {e}" for e in errors)

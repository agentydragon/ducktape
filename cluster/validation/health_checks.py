"""Health check validation for controller resources (HelmRelease, Terraform)."""

from __future__ import annotations

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


def check_controller_health_checks(cluster: ParsedCluster) -> list[str]:
    """Check that flux kustomizations deploying controller resources health-check them:
    `wait: true` checks every applied object, otherwise `healthChecks` must name the kind."""
    return [
        f"{name}: deploys a {kind} but neither waits nor has healthChecks for it. "
        f"Leave wait on, or add a {kind} health check, on its node under cluster/cdk8s."
        for name, resources in cluster.flux_kust_resources().items()
        if not (spec := cluster.flux_kustomizations[name]).wait
        for kind in HEALTH_CHECK_REQUIRED_KINDS
        if any(r.kind == kind for r in resources)
        if not any(hc.kind == kind for hc in spec.health_checks)
    ]


def check_retry_policy(cluster: ParsedCluster) -> list[str]:
    """Enforce retryInterval on async Flux Kustomizations."""
    return [
        f"{name}: has async health checks or wait: true but no retryInterval. "
        "Set retry_interval (e.g. 1m) on its node under cluster/cdk8s."
        for name, spec in cluster.active_flux_kustomizations.items()
        if name not in _RETRY_INTERVAL_EXCEPTIONS
        and not spec.retry_interval
        and (spec.wait or any(hc.kind in _ASYNC_HEALTH_CHECK_KINDS for hc in spec.health_checks))
    ]

"""Health check validation for controller resources (HelmRelease, Terraform)."""

from __future__ import annotations

from cluster.validation.cluster import ParsedCluster
from cluster.validation.k8s import FluxControllerResource

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
    """Flux waits for every applied object with wait: true, or just its named health checks."""
    errors = []
    for owner, resources in cluster.flux_kust_resources().items():
        spec = cluster.flux_kustomizations[owner]
        if spec.wait:
            continue
        checked = {(h.api_version.rpartition("/")[0], h.kind, h.namespace, h.name) for h in spec.health_checks}
        for resource in resources:
            if not isinstance(resource, FluxControllerResource) or resource.suspended:
                continue
            group = resource.api_version.rpartition("/")[0]
            if (group, resource.kind, resource.namespace, resource.name) not in checked:
                errors.append(f"{owner}: missing health check for {resource.kind} {resource.namespace}/{resource.name}")
    return errors


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

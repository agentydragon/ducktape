"""Admission-only sizing for the bootstrapped Flux controllers."""

from cdk8s import App, Chart

from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.providers.vpa.policy import requests_only_vpa

NAME = "flux-system-vpa"
OUTPUT_DIR = f"{GENERATED_ROOT}/{NAME}"

_DEPLOYMENTS = (
    "helm-controller",
    "image-automation-controller",
    "image-reflector-controller",
    "kustomize-controller",
    "notification-controller",
    "source-controller",
    "source-watcher",
)


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    for deployment in _DEPLOYMENTS:
        requests_only_vpa(chart, deployment, namespace="flux-system", container_name="manager", update_mode="Initial")
    return chart

"""Admission-only sizing for the bootstrapped Flux controllers."""

from cdk8s import ApiObjectMetadata, App, Chart
from vpa_crds.io.k8s.autoscaling import VerticalPodAutoscaler, VerticalPodAutoscalerSpecTargetRef

from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.providers.vpa.policy import requests_only_spec

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
        VerticalPodAutoscaler(
            chart,
            deployment,
            metadata=ApiObjectMetadata(name=deployment, namespace="flux-system"),
            spec=requests_only_spec(
                VerticalPodAutoscalerSpecTargetRef(api_version="apps/v1", kind="Deployment", name=deployment),
                container_name="manager",
            ),
        )
    return chart

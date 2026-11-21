"""Admission-only sizing for the KubeVirt and CDI control-plane Deployments."""

from cdk8s import ApiObjectMetadata, App, Chart
from vpa_crds.io.k8s.autoscaling import VerticalPodAutoscaler, VerticalPodAutoscalerSpecTargetRef

from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.providers.vpa.policy import requests_only_spec

NAME = "kubevirt-cdi-vpa"
OUTPUT_DIR = f"{GENERATED_ROOT}/kubevirt/vpa"

_DEPLOYMENTS = {
    "kubevirt": ("virt-api", "virt-controller", "virt-operator"),
    "cdi": ("cdi-apiserver", "cdi-deployment", "cdi-uploadproxy"),
}


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    for namespace, deployments in _DEPLOYMENTS.items():
        for deployment in deployments:
            VerticalPodAutoscaler(
                chart,
                deployment,
                metadata=ApiObjectMetadata(name=deployment, namespace=namespace),
                spec=requests_only_spec(
                    VerticalPodAutoscalerSpecTargetRef(api_version="apps/v1", kind="Deployment", name=deployment),
                    container_name=deployment,
                ),
            )
    return chart

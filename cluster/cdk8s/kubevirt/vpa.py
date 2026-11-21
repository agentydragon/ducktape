"""Admission-only sizing for the KubeVirt and CDI controllers."""

from cdk8s import ApiObjectMetadata, App, Chart
from vpa_crds.io.k8s.autoscaling import VerticalPodAutoscaler, VerticalPodAutoscalerSpecTargetRef

from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.providers.vpa.policy import initial_requests_spec

NAME = "kubevirt-cdi-vpa"
OUTPUT_DIR = f"{GENERATED_ROOT}/kubevirt/vpa"

_TARGETS = (
    ("kubevirt", "Deployment", "virt-api"),
    ("kubevirt", "Deployment", "virt-controller"),
    ("kubevirt", "Deployment", "virt-operator"),
    ("cdi", "Deployment", "cdi-apiserver"),
    ("cdi", "Deployment", "cdi-deployment"),
    ("cdi", "Deployment", "cdi-uploadproxy"),
    ("cdi", "Deployment", "cdi-operator"),
    ("kubevirt", "DaemonSet", "virt-handler"),
)


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    for namespace, kind, name in _TARGETS:
        VerticalPodAutoscaler(
            chart,
            name,
            metadata=ApiObjectMetadata(name=name, namespace=namespace),
            spec=initial_requests_spec(
                VerticalPodAutoscalerSpecTargetRef(api_version="apps/v1", kind=kind, name=name), container_name=name
            ),
        )
    return chart

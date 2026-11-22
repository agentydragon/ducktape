"""Resource request sizing for the KubeVirt and CDI controllers."""

from cdk8s import App, Chart

from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.providers.vpa.policy import requests_only_vpa

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
        requests_only_vpa(
            chart,
            name,
            namespace=namespace,
            target_kind=kind,
            container_name=name,
            # The pinned operator serves admission on both replicas. Its hostname
            # anti-affinity and PDB protect one endpoint during a VPA eviction.
            update_mode="Recreate" if name == "virt-operator" else "Initial",
        )
    return chart

"""Node Feature Discovery, which labels nodes with their PCI network and display devices
(the GPU labels the NVIDIA device plugin schedules on)."""

from __future__ import annotations

from cdk8s import App, Chart

from cluster.cdk8s import namespaces
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release, https_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa, VpaProfile
from cluster.cdk8s.vpa_policy import ContainerPolicy, ControlledValues

NAME = "node-feature-discovery"
NAMESPACE = "node-feature-discovery"
OUTPUT_DIR = f"{GENERATED_ROOT}/node-feature-discovery"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(
        chart,
        "namespace",
        name=NAMESPACE,
        vpa=VpaProfile(
            Vpa.INITIAL,
            (
                *(
                    ContainerPolicy(
                        container_name=name,
                        controlled_values=ControlledValues.REQUESTS_ONLY,
                        controlled_resources=("cpu", "memory"),
                    )
                    for name in ("gc", "master", "worker")
                ),
                ContainerPolicy(controlled_values=ControlledValues.REQUESTS_ONLY, mode="Off"),
            ),
        ),
        labels={"pod-security.kubernetes.io/enforce": "privileged"},
    )
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=https_helm_repository(
            chart, "nfd", NAMESPACE, url="https://kubernetes-sigs.github.io/node-feature-discovery/charts"
        ),
        chart=NAME,
        version="0.19.0",
        interval="15m",
        install=RETRY_FAILED_INSTALL,
        values={
            "worker": {
                "tolerations": [{"effect": "NoSchedule", "operator": "Exists"}],
                "config": {"sources": {"pci": {"deviceClassWhitelist": ["02", "03"], "deviceLabelFields": ["vendor"]}}},
            }
        },
    )
    return chart

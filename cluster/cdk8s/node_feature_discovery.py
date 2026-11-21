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
                ContainerPolicy(
                    container_name="gc",
                    controlled_values=ControlledValues.REQUESTS_ONLY,
                    controlled_resources=("cpu", "memory"),
                    min_allowed={"cpu": "10m", "memory": "128Mi"},
                    max_allowed={"cpu": "25m", "memory": "256Mi"},
                ),
                ContainerPolicy(
                    container_name="master",
                    controlled_values=ControlledValues.REQUESTS_ONLY,
                    controlled_resources=("cpu", "memory"),
                    min_allowed={"cpu": "100m", "memory": "128Mi"},
                    max_allowed={"cpu": "150m", "memory": "256Mi"},
                ),
                # The worker's 30-day working-set peak was ~68 MiB, above its 64 MiB template request.
                # The worker can burst beyond its scheduling request during discovery.
                ContainerPolicy(
                    container_name="worker",
                    controlled_values=ControlledValues.REQUESTS_ONLY,
                    controlled_resources=("cpu", "memory"),
                    min_allowed={"cpu": "5m", "memory": "96Mi"},
                    max_allowed={"cpu": "100m", "memory": "128Mi"},
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

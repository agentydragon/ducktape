"""Node Feature Discovery, which labels nodes with their PCI network and display devices
(the GPU labels the NVIDIA device plugin schedules on)."""

from __future__ import annotations

from cdk8s import App, Chart
from flux_helm.io.fluxcd.toolkit.helm import HelmReleaseSpecUpgrade

from cluster.cdk8s import namespaces
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release, https_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa

NAME = "node-feature-discovery"
NAMESPACE = "node-feature-discovery"
OUTPUT_DIR = f"{GENERATED_ROOT}/node-feature-discovery"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(
        chart,
        "namespace",
        name=NAMESPACE,
        vpa=Vpa.RECOMMEND,
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
        # The worker DaemonSet runs on the roaming laptops, which are often offline: waiting
        # for every pod times the upgrade out, as for promtail (cluster/cdk8s/monitoring/loki.py).
        upgrade=HelmReleaseSpecUpgrade(disable_wait=True),
        values={
            "worker": {
                "tolerations": [{"effect": "NoSchedule", "operator": "Exists"}],
                # Must exceed the roaming-node count, as for promtail (cluster/cdk8s/monitoring/loki.py);
                # enforced by //cluster/cdk8s/monitoring:test_roaming_daemonset_capacity.
                "updateStrategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 3}},
                "config": {"sources": {"pci": {"deviceClassWhitelist": ["02", "03"], "deviceLabelFields": ["vendor"]}}},
            }
        },
    )
    return chart


def node_feature_discovery(chart: Chart, directory: RenderedDirectory) -> Kustomization:
    return flux_kustomization(chart, NAME, directory, timeout="5m")

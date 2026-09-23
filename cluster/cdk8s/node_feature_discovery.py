"""Node Feature Discovery, which labels nodes with their PCI network and display devices
(the GPU labels the NVIDIA device plugin schedules on)."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_helm.io.fluxcd.toolkit.helm import HelmReleaseSpecUpgrade
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release
from cluster.cdk8s.metadata import metadata

NAME = "node-feature-discovery"
NAMESPACE = "node-feature-discovery"
OUTPUT_DIR = "cluster/k8s/node-feature-discovery"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=NAMESPACE,
            labels={"pod-security.kubernetes.io/enforce": "privileged", "rbac.ducktape.io/agent-readable-logs": "true"},
        ),
    )
    repository = HelmRepository(
        chart,
        "repository",
        metadata=metadata("nfd", NAMESPACE),
        spec=HelmRepositorySpec(interval="24h", url="https://kubernetes-sigs.github.io/node-feature-discovery/charts"),
    )
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=repository,
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
                # enforced by //cluster/validation:test_roaming_daemonset_capacity.
                "updateStrategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 3}},
                "config": {"sources": {"pci": {"deviceClassWhitelist": ["02", "03"], "deviceLabelFields": ["vendor"]}}},
            }
        },
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def node_feature_discovery(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    return flux_kustomization(chart, NAME, artifact, timeout="5m")

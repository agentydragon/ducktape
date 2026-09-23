"""Node Feature Discovery, which labels nodes with their PCI network and display devices
(the GPU labels the NVIDIA device plugin schedules on)."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmRelease,
    HelmReleaseSpec,
    HelmReleaseSpecChart,
    HelmReleaseSpecChartSpec,
    HelmReleaseSpecChartSpecSourceRef,
    HelmReleaseSpecChartSpecSourceRefKind,
    HelmReleaseSpecInstall,
    HelmReleaseSpecInstallRemediation,
    HelmReleaseSpecUpgrade,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization, kustomize_kustomization
from cluster.cdk8s.generation import write_charts, write_yaml
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
    HelmRelease(
        chart,
        "release",
        metadata=metadata(NAME, NAMESPACE),
        spec=HelmReleaseSpec(
            interval="15m",
            install=HelmReleaseSpecInstall(remediation=HelmReleaseSpecInstallRemediation(retries=3)),
            # The worker DaemonSet runs on the roaming laptops, which are often offline: waiting
            # for every pod times the upgrade out, as for promtail (cluster/cdk8s/monitoring/loki.py).
            upgrade=HelmReleaseSpecUpgrade(disable_wait=True),
            chart=HelmReleaseSpecChart(
                spec=HelmReleaseSpecChartSpec(
                    chart=NAME,
                    version="0.19.0",
                    source_ref=HelmReleaseSpecChartSpecSourceRef(
                        kind=HelmReleaseSpecChartSpecSourceRefKind.HELM_REPOSITORY,
                        name=repository.name,
                        namespace=repository.metadata.namespace,
                    ),
                )
            ),
            values={
                "worker": {
                    "tolerations": [{"effect": "NoSchedule", "operator": "Exists"}],
                    # Must exceed the roaming-node count, as for promtail (cluster/cdk8s/monitoring/loki.py);
                    # enforced by //cluster/validation:test_roaming_daemonset_capacity.
                    "updateStrategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 3}},
                    "config": {
                        "sources": {"pci": {"deviceClassWhitelist": ["02", "03"], "deviceLabelFields": ["vendor"]}}
                    },
                }
            },
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(root / OUTPUT_DIR / "kustomization.yaml", kustomize_kustomization(resources=[f"{NAME}.k8s.yaml"]))


def node_feature_discovery(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=artifact_source_ref(artifact),
            path=artifact_path(artifact),
            prune=True,
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name=NAME, namespace=NAMESPACE
                )
            ],
        ),
    )

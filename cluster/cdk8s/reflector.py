"""Emberstack Reflector, which mirrors annotated Secrets and ConfigMaps across namespaces."""

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
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

NAME = "reflector"
NAMESPACE = "reflector-system"
OUTPUT_DIR = "cluster/k8s/reflector"
_VERSION = "10.0.65"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeNamespace(chart, "namespace", metadata=k8s.ObjectMeta(name=NAMESPACE))
    repository = HelmRepository(
        chart,
        "repository",
        metadata=metadata("emberstack", NAMESPACE),
        spec=HelmRepositorySpec(interval="24h", url="https://emberstack.github.io/helm-charts"),
    )
    HelmRelease(
        chart,
        "release",
        metadata=metadata(NAME, NAMESPACE),
        spec=HelmReleaseSpec(
            interval="15m",
            install=HelmReleaseSpecInstall(remediation=HelmReleaseSpecInstallRemediation(retries=3)),
            chart=HelmReleaseSpecChart(
                spec=HelmReleaseSpecChartSpec(
                    chart="reflector",
                    version=_VERSION,
                    source_ref=HelmReleaseSpecChartSpecSourceRef(
                        kind=HelmReleaseSpecChartSpecSourceRefKind.HELM_REPOSITORY,
                        name=repository.name,
                        namespace=repository.metadata.namespace,
                    ),
                )
            ),
            values={
                "nameOverride": NAME,
                "fullnameOverride": NAME,
                "replicaCount": 1,
                "image": {
                    "repository": "emberstack/kubernetes-reflector",
                    "tag": _VERSION,
                    "pullPolicy": "IfNotPresent",
                },
                "configuration": {"logging": {"minimumLevel": "Information"}, "watcher": {"timeout": 300}},
                "rbac": {"enabled": True},
                "serviceAccount": {"create": True, "name": NAME},
                "resources": {
                    "requests": {"memory": "128Mi", "cpu": "100m"},
                    "limits": {"memory": "256Mi", "cpu": "200m"},
                },
                "nodeSelector": {},
                "tolerations": [],
                "affinity": {},
            },
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def reflector(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
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
            # Health check ensures Reflector is ready before secrets can be mirrored
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name=NAME, namespace=NAMESPACE
                )
            ],
        ),
    )

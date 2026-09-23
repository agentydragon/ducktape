"""KEDA, the autoscaler that scales haku-ci's runners, in its own namespace."""

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
    HelmReleaseSpecInstallCrds,
    HelmReleaseSpecInstallRemediation,
    HelmReleaseSpecUpgrade,
    HelmReleaseSpecUpgradeCrds,
    HelmReleaseSpecUpgradeRemediation,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

NAME = "keda"
NAMESPACE = "keda"
OUTPUT_DIR = "cluster/k8s/keda"


def _resources(*, cpu_request: str, memory_request: str, cpu_limit: str, memory_limit: str) -> dict[str, object]:
    return {
        "requests": {"cpu": cpu_request, "memory": memory_request},
        "limits": {"cpu": cpu_limit, "memory": memory_limit},
    }


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeNamespace(chart, "namespace", metadata=k8s.ObjectMeta(name=NAMESPACE))
    repository = HelmRepository(
        chart,
        "repository",
        metadata=metadata(NAME, NAMESPACE),
        spec=HelmRepositorySpec(interval="24h", url="https://kedacore.github.io/charts"),
    )
    HelmRelease(
        chart,
        "release",
        metadata=metadata(NAME, NAMESPACE),
        spec=HelmReleaseSpec(
            interval="15m",
            install=HelmReleaseSpecInstall(
                crds=HelmReleaseSpecInstallCrds.CREATE, remediation=HelmReleaseSpecInstallRemediation(retries=3)
            ),
            upgrade=HelmReleaseSpecUpgrade(
                crds=HelmReleaseSpecUpgradeCrds.CREATE_REPLACE, remediation=HelmReleaseSpecUpgradeRemediation(retries=3)
            ),
            chart=HelmReleaseSpecChart(
                spec=HelmReleaseSpecChartSpec(
                    chart="keda",
                    # 2.20.2 reports an empty Forgejo queue as inactive, allowing haku-ci
                    # to scale to zero.
                    version="2.20.2",
                    source_ref=HelmReleaseSpecChartSpecSourceRef(
                        kind=HelmReleaseSpecChartSpecSourceRefKind.HELM_REPOSITORY,
                        name=repository.name,
                        namespace=repository.metadata.namespace,
                    ),
                    interval="12h",
                )
            ),
            values={
                # KEDA has cluster-scoped CRDs and admission plumbing, but its operator only
                # watches the one namespace whose runners it is allowed to scale.
                "watchNamespace": "haku-ci",
                "nodeSelector": {"topology.kubernetes.io/region": "hil"},
                "resources": {
                    "operator": _resources(
                        cpu_request="50m", memory_request="128Mi", cpu_limit="250m", memory_limit="256Mi"
                    ),
                    "metricServer": _resources(
                        cpu_request="50m", memory_request="128Mi", cpu_limit="250m", memory_limit="256Mi"
                    ),
                    "webhooks": _resources(
                        cpu_request="25m", memory_request="64Mi", cpu_limit="100m", memory_limit="128Mi"
                    ),
                },
            },
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def keda(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, kyverno: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        artifact,
        timeout="5m",
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name=NAME, namespace=NAMESPACE
            )
        ],
        depends_on=[flux_kustomization_depends_on(kyverno)],
    )

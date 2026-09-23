"""The Vertical Pod Autoscaler in kube-system, from Fairwinds' chart repository (which the
goldilocks directory's release also uses)."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
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
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

NAME = "vpa"
NAMESPACE = "kube-system"
OUTPUT_DIR = "cluster/k8s/vpa"


def _component(*, memory_request: str, memory_limit: str, **extra: object) -> dict[str, object]:
    return {
        "enabled": True,
        **extra,
        "resources": {
            "requests": {"cpu": "25m", "memory": memory_request},
            "limits": {"cpu": "200m", "memory": memory_limit},
        },
    }


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    repository = HelmRepository(
        chart,
        "repository",
        metadata=metadata("fairwinds-stable", "flux-system"),
        spec=HelmRepositorySpec(interval="24h", url="https://charts.fairwinds.com/stable"),
    )
    HelmRelease(
        chart,
        "release",
        metadata=metadata(NAME, NAMESPACE),
        spec=HelmReleaseSpec(
            interval="30m",
            install=HelmReleaseSpecInstall(remediation=HelmReleaseSpecInstallRemediation(retries=3)),
            chart=HelmReleaseSpecChart(
                spec=HelmReleaseSpecChartSpec(
                    chart=NAME,
                    # renovate: datasource=helm depName=vpa registryUrl=https://charts.fairwinds.com/stable
                    version="5.0.1",
                    source_ref=HelmReleaseSpecChartSpecSourceRef(
                        kind=HelmReleaseSpecChartSpecSourceRefKind.HELM_REPOSITORY,
                        name=repository.name,
                        namespace=repository.metadata.namespace,
                    ),
                    interval="12h",
                )
            ),
            values={
                "recommender": _component(
                    memory_request="256Mi",
                    memory_limit="512Mi",
                    # 15m interval reduces control-plane system-disk writes.
                    extraArgs={"recommender-interval": "15m"},
                ),
                "updater": _component(memory_request="128Mi", memory_limit="256Mi"),
                "admissionController": _component(memory_request="128Mi", memory_limit="256Mi"),
            },
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def vpa(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, kyverno: Kustomization, metrics_server: Kustomization
) -> Kustomization:
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
            depends_on=flux_kustomization_depends_on_many(kyverno, metrics_server),
        ),
    )

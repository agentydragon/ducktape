"""The Vertical Pod Autoscaler in kube-system, from Fairwinds' chart repository (which the
goldilocks directory's release also uses)."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release, helm_repository_source_ref
from cluster.cdk8s.metadata import metadata

NAME = "vpa"
NAMESPACE = "kube-system"
OUTPUT_DIR = "cluster/k8s/vpa"
_REPOSITORY_NAME = "fairwinds-stable"
_REPOSITORY_NAMESPACE = "flux-system"
# Goldilocks installs from this repository too, from its own chart.
REPOSITORY_SOURCE_REF = helm_repository_source_ref(_REPOSITORY_NAME, _REPOSITORY_NAMESPACE)


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
        metadata=metadata(_REPOSITORY_NAME, _REPOSITORY_NAMESPACE),
        spec=HelmRepositorySpec(interval="24h", url="https://charts.fairwinds.com/stable"),
    )
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=repository,
        chart=NAME,
        version="5.0.1",
        interval="30m",
        chart_interval="12h",
        install=RETRY_FAILED_INSTALL,
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
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def vpa(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, kyverno: Kustomization, metrics_server: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart, NAME, artifact, timeout="5m", depends_on=flux_kustomization_depends_on_many(kyverno, metrics_server)
    )

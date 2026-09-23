"""Flux Kustomizations for the cluster/k8s/keda slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on


def keda(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, kyverno: Kustomization) -> Kustomization:
    name = "keda"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="5m",
            path=artifact_path(artifact),
            prune=True,
            wait=True,
            source_ref=artifact_source_ref(artifact),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name="keda", namespace="keda"
                )
            ],
            depends_on=[flux_kustomization_depends_on(kyverno)],
        ),
    )

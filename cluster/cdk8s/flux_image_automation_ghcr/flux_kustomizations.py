"""Flux Kustomizations for the cluster/k8s/flux-image-automation-ghcr slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization


def flux_image_automation_ghcr(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    name = "flux-image-automation-ghcr"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m", path=artifact_path(artifact), prune=True, source_ref=artifact_source_ref(artifact)
        ),
    )

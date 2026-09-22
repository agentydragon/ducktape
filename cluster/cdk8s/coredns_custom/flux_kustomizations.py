"""Flux Kustomizations for the cluster/k8s/coredns-custom slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization


def coredns_custom(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    name = "coredns-custom"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m0s",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            timeout="5m",
            wait=True,
        ),
    )

"""Flux Kustomizations for the cluster/k8s/user-agentydragon slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization


def user_agentydragon(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    name = "user-agentydragon"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            timeout="10m",
            decryption=SOPS_DECRYPTION,
        ),
    )

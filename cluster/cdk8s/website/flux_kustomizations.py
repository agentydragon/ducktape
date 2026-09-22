"""Flux Kustomizations for the cluster/k8s/website slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on


def website(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, gateway: Kustomization) -> Kustomization:
    name = "website"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=artifact_source_ref(artifact),
            path=artifact_path(artifact),
            prune=True,
            wait=True,
            depends_on=[
                # TLS is owned by the shared Gateway; Website only supplies an HTTPRoute.
                flux_kustomization_depends_on(gateway)
            ],
        ),
    )

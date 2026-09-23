"""Flux Kustomizations for the cluster/k8s/gatus slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def gatus(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, cnpg: Kustomization, monitoring_crds: Kustomization
) -> Kustomization:
    name = "gatus"
    return flux_kustomization(
        chart,
        name,
        artifact,
        timeout="10m",
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        depends_on=flux_kustomization_depends_on_many(cnpg, monitoring_crds),
    )

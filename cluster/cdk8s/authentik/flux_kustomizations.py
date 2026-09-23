"""Flux Kustomizations for the cluster/k8s/authentik slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def authentik(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, cnpg: Kustomization, monitoring_crds: Kustomization
) -> Kustomization:
    name = "authentik"
    return flux_kustomization(
        chart,
        name,
        artifact,
        interval="10m0s",
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        timeout="10m0s",
        decryption=SOPS_DECRYPTION,
        depends_on=flux_kustomization_depends_on_many(cnpg, monitoring_crds),
    )

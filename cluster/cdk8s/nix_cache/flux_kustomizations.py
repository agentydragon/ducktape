"""Flux Kustomizations for the cluster/k8s/nix-cache slice."""

from __future__ import annotations

from cdk8s import Chart
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def nix_cache(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    cnpg: Kustomization,
    external_secrets_operator: Kustomization,
    seaweedfs_operator: Kustomization,
) -> Kustomization:
    name = "nix-cache"
    return flux_kustomization(
        chart,
        name,
        artifact,
        timeout="5m",
        decryption=SOPS_DECRYPTION,
        depends_on=flux_kustomization_depends_on_many(cnpg, external_secrets_operator, seaweedfs_operator),
    )

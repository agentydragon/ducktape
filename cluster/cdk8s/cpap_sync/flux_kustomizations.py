"""Flux Kustomizations for the cluster/k8s/cpap-sync slice."""

from __future__ import annotations

from cdk8s import Chart
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def cpap_sync(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    external_secrets_config: Kustomization,
    kubevirt: Kustomization,
    forgejo_images: Kustomization,
) -> Kustomization:
    name = "cpap-sync"
    return flux_kustomization(
        chart,
        name,
        artifact,
        decryption=SOPS_DECRYPTION,
        timeout="30m",
        depends_on=flux_kustomization_depends_on_many(external_secrets_config, kubevirt, forgejo_images),
    )

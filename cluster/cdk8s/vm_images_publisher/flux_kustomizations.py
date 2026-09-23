"""Flux Kustomizations for the cluster/k8s/vm-images-publisher slice."""

from __future__ import annotations

from cdk8s import Chart
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on


def vm_images_publisher(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, seaweedfs_cluster: Kustomization
) -> Kustomization:
    name = "vm-images-publisher"
    return flux_kustomization(
        chart,
        name,
        # attic-reader-netrc.sops.yaml is SOPS-encrypted; without this, Flux applies
        # the ciphertext literally and the publisher's attic auth (netrc) is garbage.
        artifact,
        timeout="5m",
        decryption=SOPS_DECRYPTION,
        depends_on=[flux_kustomization_depends_on(seaweedfs_cluster)],
    )

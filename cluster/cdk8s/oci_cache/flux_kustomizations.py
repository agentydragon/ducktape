"""Flux Kustomizations for the cluster/k8s/oci-cache slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def oci_cache(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    valkey: Kustomization,
    monitoring_crds: Kustomization,
    seaweedfs_operator: Kustomization,
) -> Kustomization:
    name = "oci-cache"
    return flux_kustomization(
        chart,
        name,
        artifact,
        timeout="5m",
        # Do not delete the cache namespace if this Flux owner is removed later.
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        decryption=SOPS_DECRYPTION,
        depends_on=flux_kustomization_depends_on_many(
            # Namespace, app, and ServiceMonitor are managed together here.
            valkey,
            monitoring_crds,
            # The registry-cache Bucket, S3Identity, S3Credentials and grant CRDs.
            seaweedfs_operator,
        ),
        description="Zot OCI pull-through cache and its namespace-local monitoring.",
    )

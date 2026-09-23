"""Flux Kustomizations for the cluster/k8s/seaweedfs slice."""

from __future__ import annotations

from cdk8s import Chart
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def seaweedfs_cluster(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    seaweedfs_operator: Kustomization,
    seaweedfs_secrets: Kustomization,
    seaweedfs_filer_db: Kustomization,
    local_path_provisioner: Kustomization,
) -> Kustomization:
    name = "seaweedfs-cluster"
    return flux_kustomization(
        chart,
        name,
        artifact,
        retry_interval=None,
        suspend=False,
        depends_on=flux_kustomization_depends_on_many(
            seaweedfs_operator,
            seaweedfs_secrets,
            # Filer is configured with postgres2 backend.
            seaweedfs_filer_db,
            local_path_provisioner,
        ),
        wait=False,
        timeout="5m",
    )

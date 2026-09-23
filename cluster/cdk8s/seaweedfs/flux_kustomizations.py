"""Flux Kustomizations for the cluster/k8s/seaweedfs slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
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
        spec=KustomizationSpec(
            suspend=False,
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            depends_on=flux_kustomization_depends_on_many(
                seaweedfs_operator,
                seaweedfs_secrets,
                # Filer is configured with postgres2 backend.
                seaweedfs_filer_db,
                local_path_provisioner,
            ),
            wait=False,
            timeout="5m",
        ),
    )

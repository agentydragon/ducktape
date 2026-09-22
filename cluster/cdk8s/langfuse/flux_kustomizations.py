"""Flux Kustomizations for the cluster/k8s/langfuse slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecHealthChecks,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def langfuse(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    cnpg: Kustomization,
    valkey: Kustomization,
    seaweedfs_operator: Kustomization,
) -> Kustomization:
    name = "langfuse"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            suspend=False,
            retry_interval="1m",
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            decryption=SOPS_DECRYPTION,
            source_ref=artifact_source_ref(artifact),
            timeout="20m",
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name="langfuse", namespace="langfuse"
                )
            ],
            depends_on=flux_kustomization_depends_on_many(cnpg, valkey, seaweedfs_operator),
        ),
    )

"""Flux Kustomizations for the cluster/k8s/forgejo slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def forgejo(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    cnpg: Kustomization,
    external_secrets_operator: Kustomization,
    seaweedfs_operator: Kustomization,
    monitoring_crds: Kustomization,
) -> Kustomization:
    name = "forgejo"
    return flux_kustomization(
        chart,
        name,
        artifact,
        timeout="5m",
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="seaweed.seaweedfs.com/v1", kind="Bucket", name="forgejo", namespace="forgejo"
            ),
            KustomizationSpecHealthChecks(
                api_version="seaweed.seaweedfs.com/v1", kind="S3Credentials", name="forgejo", namespace="forgejo"
            ),
            KustomizationSpecHealthChecks(
                api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name="forgejo", namespace="forgejo"
            ),
        ],
        decryption=SOPS_DECRYPTION,
        # Admission needs these CRDs; runtime dependencies converge independently.
        depends_on=flux_kustomization_depends_on_many(
            cnpg, external_secrets_operator, seaweedfs_operator, monitoring_crds
        ),
    )

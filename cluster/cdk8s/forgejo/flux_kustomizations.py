"""Flux Kustomizations for the cluster/k8s/forgejo slice."""

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
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=artifact_source_ref(artifact),
            path=artifact_path(artifact),
            prune=True,
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            wait=True,
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
        ),
    )


def budget_namespace(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    name = "budget-namespace"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="1h",
            path=artifact_path(artifact),
            prune=False,
            source_ref=artifact_source_ref(artifact),
            timeout="1m",
        ),
    )


def forgejo_cache(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, valkey: Kustomization, local_path_provisioner: Kustomization
) -> Kustomization:
    name = "forgejo-cache"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=artifact_source_ref(artifact),
            path=artifact_path(artifact),
            prune=True,
            wait=True,
            # Retry until the forgejo aggregate creates the Namespace. Waiting for
            # Forgejo readiness would deadlock its cache-dependent startup.
            depends_on=flux_kustomization_depends_on_many(valkey, local_path_provisioner),
        ),
    )

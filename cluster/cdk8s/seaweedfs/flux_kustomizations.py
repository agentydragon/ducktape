"""Flux Kustomizations for the cluster/k8s/seaweedfs slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on_many


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


def seaweedfs_monitoring(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    seaweedfs_cluster: Kustomization,
    monitoring_crds: Kustomization,
) -> Kustomization:
    name = "seaweedfs-monitoring"
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
                seaweedfs_cluster,
                # PrometheusRule
                monitoring_crds,
            ),
        ),
    )


def seaweedfs_public_s3(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    seaweedfs_external_credentials: Kustomization,
    seaweedfs_drivefs_artifacts_bucket: Kustomization,
    vm_images_publisher: Kustomization,
    seaweedfs_secrets: Kustomization,
    seaweedfs_cluster: Kustomization,
    gateway: Kustomization,
) -> Kustomization:
    name = "seaweedfs-public-s3"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path=artifact_path(artifact),
            prune=True,
            # Gate on Bucket CRs managed in this repo that the public identities target.
            # Claude and DriveFS identities authenticate through native IAM; static
            # gateway configuration now contains only the credential-free anonymous read.
            source_ref=artifact_source_ref(artifact),
            depends_on=flux_kustomization_depends_on_many(
                seaweedfs_external_credentials,
                seaweedfs_drivefs_artifacts_bucket,
                vm_images_publisher,
                seaweedfs_secrets,
                seaweedfs_cluster,
                gateway,
            ),
            wait=True,
            timeout="5m",
            decryption=SOPS_DECRYPTION,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="public-s3", namespace="seaweedfs"
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="S3Identity",
                    name="claude-reader",
                    namespace="seaweedfs",
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="S3Identity",
                    name="drivefs-artifacts-writer",
                    namespace="seaweedfs",
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="S3Identity",
                    name="drivefs-artifacts-reader",
                    namespace="seaweedfs",
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="S3Credentials",
                    name="claude-reader",
                    namespace="seaweedfs",
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="S3Credentials",
                    name="drivefs-artifacts-writer",
                    namespace="seaweedfs",
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="S3Credentials",
                    name="drivefs-artifacts-reader",
                    namespace="seaweedfs",
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="S3Policy",
                    name="claude-reader-buckets",
                    namespace="seaweedfs",
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="S3PolicyBinding",
                    name="claude-reader-buckets",
                    namespace="seaweedfs",
                ),
            ],
        ),
    )


def seaweedfs_secrets(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    seaweedfs_namespace: Kustomization,
    external_secrets_operator: Kustomization,
) -> Kustomization:
    name = "seaweedfs-secrets"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            suspend=False,
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            decryption=SOPS_DECRYPTION,
            source_ref=artifact_source_ref(artifact),
            depends_on=flux_kustomization_depends_on_many(
                seaweedfs_namespace,
                # ExternalSecret + SecretStore CRDs + ESO controller
                external_secrets_operator,
            ),
        ),
    )

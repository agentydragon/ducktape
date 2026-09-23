"""Flux Kustomizations for the cluster/k8s/authentik slice."""

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


def authentik(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, cnpg: Kustomization, monitoring_crds: Kustomization
) -> Kustomization:
    name = "authentik"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m0s",
            path=artifact_path(artifact),
            prune=True,
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            source_ref=artifact_source_ref(artifact),
            timeout="10m0s",
            wait=True,
            # Health checks ensure Authentik is fully operational before dependents start
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name="authentik", namespace="authentik"
                ),
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="authentik-server", namespace="authentik"
                ),
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="authentik-worker", namespace="authentik"
                ),
            ],
            decryption=SOPS_DECRYPTION,
            depends_on=flux_kustomization_depends_on_many(cnpg, monitoring_crds),
        ),
    )


def authentik_db_backups(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, cnpg: Kustomization, seaweedfs_cluster: Kustomization
) -> Kustomization:
    name = "authentik-db-backups"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="15m",
            path=artifact_path(artifact),
            prune=True,
            wait=True,
            source_ref=artifact_source_ref(artifact),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="Bucket",
                    name="authentik-db-backups",
                    namespace="authentik",
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="S3Identity",
                    name="authentik-db-backups",
                    namespace="authentik",
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="S3Credentials",
                    name="authentik-db-backups",
                    namespace="authentik",
                ),
                KustomizationSpecHealthChecks(
                    api_version="barmancloud.cnpg.io/v1",
                    kind="ObjectStore",
                    name="authentik-db-ovh",
                    namespace="authentik",
                ),
            ],
            depends_on=flux_kustomization_depends_on_many(cnpg, seaweedfs_cluster),
        ),
        description="Creates the Authentik CNPG backup schedule and its SeaweedFS storage.",
    )

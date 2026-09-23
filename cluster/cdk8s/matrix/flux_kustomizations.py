"""Flux Kustomizations for the cluster/k8s/matrix slice."""

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


def matrix(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, cnpg: Kustomization) -> Kustomization:
    name = "matrix"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="10m",
            source_ref=artifact_source_ref(artifact),
            path=artifact_path(artifact),
            prune=True,
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            wait=True,
            decryption=SOPS_DECRYPTION,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2",
                    kind="HelmRelease",
                    name="matrix-synapse",
                    namespace="matrix",
                )
            ],
            depends_on=flux_kustomization_depends_on_many(cnpg),
        ),
    )


def matrix_user_provisioner(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    external_secrets_config: Kustomization,
    forgejo_images: Kustomization,
    matrix: Kustomization,
) -> Kustomization:
    name = "matrix-user-provisioner"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            timeout="5m",
            # The job registers users against Synapse's admin API, so it must not start
            # until Synapse answers. Depending on the app Kustomization (which is
            # wait:true over the HelmRelease) gives that ordering; the health check states
            # it directly rather than relying on that transitively.
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2",
                    kind="HelmRelease",
                    name="matrix-synapse",
                    namespace="matrix",
                )
            ],
            depends_on=flux_kustomization_depends_on_many(
                external_secrets_config,
                forgejo_images,
                # Synapse is deployed and healthy; also carries the registration shared secret, admin and bot passwords
                matrix,
            ),
        ),
    )

"""Flux Kustomizations for the cluster/k8s/github-secrets-sync slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def github_secrets_sync(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    tofu_controller: Kustomization,
    tofu_state_db: Kustomization,
    github_secrets_sync_secrets: Kustomization,
    forgejo_images: Kustomization,
    seaweedfs_pr_visuals_bucket: Kustomization,
) -> Kustomization:
    name = "github-secrets-sync"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            timeout="10m",
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name="github-secrets-sync",
                    namespace="flux-system",
                )
            ],
            depends_on=flux_kustomization_depends_on_many(
                tofu_controller,
                tofu_state_db,
                github_secrets_sync_secrets,
                # The Terraform module reads the canonical ducktape-ci registry credential
                # from forgejo-images before publishing it to gaffer-private's GitHub Actions
                # secrets.
                forgejo_images,
                seaweedfs_pr_visuals_bucket,
            ),
        ),
    )


def github_secrets_sync_secrets(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    external_creds: Kustomization,
    external_secrets_config: Kustomization,
) -> Kustomization:
    name = "github-secrets-sync-secrets"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path=artifact_path(artifact),
            # CLEANUP: restore pruning after ESO owns flux-system/github-secrets-sync-pat
            # and the old SOPS inventory entry has been retired safely.
            prune=False,
            source_ref=artifact_source_ref(artifact),
            timeout="2m",
            depends_on=flux_kustomization_depends_on_many(external_creds, external_secrets_config),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="external-secrets.io/v1",
                    kind="ExternalSecret",
                    name="github-secrets-sync-pat",
                    namespace="flux-system",
                ),
                KustomizationSpecHealthChecks(
                    api_version="external-secrets.io/v1",
                    kind="ExternalSecret",
                    name="buildbuddy-api-key",
                    namespace="flux-system",
                ),
            ],
            decryption=SOPS_DECRYPTION,
        ),
    )

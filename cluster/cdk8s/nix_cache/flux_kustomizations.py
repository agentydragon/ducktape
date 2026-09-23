"""Flux Kustomizations for the cluster/k8s/nix-cache slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def nix_cache(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    cnpg: Kustomization,
    external_creds: Kustomization,
    external_secrets_config: Kustomization,
    forgejo_images: Kustomization,
    gateway: Kustomization,
    cert_manager: Kustomization,
    seaweedfs_cluster: Kustomization,
) -> Kustomization:
    name = "nix-cache"
    return flux_kustomization(
        chart,
        name,
        artifact,
        timeout="5m",
        decryption=SOPS_DECRYPTION,
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="apps/v1", kind="Deployment", name="attic", namespace="nix-cache"
            ),
            KustomizationSpecHealthChecks(
                api_version="external-secrets.io/v1",
                kind="ExternalSecret",
                name="github-secrets-sync-pat",
                namespace="nix-cache",
            ),
        ],
        depends_on=flux_kustomization_depends_on_many(
            cnpg, external_creds, external_secrets_config, forgejo_images, gateway, cert_manager, seaweedfs_cluster
        ),
    )

"""Flux Kustomizations for the cluster/k8s/nix-cache slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on


def nix_cache(
    chart: Chart,
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
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/nix-cache",
            prune=True,
            wait=True,
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
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
            depends_on=[
                flux_kustomization_depends_on(cnpg),
                flux_kustomization_depends_on(external_creds),
                flux_kustomization_depends_on(external_secrets_config),
                flux_kustomization_depends_on(forgejo_images),
                flux_kustomization_depends_on(gateway),
                flux_kustomization_depends_on(cert_manager),
                flux_kustomization_depends_on(seaweedfs_cluster),
            ],
        ),
    )

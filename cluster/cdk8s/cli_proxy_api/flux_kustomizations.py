"""Flux Kustomizations for the cluster/k8s/cli-proxy-api slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on


def cli_proxy_api(
    chart: Chart,
    external_secrets_config: Kustomization,
    gateway: Kustomization,
    cert_manager_environment: Kustomization,
    sso_providers_tf: Kustomization,
    forgejo_images: Kustomization,
) -> Kustomization:
    name = "cli-proxy-api"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/cli-proxy-api",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="5m",
            wait=True,
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            depends_on=[
                flux_kustomization_depends_on(external_secrets_config),
                flux_kustomization_depends_on(gateway),
                flux_kustomization_depends_on(cert_manager_environment),
                flux_kustomization_depends_on(sso_providers_tf),
                flux_kustomization_depends_on(forgejo_images),
            ],
        ),
    )

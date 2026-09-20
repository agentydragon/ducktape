"""Flux Kustomizations for the cluster/k8s/flux-webhook slice."""

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


def flux_webhook(
    chart: Chart,
    flux_webhook_token: Kustomization,
    ntfy: Kustomization,
    external_secrets_config: Kustomization,
    gateway: Kustomization,
) -> Kustomization:
    name = "flux-webhook"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/flux-webhook",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="5m",
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            depends_on=[
                flux_kustomization_depends_on(flux_webhook_token),
                flux_kustomization_depends_on(ntfy),
                flux_kustomization_depends_on(external_secrets_config),
                flux_kustomization_depends_on(gateway),
            ],
        ),
    )

"""Flux Kustomizations for the cluster/k8s/flux-webhook slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on_many


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
            decryption=SOPS_DECRYPTION,
            depends_on=flux_kustomization_depends_on_many(flux_webhook_token, ntfy, external_secrets_config, gateway),
        ),
    )

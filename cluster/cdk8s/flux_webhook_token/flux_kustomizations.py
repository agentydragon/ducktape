"""Flux Kustomizations for the cluster/k8s/flux-webhook-token slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on


def flux_webhook_token(
    chart: Chart,
    tofu_controller: Kustomization,
    tofu_state_db: Kustomization,
    github_secrets_sync_secrets: Kustomization,
) -> Kustomization:
    name = "flux-webhook-token"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/flux-webhook-token",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="5m",
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name="flux-webhook-token",
                    namespace="flux-system",
                )
            ],
            depends_on=[
                flux_kustomization_depends_on(tofu_controller),
                flux_kustomization_depends_on(tofu_state_db),
                flux_kustomization_depends_on(github_secrets_sync_secrets),
            ],
        ),
    )

"""Flux Kustomizations for the cluster/k8s/ollama slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def ollama(
    chart: Chart,
    gateway: Kustomization,
    cert_manager_environment: Kustomization,
    nvidia_runtimeclass: Kustomization,
    external_secrets_config: Kustomization,
    reflector: Kustomization,
    claude_rbac: Kustomization,
) -> Kustomization:
    name = "ollama"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path="./cluster/k8s/ollama",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name="ollama-app", namespace="ducktape-flux"
            ),
            timeout="10m",
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="external-secrets.io/v1",
                    kind="ExternalSecret",
                    name="ollama-direct-token",
                    namespace="ollama",
                )
            ],
            depends_on=flux_kustomization_depends_on_many(
                gateway,
                cert_manager_environment,
                nvidia_runtimeclass,
                # langfuse ESO remains
                external_secrets_config,
                reflector,
                claude_rbac,
            ),
        ),
    )

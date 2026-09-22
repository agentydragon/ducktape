"""Flux Kustomizations for the cluster/k8s/github-api-proxy slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecPostBuild,
    KustomizationSpecPostBuildSubstituteFrom,
    KustomizationSpecPostBuildSubstituteFromKind,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def github_api_proxy(
    chart: Chart,
    external_secrets_operator: Kustomization,
    cert_manager: Kustomization,
    cert_manager_issuer_config: Kustomization,
    monitoring_crds: Kustomization,
) -> Kustomization:
    name = "github-api-proxy"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="5m",
            path="./cluster/k8s/github-api-proxy",
            prune=True,
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            decryption=SOPS_DECRYPTION,
            post_build=KustomizationSpecPostBuild(
                substitute_from=[
                    KustomizationSpecPostBuildSubstituteFrom(
                        kind=KustomizationSpecPostBuildSubstituteFromKind.CONFIG_MAP, name="cert-manager-issuer-config"
                    )
                ]
            ),
            depends_on=flux_kustomization_depends_on_many(
                external_secrets_operator,
                cert_manager,
                cert_manager_issuer_config,
                # PodMonitor + PrometheusRule
                monitoring_crds,
            ),
        ),
    )

"""Flux Kustomizations for the cluster/k8s/github-api-proxy slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import (
    CERT_MANAGER_ISSUER_SUBSTITUTION,
    SOPS_DECRYPTION,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
)


def github_api_proxy(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    external_secrets_operator: Kustomization,
    cert_manager: Kustomization,
    cert_manager_issuer_config: Kustomization,
    monitoring_crds: Kustomization,
) -> Kustomization:
    name = "github-api-proxy"
    return flux_kustomization(
        chart,
        name,
        artifact,
        timeout="5m",
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        decryption=SOPS_DECRYPTION,
        post_build=CERT_MANAGER_ISSUER_SUBSTITUTION,
        depends_on=flux_kustomization_depends_on_many(
            external_secrets_operator,
            cert_manager,
            cert_manager_issuer_config,
            # PodMonitor + PrometheusRule
            monitoring_crds,
        ),
    )

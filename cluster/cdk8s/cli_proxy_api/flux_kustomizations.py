"""Flux Kustomizations for the cluster/k8s/cli-proxy-api slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def cli_proxy_api(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
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
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            timeout="5m",
            wait=True,
            decryption=SOPS_DECRYPTION,
            depends_on=flux_kustomization_depends_on_many(
                external_secrets_config, gateway, cert_manager_environment, sso_providers_tf, forgejo_images
            ),
        ),
    )

"""Flux Kustomizations for the cluster/k8s/github-api-proxy slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecPostBuild,
    KustomizationSpecPostBuildSubstituteFrom,
    KustomizationSpecPostBuildSubstituteFromKind,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def github_api_proxy(
    chart: Chart,
    external_secrets_config: Kustomization,
    github_api_proxy_identity: Kustomization,
    forgejo_images: Kustomization,
    seaweedfs_csi: Kustomization,
    monitoring_crds: Kustomization,
    gateway: Kustomization,
    reloader: Kustomization,
) -> Kustomization:
    name = "github-api-proxy"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="5m",
            path="./cluster/k8s/github-api-proxy/app",
            prune=True,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            depends_on=flux_kustomization_depends_on_many(
                external_secrets_config,
                github_api_proxy_identity,
                forgejo_images,
                seaweedfs_csi,
                # PodMonitor + PrometheusRule
                monitoring_crds,
                gateway,
                reloader,
            ),
        ),
    )


def github_api_proxy_identity(
    chart: Chart, cert_manager_environment: Kustomization, cert_manager_issuer_config: Kustomization
) -> Kustomization:
    name = "github-api-proxy-identity"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="5m",
            path="./cluster/k8s/github-api-proxy/identity",
            prune=True,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=flux_kustomization_depends_on_many(cert_manager_environment, cert_manager_issuer_config),
            post_build=KustomizationSpecPostBuild(
                substitute_from=[
                    KustomizationSpecPostBuildSubstituteFrom(
                        kind=KustomizationSpecPostBuildSubstituteFromKind.CONFIG_MAP, name="cert-manager-issuer-config"
                    )
                ]
            ),
        ),
    )

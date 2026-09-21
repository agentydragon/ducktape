"""Flux Kustomizations for the cluster/k8s/home-assistant slice."""

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

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def home_assistant(
    chart: Chart,
    local_path_provisioner: Kustomization,
    seaweedfs_cluster: Kustomization,
    volsync: Kustomization,
    external_secrets_config: Kustomization,
    forgejo_images: Kustomization,
    monitoring_crds: Kustomization,
    gateway: Kustomization,
    sso_providers_tf: Kustomization,
) -> Kustomization:
    name = "home-assistant"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="10m",
            path="./",
            prune=True,
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="Bucket",
                    name="home-assistant-backups",
                    namespace="home-assistant",
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="S3Credentials",
                    name="home-assistant-backups",
                    namespace="home-assistant",
                ),
                KustomizationSpecHealthChecks(
                    api_version="external-secrets.io/v1",
                    kind="ExternalSecret",
                    name="home-assistant-config-restic-tenant",
                    namespace="home-assistant",
                ),
            ],
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            depends_on=flux_kustomization_depends_on_many(
                local_path_provisioner,
                seaweedfs_cluster,
                volsync,
                external_secrets_config,
                forgejo_images,
                # ServiceMonitor + PrometheusRule
                monitoring_crds,
                gateway,
                sso_providers_tf,
            ),
        ),
        description=(
            "Home Assistant with encrypted Restic/VolSync backups and its dedicated private SeaweedFS S3 bucket."
        ),
    )

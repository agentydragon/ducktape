"""Flux Kustomizations for the cluster/k8s/oci-cache slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def oci_cache(
    chart: Chart, valkey: Kustomization, seaweedfs_registry_cache_bucket: Kustomization, monitoring_crds: Kustomization
) -> Kustomization:
    name = "oci-cache"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/oci-cache",
            prune=True,
            # Do not delete the cache namespace if this Flux owner is removed later.
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            wait=True,
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="zot", namespace="oci-cache"
                )
            ],
            depends_on=flux_kustomization_depends_on_many(
                # Namespace, app, and ServiceMonitor are managed together here.
                valkey,
                # S3 backend: tenant-local Bucket and operator-generated credentials.
                seaweedfs_registry_cache_bucket,
                monitoring_crds,
            ),
        ),
        description="Zot OCI pull-through cache and its namespace-local monitoring.",
    )

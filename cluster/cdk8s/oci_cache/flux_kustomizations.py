"""Flux Kustomizations for the cluster/k8s/oci-cache slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def oci_cache() -> dict[str, object]:
    name = "oci-cache"
    return flux_kustomization(
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
            depends_on=[
                # Namespace, app, and ServiceMonitor are managed together here.
                KustomizationSpecDependsOn(name="valkey", namespace="ducktape-flux"),
                # S3 backend: tenant-local Bucket and operator-generated credentials.
                KustomizationSpecDependsOn(name="seaweedfs-registry-cache-bucket", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="monitoring-crds", namespace="ducktape-flux"),
            ],
        ),
        description="Zot OCI pull-through cache and its namespace-local monitoring.",
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/oci-cache/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, oci_cache())

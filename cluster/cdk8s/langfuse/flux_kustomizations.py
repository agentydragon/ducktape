"""Flux Kustomizations for the cluster/k8s/langfuse slice."""

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

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on


def langfuse(
    chart: Chart,
    langfuse_namespace: Kustomization,
    langfuse_secrets: Kustomization,
    langfuse_cache: Kustomization,
    langfuse_db: Kustomization,
    clickhouse: Kustomization,
    langfuse_seaweed: Kustomization,
    gateway: Kustomization,
    claude_rbac: Kustomization,
) -> Kustomization:
    name = "langfuse"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            suspend=False,
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/langfuse/app",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name="langfuse-app", namespace="ducktape-flux"
            ),
            timeout="20m",
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name="langfuse", namespace="langfuse"
                )
            ],
            depends_on=[
                flux_kustomization_depends_on(langfuse_namespace),
                flux_kustomization_depends_on(langfuse_secrets),
                flux_kustomization_depends_on(langfuse_cache),
                flux_kustomization_depends_on(langfuse_db),
                flux_kustomization_depends_on(clickhouse),
                flux_kustomization_depends_on(langfuse_seaweed),
                flux_kustomization_depends_on(gateway),
                flux_kustomization_depends_on(claude_rbac),
            ],
        ),
    )


def langfuse_cache(
    chart: Chart, langfuse_namespace: Kustomization, valkey: Kustomization, local_path_provisioner: Kustomization
) -> Kustomization:
    name = "langfuse-cache"
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
            path="./cluster/k8s/langfuse/cache",
            prune=True,
            wait=True,
            depends_on=[
                flux_kustomization_depends_on(langfuse_namespace),
                flux_kustomization_depends_on(valkey),
                flux_kustomization_depends_on(local_path_provisioner),
            ],
        ),
    )


def langfuse_db(
    chart: Chart, langfuse_namespace: Kustomization, cnpg: Kustomization, local_path_provisioner: Kustomization
) -> Kustomization:
    name = "langfuse-db"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            suspend=False,
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/langfuse/db",
            prune=True,
            wait=True,
            depends_on=[
                flux_kustomization_depends_on(langfuse_namespace),
                flux_kustomization_depends_on(cnpg),
                flux_kustomization_depends_on(local_path_provisioner),
            ],
        ),
    )


def langfuse_namespace(chart: Chart) -> Kustomization:
    name = "langfuse-namespace"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            suspend=False,
            interval="10m",
            path="./cluster/k8s/langfuse/namespace",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
        ),
    )


def langfuse_seaweed(
    chart: Chart, langfuse_namespace: Kustomization, seaweedfs_cluster: Kustomization
) -> Kustomization:
    name = "langfuse-seaweed"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            suspend=False,
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/langfuse/seaweed",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="5m",
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1", kind="Bucket", name="langfuse", namespace="langfuse"
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1", kind="S3Credentials", name="langfuse", namespace="langfuse"
                ),
            ],
            depends_on=[
                flux_kustomization_depends_on(langfuse_namespace),
                flux_kustomization_depends_on(seaweedfs_cluster),
            ],
        ),
    )


def langfuse_secrets(
    chart: Chart, langfuse_namespace: Kustomization, seaweedfs_cluster: Kustomization
) -> Kustomization:
    name = "langfuse-secrets"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            suspend=False,
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/langfuse/secrets",
            prune=True,
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="10m",
            depends_on=[
                flux_kustomization_depends_on(langfuse_namespace),
                flux_kustomization_depends_on(seaweedfs_cluster),
            ],
        ),
    )

"""Flux Kustomizations for the cluster/k8s/langfuse slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def langfuse() -> dict[str, object]:
    name = "langfuse"
    return flux_kustomization(
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
                KustomizationSpecDependsOn(name="langfuse-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="langfuse-secrets", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="langfuse-cache", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="langfuse-db", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="clickhouse", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="langfuse-seaweed", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="gateway", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="claude-rbac", namespace="ducktape-flux"),
            ],
        ),
    )


def langfuse_cache() -> dict[str, object]:
    name = "langfuse-cache"
    return flux_kustomization(
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
                KustomizationSpecDependsOn(name="langfuse-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="valkey", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="local-path-provisioner", namespace="ducktape-flux"),
            ],
        ),
    )


def langfuse_db() -> dict[str, object]:
    name = "langfuse-db"
    return flux_kustomization(
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
                KustomizationSpecDependsOn(name="langfuse-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cnpg", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="local-path-provisioner", namespace="ducktape-flux"),
            ],
        ),
    )


def langfuse_namespace() -> dict[str, object]:
    name = "langfuse-namespace"
    return flux_kustomization(
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


def langfuse_seaweed() -> dict[str, object]:
    name = "langfuse-seaweed"
    return flux_kustomization(
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
                KustomizationSpecDependsOn(name="langfuse-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="seaweedfs-cluster", namespace="ducktape-flux"),
            ],
        ),
    )


def langfuse_secrets() -> dict[str, object]:
    name = "langfuse-secrets"
    return flux_kustomization(
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
                KustomizationSpecDependsOn(name="langfuse-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="seaweedfs-cluster", namespace="ducktape-flux"),
            ],
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/langfuse/app/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, langfuse())
    path = root / "cluster/k8s/langfuse/cache/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, langfuse_cache())
    path = root / "cluster/k8s/langfuse/db/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, langfuse_db())
    path = root / "cluster/k8s/langfuse/namespace/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, langfuse_namespace())
    path = root / "cluster/k8s/langfuse/seaweed/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, langfuse_seaweed())
    path = root / "cluster/k8s/langfuse/secrets/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, langfuse_secrets())

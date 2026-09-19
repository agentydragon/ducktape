"""Flux Kustomizations for the cluster/k8s/tofu-state slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthCheckExprs,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def tofu_state_db() -> dict[str, object]:
    name = "tofu-state-db"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/tofu-state/db",
            prune=True,
            wait=True,
            health_check_exprs=[
                KustomizationSpecHealthCheckExprs(
                    api_version="postgresql.cnpg.io/v1",
                    kind="Database",
                    current=(
                        "has(status.applied) && status.applied && "
                        "has(status.observedGeneration) && status.observedGeneration == "
                        "metadata.generation"
                    ),
                )
            ],
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            depends_on=[
                KustomizationSpecDependsOn(name="tofu-state-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cnpg", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="local-path-provisioner", namespace="ducktape-flux"),
            ],
        ),
    )


def tofu_state_namespace() -> dict[str, object]:
    name = "tofu-state-namespace"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/tofu-state/namespace",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="2m",
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/tofu-state/db/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, tofu_state_db())
    path = root / "cluster/k8s/tofu-state/namespace/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, tofu_state_namespace())

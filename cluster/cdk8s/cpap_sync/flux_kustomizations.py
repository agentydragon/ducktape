"""Flux Kustomizations for the cluster/k8s/cpap-sync slice."""

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


def cpap_sync() -> dict[str, object]:
    name = "cpap-sync"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path="./cluster/k8s/cpap-sync",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            timeout="30m",
            depends_on=[
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="kubevirt", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
            ],
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="kubevirt.io/v1", kind="VirtualMachine", name="cpap-gateway", namespace="cpap-sync"
                )
            ],
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/cpap-sync/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, cpap_sync())

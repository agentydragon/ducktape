"""Flux Kustomizations for the cluster/k8s/gaffer-private-source slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecDependsOn,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def gaffer_private_source() -> dict[str, object]:
    name = "gaffer-private-source"
    manifest = flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="10m",
            path="./cluster/k8s/gaffer-private-source",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY, name="flux-system"
            ),
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            depends_on=[KustomizationSpecDependsOn(name="flux-image-automation-ghcr", namespace="ducktape-flux")],
        ),
    )
    manifest["metadata"] = {"name": name, "namespace": "flux-system"}
    return manifest


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/gaffer-private-source/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, gaffer_private_source())

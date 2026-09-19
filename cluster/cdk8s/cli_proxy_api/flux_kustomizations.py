"""Flux Kustomizations for the cluster/k8s/cli-proxy-api slice."""

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


def cli_proxy_api() -> dict[str, object]:
    name = "cli-proxy-api"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/cli-proxy-api",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="5m",
            wait=True,
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            depends_on=[
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="gateway", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cert-manager-environment", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="sso-providers-tf", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
            ],
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/cli-proxy-api/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, cli_proxy_api())

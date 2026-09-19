"""Flux Kustomizations for the cluster/k8s/nix-cache slice."""

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


def nix_cache() -> dict[str, object]:
    name = "nix-cache"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/nix-cache",
            prune=True,
            wait=True,
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="attic", namespace="nix-cache"
                ),
                KustomizationSpecHealthChecks(
                    api_version="external-secrets.io/v1",
                    kind="ExternalSecret",
                    name="github-secrets-sync-pat",
                    namespace="nix-cache",
                ),
            ],
            depends_on=[
                KustomizationSpecDependsOn(name="cnpg", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="external-creds", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="gateway", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cert-manager", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="seaweedfs-cluster", namespace="ducktape-flux"),
            ],
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/nix-cache/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, nix_cache())

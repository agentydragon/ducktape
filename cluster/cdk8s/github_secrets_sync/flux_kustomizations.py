"""Flux Kustomizations for the cluster/k8s/github-secrets-sync slice."""

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


def github_secrets_sync() -> dict[str, object]:
    name = "github-secrets-sync"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/github-secrets-sync",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="10m",
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name="github-secrets-sync",
                    namespace="flux-system",
                )
            ],
            depends_on=[
                KustomizationSpecDependsOn(name="tofu-controller", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="tofu-state-db", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="github-secrets-sync-secrets", namespace="ducktape-flux"),
                # The Terraform module reads the canonical ducktape-ci registry credential
                # from forgejo-images before publishing it to gaffer-private's GitHub Actions
                # secrets.
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="seaweedfs-pr-visuals-bucket", namespace="ducktape-flux"),
            ],
        ),
    )


def github_secrets_sync_secrets() -> dict[str, object]:
    name = "github-secrets-sync-secrets"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path="./cluster/k8s/github-secrets-sync/secrets",
            # CLEANUP: restore pruning after ESO owns flux-system/github-secrets-sync-pat
            # and the old SOPS inventory entry has been retired safely.
            prune=False,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="2m",
            depends_on=[
                KustomizationSpecDependsOn(name="external-creds", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
            ],
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="external-secrets.io/v1",
                    kind="ExternalSecret",
                    name="github-secrets-sync-pat",
                    namespace="flux-system",
                )
            ],
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/github-secrets-sync/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, github_secrets_sync())
    path = root / "cluster/k8s/github-secrets-sync/secrets/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, github_secrets_sync_secrets())

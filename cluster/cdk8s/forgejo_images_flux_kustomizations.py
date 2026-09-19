"""Flux Kustomizations for the cluster/k8s/forgejo-images slice."""

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


def forgejo_images() -> dict[str, object]:
    name = "forgejo-images"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="10m",
            path="./cluster/k8s/forgejo-images",
            prune=True,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name="forgejo-images",
                    namespace="flux-system",
                )
            ],
            depends_on=[
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="forgejo",  # Forgejo API must be up (provider target)
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(name="tofu-controller", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="tofu-state-db", namespace="ducktape-flux"),
            ],
        ),
        description=(
            "ducktape-ci Forgejo registry tenant — shared credential (read by "
            "consumers, including flux-system, via per-namespace ExternalSecrets "
            "against kubernetes-forgejo-images-secret-store) + Terraform that "
            "provisions the Forgejo user."
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/forgejo-images/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, forgejo_images())

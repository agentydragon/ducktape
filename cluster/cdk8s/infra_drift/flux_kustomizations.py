"""Flux Kustomizations for the cluster/k8s/infra-drift slice."""

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


def infra_drift() -> dict[str, object]:
    name = "infra-drift"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="10m",
            path="./cluster/k8s/infra-drift",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            # Ready tracks the plan, so a drift finding shows up here as a NotReady
            # Kustomization — README § Reading a plan.
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name="infra-drift",
                    namespace="flux-system",
                )
            ],
            depends_on=[
                KustomizationSpecDependsOn(name="tofu-controller", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="tofu-state-db", namespace="ducktape-flux"),
            ],
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/infra-drift/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, infra_drift())

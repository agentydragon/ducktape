"""Flux Kustomizations for the cluster/k8s/ollama slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def ollama() -> dict[str, object]:
    name = "ollama"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path="./cluster/k8s/ollama",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name="ollama-app", namespace="ducktape-flux"
            ),
            timeout="10m",
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="external-secrets.io/v1",
                    kind="ExternalSecret",
                    name="ollama-direct-token",
                    namespace="ollama",
                )
            ],
            depends_on=[
                KustomizationSpecDependsOn(name="gateway", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cert-manager-environment", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="nvidia-runtimeclass", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="external-secrets-config",  # langfuse ESO remains
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(name="reflector", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="claude-rbac", namespace="ducktape-flux"),
            ],
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/ollama/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, ollama())

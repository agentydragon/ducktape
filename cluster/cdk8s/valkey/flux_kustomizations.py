"""Flux Kustomizations for the cluster/k8s/valkey slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def valkey() -> dict[str, object]:
    name = "valkey"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path="./cluster/k8s/valkey",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2",
                    kind="HelmRelease",
                    name="redis-operator",
                    namespace="valkey-system",
                )
            ],
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/valkey/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, valkey())

"""Flux Kustomizations for the cluster/k8s/tofu-controller slice."""

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


def tofu_controller() -> dict[str, object]:
    name = "tofu-controller"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m0s",
            path="./cluster/k8s/tofu-controller",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="10m0s",
            wait=True,
            depends_on=[
                KustomizationSpecDependsOn(name="cert-manager", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="kyverno", namespace="ducktape-flux"),
            ],
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2",
                    kind="HelmRelease",
                    name="tofu-controller",
                    namespace="flux-system",
                ),
                KustomizationSpecHealthChecks(
                    api_version="apiextensions.k8s.io/v1",
                    kind="CustomResourceDefinition",
                    name="terraforms.infra.contrib.fluxcd.io",
                    namespace="",
                ),
            ],
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/tofu-controller/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, tofu_controller())

"""Flux Kustomizations for the cluster/k8s/gateway slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthChecks,
    KustomizationSpecPostBuild,
    KustomizationSpecPostBuildSubstituteFrom,
    KustomizationSpecPostBuildSubstituteFromKind,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def gateway() -> dict[str, object]:
    name = "gateway"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/gateway",
            prune=True,
            wait=True,
            depends_on=[
                KustomizationSpecDependsOn(name="cert-manager", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="kyverno", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cert-manager-issuer-config", namespace="ducktape-flux"),
            ],
            post_build=KustomizationSpecPostBuild(
                substitute_from=[
                    KustomizationSpecPostBuildSubstituteFrom(
                        kind=KustomizationSpecPostBuildSubstituteFromKind.CONFIG_MAP, name="cert-manager-issuer-config"
                    )
                ]
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="gateway.networking.k8s.io/v1",
                    kind="Gateway",
                    name="cluster-gateway",
                    namespace="gateway-system",
                )
            ],
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/gateway/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, gateway())

"""Flux Kustomizations for the cluster/k8s/seaweedfs-csi slice."""

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


def seaweedfs_csi() -> dict[str, object]:
    name = "seaweedfs-csi"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="10m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/seaweedfs-csi",
            prune=True,
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2",
                    kind="HelmRelease",
                    name="seaweedfs-csi-driver",
                    namespace="seaweedfs-csi-system",
                )
            ],
            depends_on=[KustomizationSpecDependsOn(name="seaweedfs-cluster", namespace="ducktape-flux")],
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/seaweedfs-csi/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, seaweedfs_csi())

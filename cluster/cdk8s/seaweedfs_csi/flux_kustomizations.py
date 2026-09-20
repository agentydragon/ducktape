"""Flux Kustomizations for the cluster/k8s/seaweedfs-csi slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on


def seaweedfs_csi(chart: Chart, seaweedfs_cluster: Kustomization) -> Kustomization:
    name = "seaweedfs-csi"
    return flux_kustomization(
        chart,
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
            depends_on=[flux_kustomization_depends_on(seaweedfs_cluster)],
        ),
    )

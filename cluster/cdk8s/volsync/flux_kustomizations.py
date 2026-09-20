"""Flux Kustomizations for the cluster/k8s/volsync slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecHealthCheckExprs,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on


def volsync(chart: Chart, snapshot_controller: Kustomization) -> Kustomization:
    name = "volsync"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/volsync",
            prune=True,
            wait=True,
            depends_on=[flux_kustomization_depends_on(snapshot_controller)],
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2",
                    kind="HelmRelease",
                    name="volsync",
                    namespace="volsync-system",
                )
            ],
            # The token controller populates data.token asynchronously. Do not declare
            # the VolSync auth material ready until Alloy can actually use it.
            health_check_exprs=[
                KustomizationSpecHealthCheckExprs(
                    api_version="v1", kind="Secret", current="has(data.token) && data.token != ''"
                )
            ],
        ),
    )

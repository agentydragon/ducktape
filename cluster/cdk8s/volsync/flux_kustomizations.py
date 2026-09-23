"""Flux Kustomizations for the cluster/k8s/volsync slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecHealthCheckExprs,
    KustomizationSpecHealthChecks,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on


def volsync(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, snapshot_controller: Kustomization
) -> Kustomization:
    name = "volsync"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=artifact_source_ref(artifact),
            path=artifact_path(artifact),
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

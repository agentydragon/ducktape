"""Flux Kustomizations for the cluster/k8s/metrics-server slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on


def metrics_server(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, kyverno: Kustomization) -> Kustomization:
    name = "metrics-server"
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
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2",
                    kind="HelmRelease",
                    name="metrics-server",
                    namespace="kube-system",
                )
            ],
            depends_on=[
                # Kyverno webhook must be ready before creating workloads
                flux_kustomization_depends_on(kyverno)
            ],
        ),
    )

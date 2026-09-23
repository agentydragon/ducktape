"""Flux Kustomizations for the cluster/k8s/flux-monitoring slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on


def flux_monitoring(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, monitoring_crds: Kustomization
) -> Kustomization:
    name = "flux-monitoring"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="2m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            depends_on=[
                # PodMonitor CRD ships with kube-prometheus-stack in monitoring-stack.
                # PodMonitor
                flux_kustomization_depends_on(monitoring_crds)
            ],
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="monitoring.coreos.com/v1",
                    kind="PodMonitor",
                    name="flux-system",
                    namespace="flux-system",
                )
            ],
        ),
    )

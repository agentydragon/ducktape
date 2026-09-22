"""Flux Kustomizations for the cluster/k8s/vector-talos-logs slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on


def vector_talos_logs(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, loki: Kustomization) -> Kustomization:
    name = "vector-talos-logs"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            timeout="2m",
            depends_on=[
                # loki-write is the log sink.
                flux_kustomization_depends_on(loki)
            ],
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="DaemonSet", name="vector-talos-logs", namespace="vector-talos-logs"
                )
            ],
        ),
    )

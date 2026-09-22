"""Flux Kustomizations for the cluster/k8s/descheduler slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on


def descheduler(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, kyverno: Kustomization) -> Kustomization:
    name = "descheduler"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            depends_on=[flux_kustomization_depends_on(kyverno)],
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
                    name="descheduler",
                    namespace="kube-system",
                )
            ],
        ),
    )

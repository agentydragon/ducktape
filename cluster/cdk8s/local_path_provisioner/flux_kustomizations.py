"""Flux Kustomizations for the cluster/k8s/local-path-provisioner slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization


def local_path_provisioner(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    name = "local-path-provisioner"
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
                    name="local-path-provisioner",
                    namespace="local-path-storage",
                ),
                KustomizationSpecHealthChecks(
                    api_version="apps/v1",
                    kind="Deployment",
                    name="local-path-provisioner",
                    namespace="local-path-storage",
                ),
            ],
        ),
    )

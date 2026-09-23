"""Flux Kustomizations for the cluster/k8s/talos-cloud-controller-manager slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization


def talos_cloud_controller_manager(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    name = "talos-cloud-controller-manager"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="30m",
            path=artifact_path(artifact),
            prune=True,
            wait=True,
            source_ref=artifact_source_ref(artifact),
            target_namespace="kube-system",
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2",
                    kind="HelmRelease",
                    name="talos-cloud-controller-manager",
                    namespace="kube-system",
                )
            ],
        ),
    )

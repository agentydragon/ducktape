"""Flux Kustomizations for the cluster/k8s/headlamp slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def headlamp(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, gateway: Kustomization, sso_providers_tf: Kustomization
) -> Kustomization:
    name = "headlamp"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="10m",
            source_ref=artifact_source_ref(artifact),
            path=artifact_path(artifact),
            prune=True,
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(api_version="v1", kind="Namespace", name="headlamp"),
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name="headlamp", namespace="headlamp"
                ),
            ],
            depends_on=flux_kustomization_depends_on_many(gateway, sso_providers_tf),
        ),
    )

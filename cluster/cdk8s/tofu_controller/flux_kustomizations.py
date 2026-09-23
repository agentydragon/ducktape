"""Flux Kustomizations for the cluster/k8s/tofu-controller slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def tofu_controller(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, cert_manager: Kustomization, kyverno: Kustomization
) -> Kustomization:
    name = "tofu-controller"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m0s",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            timeout="10m0s",
            wait=True,
            depends_on=flux_kustomization_depends_on_many(cert_manager, kyverno),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2",
                    kind="HelmRelease",
                    name="tofu-controller",
                    namespace="flux-system",
                ),
                KustomizationSpecHealthChecks(
                    api_version="apiextensions.k8s.io/v1",
                    kind="CustomResourceDefinition",
                    name="terraforms.infra.contrib.fluxcd.io",
                    namespace="",
                ),
            ],
        ),
    )

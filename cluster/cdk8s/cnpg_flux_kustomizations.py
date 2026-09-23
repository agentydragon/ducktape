"""Flux Kustomizations for the cluster/k8s/cnpg slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on


def cnpg(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, cert_manager: Kustomization) -> Kustomization:
    name = "cnpg"
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
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name="cnpg", namespace="cnpg-system"
                ),
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2",
                    kind="HelmRelease",
                    name="plugin-barman-cloud",
                    namespace="cnpg-system",
                ),
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="plugin-barman-cloud", namespace="cnpg-system"
                ),
            ],
            depends_on=[flux_kustomization_depends_on(cert_manager)],
        ),
    )

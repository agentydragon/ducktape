"""Flux Kustomizations for the cluster/k8s/flux-grafana-secrets slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def flux_grafana_secrets(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    grafana_instance: Kustomization,
    grafana_operator: Kustomization,
) -> Kustomization:
    name = "flux-grafana-secrets"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="grafana.integreatly.org/v1beta1",
                    kind="GrafanaServiceAccount",
                    name="flux-notifications",
                    namespace="flux-system",
                )
            ],
            timeout="5m",
            depends_on=flux_kustomization_depends_on_many(grafana_instance, grafana_operator),
        ),
    )

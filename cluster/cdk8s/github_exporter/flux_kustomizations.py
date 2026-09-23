"""Flux Kustomizations for the cluster/k8s/github-exporter slice."""

from __future__ import annotations

from cdk8s import Chart
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def github_exporter(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    forgejo_images: Kustomization,
    monitoring_namespace: Kustomization,
    monitoring_crds: Kustomization,
    grafana_instance: Kustomization,
    external_secrets_config: Kustomization,
    external_creds: Kustomization,
) -> Kustomization:
    name = "github-exporter"
    return flux_kustomization(
        chart,
        name,
        artifact,
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(
            forgejo_images,
            monitoring_namespace,
            # ServiceMonitor
            monitoring_crds,
            grafana_instance,
            external_secrets_config,
            external_creds,
        ),
        description="GitHub API rate-limit metrics for the human and agent accounts.",
    )

"""Flux Kustomizations for the cluster/k8s/github-exporter slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def github_exporter(
    chart: Chart,
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
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path="./cluster/k8s/github-exporter",
            prune=True,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="5m",
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="external-secrets.io/v1",
                    kind="ExternalSecret",
                    name="github-exporter-agentydragon-token",
                    namespace="monitoring",
                ),
                KustomizationSpecHealthChecks(
                    api_version="external-secrets.io/v1",
                    kind="ExternalSecret",
                    name="github-exporter-agentydragon-agent-token",
                    namespace="monitoring",
                ),
                KustomizationSpecHealthChecks(
                    api_version="apps/v1",
                    kind="Deployment",
                    name="github-exporter-agentydragon",
                    namespace="monitoring",
                ),
                KustomizationSpecHealthChecks(
                    api_version="apps/v1",
                    kind="Deployment",
                    name="github-exporter-agentydragon-agent",
                    namespace="monitoring",
                ),
            ],
            depends_on=flux_kustomization_depends_on_many(
                forgejo_images,
                monitoring_namespace,
                # ServiceMonitor
                monitoring_crds,
                grafana_instance,
                external_secrets_config,
                external_creds,
            ),
        ),
        description="GitHub API rate-limit metrics for the human and agent accounts.",
    )

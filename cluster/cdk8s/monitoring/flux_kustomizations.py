"""Generated Flux Kustomizations for the monitoring slice."""

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpecDeletionPolicy,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def alloy(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, mimir: Kustomization, grafana_helmrepository: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        "alloy",
        artifact,
        wait=None,
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name="alloy", namespace="monitoring"
            )
        ],
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(mimir, grafana_helmrepository),
    )


def monitoring_crds(chart: Chart) -> Kustomization:
    return flux_kustomization(
        chart,
        "monitoring-crds",
        # The description-bearing variant: the CRDs kube-prometheus-stack's own `crds`
        # subchart installed, so adopting them changes no schema.
        KustomizationSpecSourceRef(
            kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY,
            name="prometheus-operator-source",
            namespace="ducktape-flux",
        ),
        interval="1h",
        path="./example/prometheus-operator-crd-full",
        prune=False,  # Don't delete CRDs on uninstall (safety)
        timeout="5m",
    )


def grafana_instance(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, grafana_operator: Kustomization, cnpg: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        "grafana-instance",
        artifact,
        wait=None,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="postgresql.cnpg.io/v1", kind="Cluster", name="grafana-db-ovh", namespace="monitoring"
            ),
            KustomizationSpecHealthChecks(
                api_version="apps/v1", kind="Deployment", name="grafana-deployment", namespace="monitoring"
            ),
        ],
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(grafana_operator, cnpg),
    )

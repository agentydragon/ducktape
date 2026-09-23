"""Generated Flux Kustomizations for the monitoring slice."""

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on,
    flux_kustomization_depends_on_many,
)


def alloy(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, mimir: Kustomization, grafana_helmrepository: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        "alloy",
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name="alloy", namespace="monitoring"
                )
            ],
            timeout="5m",
            depends_on=flux_kustomization_depends_on_many(mimir, grafana_helmrepository),
        ),
    )


def monitoring_crds(chart: Chart) -> Kustomization:
    return flux_kustomization(
        chart,
        "monitoring-crds",
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="1h",
            # The description-bearing variant: the CRDs kube-prometheus-stack's own `crds`
            # subchart installed, so adopting them changes no schema.
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY,
                name="prometheus-operator-source",
                namespace="ducktape-flux",
            ),
            path="./example/prometheus-operator-crd-full",
            prune=False,  # Don't delete CRDs on uninstall (safety)
            wait=True,
            timeout="5m",
        ),
    )


def grafana_instance(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, grafana_operator: Kustomization, cnpg: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        "grafana-instance",
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            source_ref=artifact_source_ref(artifact),
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
        ),
    )


def loki(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    grafana_helmrepository: Kustomization,
    seaweedfs_cluster: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        "loki",
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            decryption=SOPS_DECRYPTION,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name="loki", namespace="loki"
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1", kind="Bucket", name="loki", namespace="loki"
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1", kind="S3Credentials", name="loki", namespace="loki"
                ),
            ],
            timeout="10m",
            depends_on=flux_kustomization_depends_on_many(grafana_helmrepository, seaweedfs_cluster),
        ),
    )


def mimir(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    monitoring_crds: Kustomization,
    grafana_helmrepository: Kustomization,
    seaweedfs_cluster: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        "mimir",
        spec=KustomizationSpec(
            suspend=False,
            retry_interval="1m",
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            decryption=SOPS_DECRYPTION,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1", kind="Bucket", name="mimir-blocks", namespace="monitoring"
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1", kind="Bucket", name="mimir-ruler", namespace="monitoring"
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1", kind="S3Credentials", name="mimir", namespace="monitoring"
                ),
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name="mimir", namespace="monitoring"
                ),
            ],
            timeout="10m",
            depends_on=flux_kustomization_depends_on_many(
                # the chart's metaMonitoring.serviceMonitor
                monitoring_crds,
                grafana_helmrepository,
                # seaweedfs-cluster provides the Seaweed CR + Bucket CRD that our
                # mimir-blocks / mimir-ruler Bucket resources reference (buckets.yaml).
                seaweedfs_cluster,
            ),
        ),
    )


def monitoring_rules(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, monitoring_crds: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        "monitoring-rules",
        spec=KustomizationSpec(
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            depends_on=[
                # PrometheusRule
                flux_kustomization_depends_on(monitoring_crds)
            ],
        ),
    )


def tempo(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    monitoring_crds: Kustomization,
    grafana_helmrepository: Kustomization,
    seaweedfs_cluster: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        "tempo",
        spec=KustomizationSpec(
            suspend=False,
            retry_interval="1m",
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            decryption=SOPS_DECRYPTION,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1", kind="Bucket", name="tempo", namespace="monitoring"
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1", kind="S3Credentials", name="tempo", namespace="monitoring"
                ),
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name="tempo", namespace="monitoring"
                ),
            ],
            timeout="5m",
            depends_on=flux_kustomization_depends_on_many(
                # the chart's serviceMonitor.enabled
                monitoring_crds,
                grafana_helmrepository,
                seaweedfs_cluster,
            ),
        ),
    )

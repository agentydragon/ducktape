"""Generated Flux Kustomizations for the monitoring slice."""

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecHealthCheckExprs,
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


def alloy_otlp_bearer_token_tf(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    tofu_controller: Kustomization,
    tofu_state_db: Kustomization,
    authentik_jwt_rotation: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        "alloy-otlp-bearer-token-tf",
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name="alloy-otlp-bearer-token",
                    namespace="flux-system",
                )
            ],
            timeout="10m",
            depends_on=flux_kustomization_depends_on_many(
                tofu_controller,
                tofu_state_db,
                # authentik-jwt-rotation owns the agents-infra namespace this secret's
                # rotator runs in, and rotates the alloy-otlp bearer token committed here.
                authentik_jwt_rotation,
            ),
        ),
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


def cilium_monitoring(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, monitoring_crds: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        "cilium-monitoring",
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="2m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            depends_on=[
                # ServiceMonitor
                flux_kustomization_depends_on(monitoring_crds)
            ],
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="monitoring.coreos.com/v1",
                    kind="ServiceMonitor",
                    name="cilium-agent",
                    namespace="monitoring",
                ),
                KustomizationSpecHealthChecks(
                    api_version="monitoring.coreos.com/v1", kind="ServiceMonitor", name="hubble", namespace="monitoring"
                ),
            ],
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


def grafana_helmrepository(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    return flux_kustomization(
        chart,
        "grafana-helmrepository",
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
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


def grafana_operator(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, monitoring_namespace: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        "grafana-operator",
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2",
                    kind="HelmRelease",
                    name="grafana-operator",
                    namespace="monitoring",
                )
            ],
            timeout="5m",
            depends_on=[flux_kustomization_depends_on(monitoring_namespace)],
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


def monitoring_namespace(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    return flux_kustomization(
        chart,
        "monitoring-namespace",
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            wait=True,
            # Health check ensures monitoring namespace exists before dependents deploy
            health_checks=[KustomizationSpecHealthChecks(api_version="v1", kind="Namespace", name="monitoring")],
            depends_on=[],
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


def monitoring_stack(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    monitoring_namespace: Kustomization,
    monitoring_crds: Kustomization,
    ntfy: Kustomization,
    external_secrets_config: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        "monitoring-stack",
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            decryption=SOPS_DECRYPTION,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="v1", kind="Secret", name="alloy-control-plane-token", namespace="monitoring"
                ),
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2",
                    kind="HelmRelease",
                    name="kube-prometheus-stack",
                    namespace="monitoring",
                ),
            ],
            # The built-in Secret health check only checks existence. This CEL check waits
            # for the service-account token controller to populate data.token.
            health_check_exprs=[
                KustomizationSpecHealthCheckExprs(
                    api_version="v1", kind="Secret", current="has(data.token) && data.token != ''"
                )
            ],
            timeout="10m",
            depends_on=flux_kustomization_depends_on_many(
                monitoring_namespace,
                # The chart's Prometheus/Alertmanager CRs are rejected at admission until
                # the CRDs exist, and the chart no longer installs them itself.
                monitoring_crds,
                ntfy,
                external_secrets_config,
            ),
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

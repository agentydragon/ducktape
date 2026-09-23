"""Mimir (metrics storage and the ruler) with its tenant-local SeaweedFS buckets and
credentials."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmRelease,
    HelmReleaseSpec,
    HelmReleaseSpecChart,
    HelmReleaseSpecChartSpec,
    HelmReleaseSpecChartSpecSourceRef,
    HelmReleaseSpecChartSpecSourceRefKind,
    HelmReleaseSpecInstall,
    HelmReleaseSpecInstallRemediation,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from seaweed_bucket_crds.com.seaweedfs.seaweed import (
    Bucket,
    BucketSpec,
    BucketSpecAccess,
    BucketSpecAccessActions,
    BucketSpecClusterRef,
    BucketSpecReclaimPolicy,
)
from seaweed_resourcereferencegrant_crds.com.seaweedfs.seaweed import (
    ResourceReferenceGrant,
    ResourceReferenceGrantSpec,
    ResourceReferenceGrantSpecFrom,
    ResourceReferenceGrantSpecTo,
)
from seaweed_s3credentials_crds.com.seaweedfs.seaweed import (
    S3Credentials,
    S3CredentialsSpec,
    S3CredentialsSpecIdentityRef,
    S3CredentialsSpecReclaimPolicy,
    S3CredentialsSpecSeaweedRef,
    S3CredentialsSpecSecretRef,
)
from seaweed_s3identity_crds.com.seaweedfs.seaweed import (
    S3Identity,
    S3IdentitySpec,
    S3IdentitySpecReclaimPolicy,
    S3IdentitySpecSeaweedRef,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

NAME = "mimir"
OUTPUT_DIR = "cluster/k8s/monitoring/mimir"
_NAMESPACE = "monitoring"
_SEAWEEDFS = "seaweedfs"
_SEAWEED_GROUP = "seaweed.seaweedfs.com"
_CREDENTIALS_SECRET = "mimir-seaweedfs-credentials"
_S3_ENDPOINT = "seaweedfs-s3.seaweedfs.svc:8333"
_ZONE_SELECTOR = {"topology.kubernetes.io/zone": "hil-ovh"}
# Prefer ordinary workers when this workload tolerates control planes.
_PREFER_WORKERS_NODE_AFFINITY = {
    "preferredDuringSchedulingIgnoredDuringExecution": [
        {
            "weight": 100,
            "preference": {
                "matchExpressions": [{"key": "node-role.kubernetes.io/control-plane", "operator": "DoesNotExist"}]
            },
        }
    ]
}


def _s3(bucket: str) -> dict[str, object]:
    # Credentials injected via global.extraEnvFrom; expanded by -config.expand-env=true.
    return {
        "endpoint": _S3_ENDPOINT,
        "bucket_name": bucket,
        "region": "us-east-1",
        "insecure": True,
        "access_key_id": "${AWS_ACCESS_KEY_ID}",
        "secret_access_key": "${AWS_SECRET_ACCESS_KEY}",
    }


def _component(replicas: int, cpu: str, memory: str, **extra: object) -> dict[str, object]:
    """A Mimir component pinned to the OVH workers, with requests only."""
    return {
        "replicas": replicas,
        **extra,
        "resources": {"requests": {"cpu": cpu, "memory": memory}},
        "nodeSelector": _ZONE_SELECTOR,
        "affinity": {"nodeAffinity": _PREFER_WORKERS_NODE_AFFINITY},
    }


def _storage(chart: Chart) -> None:
    # Tenant-local ownership for Mimir's existing Seaweed buckets and credentials.
    # The old seaweedfs-namespace resources remain until the consumer cutover and
    # data-path verification are complete.
    for bucket, description in (("mimir-blocks", "Mimir blocks."), ("mimir-ruler", "Mimir ruler state.")):
        Bucket(
            chart,
            bucket,
            metadata=metadata(bucket, _NAMESPACE, annotations={"description": description}),
            spec=BucketSpec(
                name=bucket,
                adopt_existing=True,
                cluster_ref=BucketSpecClusterRef(name=_SEAWEEDFS, namespace=_SEAWEEDFS),
                reclaim_policy=BucketSpecReclaimPolicy.RETAIN,
                access=[
                    BucketSpecAccess(
                        user=NAME,
                        actions=[
                            BucketSpecAccessActions.READ,
                            BucketSpecAccessActions.WRITE,
                            BucketSpecAccessActions.LIST,
                            BucketSpecAccessActions.TAGGING,
                        ],
                    )
                ],
            ),
        )
    S3Credentials(
        chart,
        "credentials",
        metadata=metadata(NAME, _NAMESPACE, annotations={"description": "Mimir's tenant-local SeaweedFS credentials."}),
        spec=S3CredentialsSpec(
            seaweed_ref=S3CredentialsSpecSeaweedRef(name=_SEAWEEDFS, namespace=_SEAWEEDFS),
            # The IAM identity name is cluster-global. Without a same-namespace
            # S3Identity, the operator uses the existing identity named mimir.
            identity_ref=S3CredentialsSpecIdentityRef(name=NAME),
            # Use a new Secret during the staged handoff. The existing Secret is
            # populated by the old cross-namespace S3Credentials object and cannot be
            # adopted here.
            secret_ref=S3CredentialsSpecSecretRef(
                name=_CREDENTIALS_SECRET, access_key_field="AWS_ACCESS_KEY_ID", secret_key_field="AWS_SECRET_ACCESS_KEY"
            ),
            reclaim_policy=S3CredentialsSpecReclaimPolicy.RETAIN,
        ),
    )
    # Permit only Mimir's tenant-local Buckets and S3Credentials to reference the
    # SeaweedFS cluster in its namespace.
    ResourceReferenceGrant(
        chart,
        "seaweed-grant",
        metadata=metadata(NAME, _SEAWEEDFS),
        spec=ResourceReferenceGrantSpec(
            from_=[
                ResourceReferenceGrantSpecFrom(group=_SEAWEED_GROUP, kind="Bucket", namespace=_NAMESPACE),
                ResourceReferenceGrantSpecFrom(group=_SEAWEED_GROUP, kind="S3Credentials", namespace=_NAMESPACE),
            ],
            to=[ResourceReferenceGrantSpecTo(group=_SEAWEED_GROUP, kind="Seaweed", name=_SEAWEEDFS)],
        ),
    )
    # Retain the previous credential Secret during the staged handoff. The old
    # S3Credentials resource is retired separately; revoking its retained key and
    # removing this rollback Secret is an explicit follow-up.
    k8s.KubeSecret(
        chart,
        "legacy-credentials",
        metadata=k8s.ObjectMeta(
            name="mimir-s3-credentials", namespace=_NAMESPACE, annotations={"kustomize.toolkit.fluxcd.io/ssa": "Merge"}
        ),
        type="Opaque",
    )
    S3Identity(
        chart,
        "identity",
        metadata=metadata(NAME, _SEAWEEDFS),
        spec=S3IdentitySpec(
            seaweed_ref=S3IdentitySpecSeaweedRef(name=_SEAWEEDFS), reclaim_policy=S3IdentitySpecReclaimPolicy.RETAIN
        ),
    )


def _values() -> dict[str, object]:
    return {
        "global": {"extraEnvFrom": [{"secretRef": {"name": _CREDENTIALS_SECRET}}]},
        "minio": {"enabled": False},
        "kafka": {"enabled": False},
        # S3 storage configuration via structuredConfig overrides.
        "mimir": {
            "structuredConfig": {
                "common": {"storage": {"backend": "s3", "s3": _s3("mimir-blocks")}},
                "blocks_storage": {"s3": {"bucket_name": "mimir-blocks"}},
                # Chart 6.x defaults to Kafka ingest storage. Disable — at this
                # scale (2 ingesters, no Kafka) the decoupled write path isn't
                # worth the operator + Kafka cluster overhead.
                "ingest_storage": {"enabled": False},
                "ingester": {
                    "push_grpc_method_enabled": True,
                    # 2 ingesters on the 2 OVH kimsufi workers with RF=2:
                    # every sample lives on both pods, so we can drain a node
                    # and the survivor still serves writes + reads.
                    "ring": {"replication_factor": 2},
                },
                "multitenancy_enabled": False,
                "ruler": {
                    # `dns+` and the *headless* service, not the ClusterIP one, on purpose.
                    # Alertmanager HA expects the sender to fan out to every replica: all
                    # instances receive all alerts and gossip decides which one notifies.
                    # A ClusterIP URL is a load balancer -- kube-proxy picks one backend per
                    # connection, so every notification landed on a single pod. Observed
                    # 2026-08-06: alertmanager-monitoring-0 held 28 alerts while -1 held 0,
                    # both reporting `cluster: ready, peers: 2`, so nothing looked wrong.
                    # `dns+` resolves the headless service's A records to every pod IP;
                    # alertmanager_refresh_interval (default 1m) re-resolves as pods roll.
                    "alertmanager_url": "dns+http://alertmanager-operated.monitoring.svc.cluster.local:9093/",
                    "rule_path": "/data/rules",
                },
                "ruler_storage": {"backend": "s3", "s3": _s3("mimir-ruler")},
                "limits": {
                    "compactor_blocks_retention_period": "365d",
                    "out_of_order_time_window": "30m",
                    "max_global_series_per_user": 0,
                    "ingestion_rate": 100000,
                    "ingestion_burst_size": 500000,
                    # Mimir defaults this to 20; kube-prometheus-stack's `node-exporter`
                    # group ships 26 alerts, so the whole group was rejected on every sync
                    # (`per-user rules per rule group limit (limit: 20 actual: 26)
                    # exceeded`) and silently never loaded — costing all the node-level
                    # alerts: NodeFilesystem{SpaceFillingUp,AlmostOutOfSpace,...},
                    # NodeRAID{Degraded,DiskFailure}, NodeClock*, NodeFileDescriptorLimit.
                    # Every other group stayed under the limit and evaluated fine, so the
                    # only symptom was a 5-minutely error in the Alloy log.
                    # Headroom, not unlimited (0 disables), so a runaway group still trips.
                    "ruler_max_rules_per_rule_group": 60,
                },
            }
        },
        # --- Component replicas (minimal for small cluster) ---
        # All pinned to the OVH kimsufi workers. No CP tolerations — workers only.
        # No explicit limits — Goldilocks/VPA (mode: initial) manages them.
        "distributor": _component(1, "50m", "128Mi"),
        "ingester": {
            "replicas": 2,
            "persistentVolume": {"storageClass": "local-path-ovh", "size": "10Gi"},
            "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}},
            "nodeSelector": _ZONE_SELECTOR,
            # 2 ingesters + RF=2 + RF=2 on the 2 kimsufi workers gives all-pairs
            # placement; zone-aware would just add a second StatefulSet and
            # rollout-operator coupling for no behavioural win at this scale.
            "zoneAwareReplication": {"enabled": False},
            "affinity": {
                "nodeAffinity": _PREFER_WORKERS_NODE_AFFINITY,
                "podAntiAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "labelSelector": {
                                "matchLabels": {
                                    "app.kubernetes.io/component": "ingester",
                                    "app.kubernetes.io/name": NAME,
                                    "app.kubernetes.io/instance": NAME,
                                }
                            },
                            "topologyKey": "kubernetes.io/hostname",
                        }
                    ]
                },
            },
        },
        "querier": _component(1, "50m", "128Mi"),
        "query_frontend": _component(1, "50m", "128Mi"),
        "query_scheduler": _component(1, "10m", "64Mi"),
        # Compactor's local disk is scratch for compaction passes; a lost
        # in-progress compaction just re-runs next cycle. No reason to back
        # it with a real PVC.
        "compactor": _component(1, "50m", "128Mi", persistentVolume={"enabled": False}),
        "store_gateway": {
            # Store-gateway's local disk is just bucket-index cache; rebuilt
            # from S3 on cold start (a few minutes of warm-up). Run on emptyDir.
            **_component(1, "50m", "128Mi", persistentVolume={"enabled": False, "emptyDir": {"sizeLimit": "10Gi"}}),
            "zoneAwareReplication": {"enabled": False},
        },
        # The gateway is nginx and serves /ready/proxy routes, not Mimir's
        # Prometheus /metrics endpoint. Keep it out of the chart-generated
        # ServiceMonitor so TargetDown only covers real Mimir components.
        "gateway": _component(1, "10m", "32Mi", service={"labels": {"prometheus.io/service-monitor": "false"}}),
        "ruler": {"enabled": True, **_component(1, "50m", "128Mi")},
        # --- Disabled components ---
        "alertmanager": {"enabled": False},
        "chunks-cache": {"enabled": False},
        "index-cache": {"enabled": False},
        "metadata-cache": {"enabled": False},
        "results-cache": {"enabled": False},
        "metaMonitoring": {
            "serviceMonitor": {"enabled": True},
            "dashboards": {"enabled": False},
            "grafanaAgent": {"enabled": False},
        },
        "rollout_operator": {"enabled": False},
        "overrides_exporter": {"enabled": False},
        "continuous_test": {"enabled": False},
    }


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    _storage(chart)
    HelmRelease(
        chart,
        "helm-release",
        metadata=metadata(NAME, _NAMESPACE),
        spec=HelmReleaseSpec(
            interval="30m",
            timeout="10m",
            install=HelmReleaseSpecInstall(remediation=HelmReleaseSpecInstallRemediation(retries=3)),
            chart=HelmReleaseSpecChart(
                spec=HelmReleaseSpecChartSpec(
                    chart="mimir-distributed",
                    # renovate: datasource=helm depName=mimir-distributed registryUrl=https://grafana.github.io/helm-charts
                    version="6.x",
                    source_ref=HelmReleaseSpecChartSpecSourceRef(
                        kind=HelmReleaseSpecChartSpecSourceRefKind.HELM_REPOSITORY,
                        name="grafana",
                        namespace="flux-system",
                    ),
                    interval="12h",
                )
            ),
            values=_values(),
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


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

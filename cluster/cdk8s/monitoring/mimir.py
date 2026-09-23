"""Mimir (metrics storage and the ruler) with its tenant-local SeaweedFS buckets and
credentials."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release
from cluster.cdk8s.monitoring import grafana_helmrepository
from cluster.cdk8s.seaweedfs import s3

NAME = "mimir"
OUTPUT_DIR = "cluster/k8s/monitoring/mimir"
_NAMESPACE = "monitoring"
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
    identity = s3.Identity(chart, "identity", name=NAME)
    for bucket, description in (("mimir-blocks", "Mimir blocks."), ("mimir-ruler", "Mimir ruler state.")):
        s3.Bucket(
            chart,
            bucket,
            name=bucket,
            namespace=_NAMESPACE,
            adopt_existing=True,
            description=description,
            grant_name=NAME,
        ).grant_read_write(identity)
    identity.credentials(
        namespace=_NAMESPACE,
        # A new Secret during the staged handoff: the existing one is populated by the old
        # cross-namespace S3Credentials object and cannot be adopted here.
        secret=_CREDENTIALS_SECRET,
        key_fields=s3.AWS_ENV_KEY_FIELDS,
        description="Mimir's tenant-local SeaweedFS credentials.",
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
    helm_release(
        chart,
        NAME,
        _NAMESPACE,
        repository=grafana_helmrepository.SOURCE_REF,
        chart="mimir-distributed",
        version="6.x",
        interval="30m",
        chart_interval="12h",
        timeout="10m",
        install=RETRY_FAILED_INSTALL,
        values=_values(),
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
        artifact,
        wait=None,
        suspend=False,
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
    )

"""Loki (SimpleScalable) with its SeaweedFS bucket, identity, credentials and CiliumNetworkPolicy.

Grafana reaches Loki internally via the cluster Service.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks

from cluster.cdk8s import cilium, namespaces, node_scheduling, piraeus_storage
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.monitoring import alloy_endpoints, grafana_helmrepository
from cluster.cdk8s.namespaces import PRIVILEGED_POD_SECURITY, Vpa
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, IngressRule, NetworkPolicy
from cluster.cdk8s.seaweedfs import s3
from cluster.cdk8s.seaweedfs.endpoints import S3
from cluster.cdk8s.vpa_policy import (
    VPA_RESOURCE_POLICY_ANNOTATION,
    ContainerPolicy,
    ControlledValues,
    resource_policy_annotation,
)

NAME = "loki"
OUTPUT_DIR = f"{GENERATED_ROOT}/monitoring/loki"
# Written by the tenant-local S3Credentials; what the Loki pods read.
_CREDENTIALS_SECRET = "loki-seaweedfs-credentials"
WRITE_URL = "http://loki-write.loki.svc.cluster.local:3100"
READ_URL = "http://loki-read.loki.svc.cluster.local:3100"
PUSH_URL = f"{WRITE_URL}/loki/api/v1/push"
_CREDENTIALS_ENV_FROM = [{"secretRef": {"name": _CREDENTIALS_SECRET}}]
_GOLDILOCKS_OFF = {"goldilocks.fairwinds.com/enabled": "false"}


def _storage(chart: Chart) -> None:
    namespaces.namespace(chart, "namespace", name=NAME, vpa=Vpa.INITIAL, labels=PRIVILEGED_POD_SECURITY)
    # Single bucket "loki" carrying chunks, ruler, and admin sub-paths
    # (Loki splits them internally by key prefix). See the HelmRelease's
    # `storage.bucketNames` — all three point at the same bucket.
    s3.PrivateBucket(
        chart,
        "storage",
        name=NAME,
        tenant=NAME,
        adopt_existing=True,
        description="Loki chunks, ruler, and admin objects.",
        secret_name=_CREDENTIALS_SECRET,
    )


def _loki_values() -> dict[str, object]:
    return {
        "deploymentMode": "SimpleScalable",
        "loki": {
            "auth_enabled": False,
            "commonConfig": {"replication_factor": 2},
            "storage": {
                "type": "s3",
                "s3": {"endpoint": S3.url, "region": "us-east-1", "s3ForcePathStyle": True, "insecure": True},
                "bucketNames": {"chunks": NAME, "ruler": NAME, "admin": NAME},
            },
            "schemaConfig": {
                "configs": [
                    {
                        "from": "2026-04-06",
                        "store": "tsdb",
                        "object_store": "s3",
                        "schema": "v13",
                        "index": {"prefix": "loki_index_", "period": "24h"},
                    }
                ]
            },
            "limits_config": {
                # Keep the central log store bounded to roughly three months. Loki
                # durations use hours rather than a calendar-month unit.
                "retention_period": "2160h",
                "reject_old_samples": True,
                "reject_old_samples_max_age": "168h",
                "ingestion_rate_mb": 10,
                "ingestion_burst_size_mb": 20,
                "per_stream_rate_limit": "5MB",
                "per_stream_rate_limit_burst": "10MB",
                # Enables /loki/api/v1/index/volume, which the Grafana Loki
                # Explore app uses for its log-volume sparkline and label
                # drilldowns. Without it the UI shows "Log volume has not been
                # configured".
                "volume_enabled": True,
            },
            # The default replay ceiling is 4 GiB, above the writer's cgroup
            # limit. Bound replay so recovery can flush before it exhausts memory.
            "ingester": {"wal": {"replay_memory_ceiling": "1GB"}},
            "compactor": {
                "retention_enabled": True,
                "retention_delete_delay": "2h",
                "compaction_interval": "10m",
                "delete_request_store": "s3",
            },
        },
        "singleBinary": {"replicas": 0},
        # SimpleScalable: write owns the ingester WAL (needs PVC), backend's disk
        # is shipper/compactor cache (emptyDir is fine), read is stateless.
        # All pinned to OVH kimsufi zone, where the Piraeus HDD pools are.
        "write": {
            "replicas": 2,
            # Initial-mode VPA normally scales limits with requests. Keep the
            # cgroup limit aligned with GOMEMLIMIT and WAL replay while VPA sizes
            # requests for scheduling, including enough memory for recovery.
            "annotations": {
                VPA_RESOURCE_POLICY_ANNOTATION: resource_policy_annotation(
                    ContainerPolicy(
                        container_name="loki",
                        controlled_values=ControlledValues.REQUESTS_ONLY,
                        min_allowed={"memory": "1Gi"},
                        max_allowed={"memory": "2Gi"},
                    ),
                    compact=True,  # Preserve the Helm values byte for byte.
                )
            },
            "nodeSelector": node_scheduling.HIL_OVH_NODE_SELECTOR,
            "affinity": node_scheduling.PREFER_WORKERS,
            "persistence": {"storageClass": piraeus_storage.HDD_LOCAL, "size": "10Gi"},
            "extraEnvFrom": _CREDENTIALS_ENV_FROM,
            "extraEnv": [{"name": "GOMEMLIMIT", "value": "1536MiB"}],
            "resources": {"requests": {"cpu": "100m", "memory": "1Gi"}, "limits": {"cpu": "500m", "memory": "2Gi"}},
        },
        "read": {
            # The chart rolls one unavailable Pod at a time. Keep two reads available
            # through rollouts and voluntary evictions across the three OVH workers.
            "replicas": 3,
            "maxUnavailable": 1,
            "annotations": {**_GOLDILOCKS_OFF, namespaces.VPA_UPDATE_MODE_LABEL: Vpa.AUTO},
            "nodeSelector": node_scheduling.HIL_OVH_NODE_SELECTOR,
            "affinity": node_scheduling.PREFER_WORKERS,
            "extraEnvFrom": _CREDENTIALS_ENV_FROM,
            # See upstream issues grafana/loki#23514 and #19586: Loki does not derive
            # `GOMEMLIMIT` from its cgroup limit, so set it to ~80% of the hard
            # cap. `read` combines query-frontend and querier; broad queries can decompress
            # matching chunks before filtering, so memory tracks data scanned (#4763; the
            # query cause in that incident was unconfirmed).
            "extraEnv": [{"name": "GOMEMLIMIT", "value": "3277MiB"}],
            "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "4Gi"}},
        },
        "backend": {
            "replicas": 2,
            "annotations": _GOLDILOCKS_OFF,
            "nodeSelector": node_scheduling.HIL_OVH_NODE_SELECTOR,
            "affinity": node_scheduling.PREFER_WORKERS,
            "persistence": {"volumeClaimsEnabled": False},
            "extraEnvFrom": _CREDENTIALS_ENV_FROM,
            # Backend memory follows the retained index, not active ingester streams;
            # its old 256Mi cap caused 165 OOM kills. Dropping Promtail's `filename`
            # label limits future growth, but the existing index ages out only through
            # retention (ducktape#4763).
            "resources": {"requests": {"cpu": "50m", "memory": "256Mi"}, "limits": {"cpu": "500m", "memory": "1Gi"}},
        },
        "gateway": {
            "enabled": True,
            # Loki canary's default target is loki-gateway. Keep the internal gateway
            # enabled for read/write API routing; the chart labels the nginx gateway
            # service out of ServiceMonitor discovery.
            "replicas": 1,
            "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}},
            "nodeSelector": node_scheduling.HIL_OVH_NODE_SELECTOR,
            "affinity": node_scheduling.PREFER_WORKERS,
            # nginx's fixed UDP source port can stay conntrack-pinned to a dead
            # CoreDNS pod after rescheduling. dnsmasq opens fresh flows and forwards
            # through the kubelet-injected resolver; see
            # `cluster/docs/lessons_learned/2026_08_26_loki_gateway_nginx_dns_conntrack_pinning.md`.
            "nginxConfig": {"resolver": "127.0.0.1:8053"},
            "extraContainers": [
                {
                    "name": "dnsmasq",
                    "image": "4km3/dnsmasq:2.90-r3-alpine-3.22.2",
                    "args": ["-k", "--no-hosts", "--listen-address=127.0.0.1", "--port=8053", "--cache-size=150"],
                    "ports": [{"name": "dns", "containerPort": 8053, "protocol": "UDP"}],
                    "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                    "resources": {
                        "requests": {"cpu": "5m", "memory": "16Mi"},
                        "limits": {"cpu": "50m", "memory": "64Mi"},
                    },
                }
            ],
        },
        # Disable bundled monitoring (we have kube-prometheus-stack)
        "monitoring": {
            "serviceMonitor": {"enabled": True},
            "dashboards": {"enabled": False},
            "rules": {"enabled": False},
            "selfMonitoring": {"enabled": False, "grafanaAgent": {"installOperator": False}},
        },
        "test": {"enabled": False},
        "chunksCache": {"enabled": False},
        "resultsCache": {"enabled": False},
    }


def _helm_releases(chart: Chart) -> None:
    helm_release(
        chart,
        NAME,
        NAME,
        repository=grafana_helmrepository.SOURCE_REF,
        chart="loki",
        version="7.x",
        interval="30m",
        chart_interval="12h",
        install=RETRY_FAILED_INSTALL,
        values=_loki_values(),
    )


def _network_policy(chart: Chart) -> None:
    namespace_label = "k8s:io.kubernetes.pod.namespace"
    loki_pods = {"app.kubernetes.io/name": NAME, namespace_label: NAME}
    # Loki has auth disabled: any pod reaching port 3100 can read, write, or delete
    # every cluster log. These per-consumer rules are its network access boundary.
    NetworkPolicy(
        chart,
        "network-policy",
        metadata=ApiObjectMetadata(name="ingress", namespace=NAME),
        endpoint_selector={"app.kubernetes.io/name": NAME},
        ingress=[
            # Alloy Talos-log receiver → Loki (Talos node logs). The receiver runs in
            # hostNetwork so it can bind only 127.0.0.1:13333; Cilium represents it as
            # host on the local node and remote-node after a cross-node service hop.
            IngressRule.from_entities(Entity.HOST, Entity.REMOTE_NODE, ports=(3100,)),
            # Grafana → Loki (log queries)
            IngressRule.from_endpoints({"app": "grafana", namespace_label: "monitoring"}, ports=(3100,)),
            # Props backend → Loki (GET /api/runs/{id}/logs reads agent container logs)
            IngressRule.from_endpoints(
                {
                    "app.kubernetes.io/component": "backend",
                    "app.kubernetes.io/instance": "props",
                    namespace_label: "props",
                },
                ports=(3100,),
            ),
            # Central Alloy → Loki (OTel log forwarding), the syslog Alloy → Loki (the home switch's
            # logs); the per-node Alloys scrape the components' metrics on the same port.
            IngressRule.from_endpoints(
                alloy_endpoints.CENTRAL.cilium,
                alloy_endpoints.NODE.cilium,
                alloy_endpoints.SYSLOG.cilium,
                alloy_endpoints.JOURNAL.cilium,
                ports=(3100,),
            ),
            # Per-node Alloy → Loki canary metrics (ServiceMonitor scraping)
            alloy_endpoints.NODE.admit(3500),
            # Gatus → Loki (health checks)
            cilium.PROBER.admit(3100),
            # Authentik proxy outpost → Loki (SSO-protected external access)
            IngressRule.from_endpoints({namespace_label: "authentik"}, ports=(3100,)),
            # Haku's read-only, namespace-filtered proxy queries `loki-read` directly,
            # enforcing its validator/allowlist without the gateway resolver path (#4750).
            IngressRule.from_endpoints(
                {"app.kubernetes.io/name": "loki-read-proxy", namespace_label: "loki-read-proxy"}, ports=(3100,)
            ),
            # Loki ↔ Loki (SimpleScalable read/write/backend, gateway, and canary)
            IngressRule.from_endpoints(loki_pods, ports=(80, 8080, 3100, 9095, 7946)),
        ],
        egress=[
            # Loki → kube-apiserver (sc-rules sidecar watches secrets)
            EgressRule.to_entities(Entity.KUBE_APISERVER, ports=(443, 6443)),
            # Loki → kube-dns (DNS resolution)
            cilium.dns_egress(),
            # Loki → SeaweedFS S3 (S3 chunk/index storage)
            S3.egress(),
            # Loki ↔ Loki (SimpleScalable read/write/backend, gateway, and canary)
            EgressRule.to_endpoints(loki_pods, 80, 8080, 7946, 9095, 3100),
        ],
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    _storage(chart)
    _helm_releases(chart)
    _network_policy(chart)
    return chart


def loki(
    chart: Chart,
    directory: RenderedDirectory,
    seaweedfs_operator: Kustomization,
    monitoring_crds: Kustomization,
    *,
    health_checks: list[KustomizationSpecHealthChecks],
) -> Kustomization:
    return flux_kustomization(
        chart,
        "loki",
        directory,
        wait=None,
        health_checks=health_checks,
        timeout="10m",
        depends_on=flux_kustomization_depends_on_many(
            seaweedfs_operator,
            # the chart's monitoring.serviceMonitor
            monitoring_crds,
        ),
    )

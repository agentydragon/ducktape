"""Loki (SimpleScalable) with its SeaweedFS bucket, identity and credentials, the two promtail
DaemonSets shipping pod logs and the NixOS journal, and Loki's CiliumNetworkPolicy.

External access goes through the Authentik proxy outpost (native blueprint in
cluster/k8s/authentik/app/blueprints/): Gateway → ak-outpost-loki-outpost (auth) → loki
backend. Grafana reaches Loki internally via the cluster Service.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cilium_crds.io.cilium import (
    CiliumNetworkPolicySpecEgress,
    CiliumNetworkPolicySpecEgressToEndpoints,
    CiliumNetworkPolicySpecEgressToEntities,
    CiliumNetworkPolicySpecEgressToPorts,
    CiliumNetworkPolicySpecEgressToPortsPorts,
    CiliumNetworkPolicySpecEgressToPortsPortsProtocol,
    CiliumNetworkPolicySpecIngress,
    CiliumNetworkPolicySpecIngressFromEndpoints,
    CiliumNetworkPolicySpecIngressFromEntities,
    CiliumNetworkPolicySpecIngressToPorts,
    CiliumNetworkPolicySpecIngressToPortsPorts,
    CiliumNetworkPolicySpecIngressToPortsPortsProtocol,
)
from flux_helm.io.fluxcd.toolkit.helm import HelmReleaseSpecUpgrade
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks
from python.runfiles import Runfiles

from cluster.cdk8s import cilium, namespaces, node_scheduling
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.monitoring import grafana_helmrepository
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.providers.cilium.network_policy import NetworkPolicy
from cluster.cdk8s.seaweedfs import s3

NAME = "loki"
OUTPUT_DIR = f"{GENERATED_ROOT}/monitoring/loki"
_SEAWEEDFS = "seaweedfs"
# Written by the tenant-local S3Credentials; what the Loki pods read.
_CREDENTIALS_SECRET = "loki-seaweedfs-credentials"
WRITE_URL = "http://loki-write.loki.svc.cluster.local:3100"
READ_URL = "http://loki-read.loki.svc.cluster.local:3100"
PUSH_URL = f"{WRITE_URL}/loki/api/v1/push"
_CREDENTIALS_ENV_FROM = [{"secretRef": {"name": _CREDENTIALS_SECRET}}]
_GOLDILOCKS_OFF = {"goldilocks.fairwinds.com/enabled": "false"}
_TOLERATE_NO_SCHEDULE = [{"effect": "NoSchedule", "operator": "Exists"}]
# `maxUnavailable` must exceed the roaming-node count: an offline pod can hold the
# rollout budget indefinitely. `//cluster/cdk8s/monitoring:test_roaming_daemonset_capacity`
# derives that count from `nebula-mesh.json`; see
# `cluster/docs/lessons_learned/2026_07_31_promtail_daemonset_roaming_deadlock.md`.
_ROAMING_SAFE_UPDATE_STRATEGY = {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 3}}


def _storage(chart: Chart) -> None:
    namespaces.namespace(
        chart,
        "namespace",
        name=NAME,
        vpa=Vpa.INITIAL,
        labels={
            "pod-security.kubernetes.io/enforce": "privileged",
            "pod-security.kubernetes.io/audit": "privileged",
            "pod-security.kubernetes.io/warn": "privileged",
        },
    )
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
                "s3": {
                    "endpoint": "http://seaweedfs-s3.seaweedfs.svc:8333",
                    "region": "us-east-1",
                    "s3ForcePathStyle": True,
                    "insecure": True,
                },
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
                "goldilocks.fairwinds.com/vpa-resource-policy": (
                    '{"containerPolicies":[{"containerName":"loki","controlledValues":"RequestsOnly",'
                    '"minAllowed":{"memory":"1Gi"},"maxAllowed":{"memory":"2Gi"}}]}'
                )
            },
            "nodeSelector": node_scheduling.HIL_OVH_NODE_SELECTOR,
            "affinity": node_scheduling.PREFER_WORKERS,
            "persistence": {"storageClass": "piraeus-hdd-local", "size": "10Gi"},
            "extraEnvFrom": _CREDENTIALS_ENV_FROM,
            "extraEnv": [{"name": "GOMEMLIMIT", "value": "1536MiB"}],
            "resources": {"requests": {"cpu": "100m", "memory": "1Gi"}, "limits": {"cpu": "500m", "memory": "2Gi"}},
        },
        "read": {
            "replicas": 2,
            "annotations": _GOLDILOCKS_OFF,
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


def _promtail_values() -> dict[str, object]:
    return {
        "updateStrategy": _ROAMING_SAFE_UPDATE_STRATEGY,
        "config": {
            "clients": [
                # SimpleScalable writes through loki-write; the old single-binary
                # `loki` Service no longer exists.
                {"url": PUSH_URL}
            ],
            "snippets": {
                # Drop the default `filename` label: its per-Pod UID churns streams,
                # growing the retained index; cardinality, not ingest volume, drives
                # that cost, while namespace/pod/container already identify the source.
                # Drop it in a pipeline stage because Promtail
                # adds it after relabeling. `pipelineStages` replaces chart defaults,
                # so keep the `cri` stage here too.
                "pipelineStages": [{"cri": {}}, {"labeldrop": ["filename"]}]
            },
        },
        "resources": {
            "requests": {
                "cpu": "50m",
                # cgroup v2 charges tailed-file page cache here; a 128Mi cap caused
                # eviction/re-read thrash that starved etcd. Keep ample headroom above
                # the tail working set; measurements are in
                # `cluster/docs/lessons_learned/2026_06_19_etcd_hdd_io_contention.md`.
                "memory": "256Mi",
            },
            "limits": {"cpu": "500m", "memory": "1Gi"},
        },
        "tolerations": _TOLERATE_NO_SCHEDULE,
    }


def _promtail_journal_values() -> dict[str, object]:
    return {
        # Restrict to systemd/journald nodes; label set in nix/nixos/hosts/*/default.nix.
        "nodeSelector": {"node-vendor": "nixos"},
        "tolerations": _TOLERATE_NO_SCHEDULE,
        # The NixOS-only selector includes offline roaming laptops; the shared
        # rollout budget must exceed their count so they cannot block an update.
        "updateStrategy": _ROAMING_SAFE_UPDATE_STRATEGY,
        "config": {
            "clients": [{"url": PUSH_URL}],
            "snippets": {
                # Main Promtail already collects pod logs on every node; avoid duplicates.
                "scrapeConfigs": "",
                "extraScrapeConfigs": (
                    Runfiles.CreateOrRaise().root() / "ducktape/cluster/cdk8s/monitoring/journal-scrape.yaml"
                ).read_text(),
            },
        },
        "extraVolumes": [
            {"name": "journal", "hostPath": {"path": "/var/log/journal"}},
            {"name": "machine-id", "hostPath": {"path": "/etc/machine-id"}},
        ],
        "extraVolumeMounts": [
            {"name": "journal", "mountPath": "/var/log/journal", "readOnly": True},
            {"name": "machine-id", "mountPath": "/etc/machine-id", "readOnly": True},
        ],
        "resources": {
            "requests": {
                "cpu": "50m",
                # Same cgroup page-cache/etcd I/O constraint as pod-log Promtail above.
                "memory": "256Mi",
            },
            "limits": {"cpu": "500m", "memory": "512Mi"},
        },
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
    helm_release(
        chart,
        "promtail",
        NAME,
        repository=grafana_helmrepository.SOURCE_REF,
        chart="promtail",
        version="6.x",
        interval="30m",
        chart_interval="12h",
        install=RETRY_FAILED_INSTALL,
        upgrade=HelmReleaseSpecUpgrade(
            # Helm otherwise waits on Pending/Terminating pods at offline roaming nodes;
            # this is separate from the DaemonSet rollout budget above.
            disable_wait=True
        ),
        values=_promtail_values(),
    )
    # Journal-only deviation: keep NixOS kernel events (including GPU Xid 79) in
    # cluster-retained Loki logs beyond local journal retention. Talos uses
    # `cluster/cdk8s/vector_talos_logs.py`; pod-log Promtail remains the all-node collector.
    helm_release(
        chart,
        "promtail-journal",
        NAME,
        repository=grafana_helmrepository.SOURCE_REF,
        chart="promtail",
        version="6.x",
        interval="30m",
        chart_interval="12h",
        install=RETRY_FAILED_INSTALL,
        upgrade=HelmReleaseSpecUpgrade(
            # Same Helm-wait issue as pod-log Promtail above.
            disable_wait=True
        ),
        values=_promtail_journal_values(),
    )


def _ingress_tcp(*ports: str) -> list[CiliumNetworkPolicySpecIngressToPorts]:
    return [
        CiliumNetworkPolicySpecIngressToPorts(
            ports=[
                CiliumNetworkPolicySpecIngressToPortsPorts(
                    port=port, protocol=CiliumNetworkPolicySpecIngressToPortsPortsProtocol.TCP
                )
                for port in ports
            ]
        )
    ]


def _from_pods(labels: dict[str, str]) -> list[CiliumNetworkPolicySpecIngressFromEndpoints]:
    return [CiliumNetworkPolicySpecIngressFromEndpoints(match_labels=labels)]


def _egress_ports(
    *ports: tuple[str, CiliumNetworkPolicySpecEgressToPortsPortsProtocol],
) -> list[CiliumNetworkPolicySpecEgressToPorts]:
    return [
        CiliumNetworkPolicySpecEgressToPorts(
            ports=[CiliumNetworkPolicySpecEgressToPortsPorts(port=port, protocol=protocol) for port, protocol in ports]
        )
    ]


def _network_policy(chart: Chart) -> None:
    namespace_label = "k8s:io.kubernetes.pod.namespace"
    tcp = CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP
    udp = CiliumNetworkPolicySpecEgressToPortsPortsProtocol.UDP
    loki_pods = {"app.kubernetes.io/name": NAME, namespace_label: NAME}
    # Loki has auth disabled: any pod reaching port 3100 can read, write, or delete
    # every cluster log. These per-consumer rules are its network access boundary.
    NetworkPolicy(
        chart,
        "network-policy",
        metadata=ApiObjectMetadata(name="ingress", namespace=NAME),
        endpoint_selector={"app.kubernetes.io/name": NAME},
        ingress=[
            # Promtail → Loki (log push)
            CiliumNetworkPolicySpecIngress(
                from_endpoints=_from_pods({"app.kubernetes.io/name": "promtail", namespace_label: NAME}),
                to_ports=_ingress_tcp("3100"),
            ),
            # Vector Talos-log receiver → Loki (Talos node logs). The receiver runs in
            # hostNetwork so it can bind only 127.0.0.1:13333; Cilium represents it as
            # host on the local node and remote-node after a cross-node service hop.
            CiliumNetworkPolicySpecIngress(
                from_entities=[
                    CiliumNetworkPolicySpecIngressFromEntities.HOST,
                    CiliumNetworkPolicySpecIngressFromEntities.REMOTE_HYPHEN_NODE,
                ],
                to_ports=_ingress_tcp("3100"),
            ),
            # Grafana → Loki (log queries)
            CiliumNetworkPolicySpecIngress(
                from_endpoints=_from_pods({"app": "grafana", namespace_label: "monitoring"}),
                to_ports=_ingress_tcp("3100"),
            ),
            # Props backend → Loki (GET /api/runs/{id}/logs reads agent container logs)
            CiliumNetworkPolicySpecIngress(
                from_endpoints=_from_pods(
                    {
                        "app.kubernetes.io/component": "backend",
                        "app.kubernetes.io/instance": "props",
                        namespace_label: "props",
                    }
                ),
                to_ports=_ingress_tcp("3100"),
            ),
            # Central Alloy → Loki (OTel log forwarding), the syslog Alloy → Loki (the home switch's
            # logs); the per-node Alloys scrape the components' metrics on the same port.
            CiliumNetworkPolicySpecIngress(
                from_endpoints=[
                    *_from_pods({"app.kubernetes.io/name": "alloy", namespace_label: "monitoring"}),
                    *_from_pods({"app.kubernetes.io/name": "alloy-node", namespace_label: "monitoring"}),
                    *_from_pods({"app.kubernetes.io/name": "alloy-syslog", namespace_label: "monitoring"}),
                ],
                to_ports=_ingress_tcp("3100"),
            ),
            # Per-node Alloy → Loki canary metrics (ServiceMonitor scraping)
            CiliumNetworkPolicySpecIngress(
                from_endpoints=_from_pods({"app.kubernetes.io/name": "alloy-node", namespace_label: "monitoring"}),
                to_ports=_ingress_tcp("3500"),
            ),
            # Gatus → Loki (health checks)
            CiliumNetworkPolicySpecIngress(
                from_endpoints=_from_pods(cilium.PROBER.cilium), to_ports=_ingress_tcp("3100")
            ),
            # Authentik proxy outpost → Loki (SSO-protected external access)
            CiliumNetworkPolicySpecIngress(
                from_endpoints=_from_pods({namespace_label: "authentik"}), to_ports=_ingress_tcp("3100")
            ),
            # Haku's read-only, namespace-filtered proxy queries `loki-read` directly,
            # enforcing its validator/allowlist without the gateway resolver path (#4750).
            CiliumNetworkPolicySpecIngress(
                from_endpoints=_from_pods(
                    {"app.kubernetes.io/name": "loki-read-proxy", namespace_label: "loki-read-proxy"}
                ),
                to_ports=_ingress_tcp("3100"),
            ),
            # Loki ↔ Loki (SimpleScalable read/write/backend, gateway, and canary)
            CiliumNetworkPolicySpecIngress(
                from_endpoints=_from_pods(loki_pods), to_ports=_ingress_tcp("80", "8080", "3100", "9095", "7946")
            ),
        ],
        egress=[
            # Loki → kube-apiserver (sc-rules sidecar watches secrets)
            CiliumNetworkPolicySpecEgress(
                to_entities=[CiliumNetworkPolicySpecEgressToEntities.KUBE_HYPHEN_APISERVER],
                to_ports=_egress_ports(("443", tcp), ("6443", tcp)),
            ),
            # Loki → kube-dns (DNS resolution)
            CiliumNetworkPolicySpecEgress(
                to_endpoints=[
                    CiliumNetworkPolicySpecEgressToEndpoints(
                        match_labels={namespace_label: "kube-system", "k8s-app": "kube-dns"}
                    )
                ],
                to_ports=_egress_ports(("53", udp), ("53", tcp)),
            ),
            # Loki → SeaweedFS S3 (S3 chunk/index storage)
            CiliumNetworkPolicySpecEgress(
                to_endpoints=[
                    CiliumNetworkPolicySpecEgressToEndpoints(
                        match_labels={
                            "app.kubernetes.io/component": "s3",
                            "app.kubernetes.io/name": _SEAWEEDFS,
                            namespace_label: _SEAWEEDFS,
                        }
                    )
                ],
                to_ports=_egress_ports(("8333", tcp)),
            ),
            # Loki ↔ Loki (SimpleScalable read/write/backend, gateway, and canary)
            CiliumNetworkPolicySpecEgress(
                to_endpoints=[CiliumNetworkPolicySpecEgressToEndpoints(match_labels=loki_pods)],
                to_ports=_egress_ports(("80", tcp), ("8080", tcp), ("7946", tcp), ("9095", tcp), ("3100", tcp)),
            ),
        ],
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    _storage(chart)
    _helm_releases(chart)
    _network_policy(chart)
    return chart


def loki(
    chart: Chart, directory: RenderedDirectory, seaweedfs_operator: Kustomization, monitoring_crds: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        "loki",
        directory,
        wait=None,
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
        depends_on=flux_kustomization_depends_on_many(
            seaweedfs_operator,
            # the chart's monitoring.serviceMonitor
            monitoring_crds,
        ),
    )

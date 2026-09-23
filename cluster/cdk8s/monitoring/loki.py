"""Loki (SimpleScalable) with its SeaweedFS bucket and credentials, the two promtail
DaemonSets shipping pod logs and the NixOS journal, and Loki's CiliumNetworkPolicy.

External access goes through the Authentik proxy outpost (native blueprint in
cluster/k8s/authentik/app/blueprints/): Gateway → ak-outpost-loki-outpost (auth) → loki
backend. Grafana reaches Loki internally via the cluster Service.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from cilium_crds.io.cilium import (
    CiliumNetworkPolicy,
    CiliumNetworkPolicySpec,
    CiliumNetworkPolicySpecEgress,
    CiliumNetworkPolicySpecEgressToEndpoints,
    CiliumNetworkPolicySpecEgressToEntities,
    CiliumNetworkPolicySpecEgressToPorts,
    CiliumNetworkPolicySpecEgressToPortsPorts,
    CiliumNetworkPolicySpecEgressToPortsPortsProtocol,
    CiliumNetworkPolicySpecEndpointSelector,
    CiliumNetworkPolicySpecIngress,
    CiliumNetworkPolicySpecIngressFromEndpoints,
    CiliumNetworkPolicySpecIngressFromEntities,
    CiliumNetworkPolicySpecIngressToPorts,
    CiliumNetworkPolicySpecIngressToPortsPorts,
    CiliumNetworkPolicySpecIngressToPortsPortsProtocol,
)
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmRelease,
    HelmReleaseSpec,
    HelmReleaseSpecChart,
    HelmReleaseSpecChartSpec,
    HelmReleaseSpecChartSpecSourceRef,
    HelmReleaseSpecChartSpecSourceRefKind,
    HelmReleaseSpecInstall,
    HelmReleaseSpecInstallRemediation,
    HelmReleaseSpecUpgrade,
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
from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.metadata import metadata

NAME = "loki"
OUTPUT_DIR = "cluster/k8s/monitoring/loki"
_SEAWEEDFS = "seaweedfs"
_SEAWEED_GROUP = "seaweed.seaweedfs.com"
# Written by the old cross-namespace S3Credentials in seaweedfs.
_LEGACY_CREDENTIALS_SECRET = "loki-s3-credentials"
# Written by the tenant-local S3Credentials; what the Loki pods read.
_CREDENTIALS_SECRET = "loki-seaweedfs-credentials"
_BUCKET_ACTIONS = [
    BucketSpecAccessActions.READ,
    BucketSpecAccessActions.WRITE,
    BucketSpecAccessActions.LIST,
    BucketSpecAccessActions.TAGGING,
]
_PUSH_URL = "http://loki-write.loki.svc.cluster.local:3100/loki/api/v1/push"
_ZONE_SELECTOR = {"topology.kubernetes.io/zone": "hil-ovh"}
# Prefer ordinary workers when this workload tolerates control planes.
_PREFER_WORKERS_AFFINITY = {
    "nodeAffinity": {
        "preferredDuringSchedulingIgnoredDuringExecution": [
            {
                "weight": 100,
                "preference": {
                    "matchExpressions": [{"key": "node-role.kubernetes.io/control-plane", "operator": "DoesNotExist"}]
                },
            }
        ]
    }
}
_CREDENTIALS_ENV_FROM = [{"secretRef": {"name": _CREDENTIALS_SECRET}}]
_GOLDILOCKS_OFF = {"goldilocks.fairwinds.com/enabled": "false"}
_TOLERATE_NO_SCHEDULE = [{"effect": "NoSchedule", "operator": "Exists"}]
# Must exceed the roaming-node count, or an offline laptop's undeletable pod
# holds the whole unavailable budget and the rollout deadlocks silently.
# Enforced by //cluster/validation:test_roaming_daemonset_capacity (which
# derives the count from nebula-mesh.json); incident write-up in
# cluster/docs/lessons_learned/2026_07_31_promtail_daemonset_roaming_deadlock.md.
_ROAMING_SAFE_UPDATE_STRATEGY = {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 3}}


def _grafana_chart(chart: str, version: str) -> HelmReleaseSpecChart:
    return HelmReleaseSpecChart(
        spec=HelmReleaseSpecChartSpec(
            chart=chart,
            version=version,
            source_ref=HelmReleaseSpecChartSpecSourceRef(
                kind=HelmReleaseSpecChartSpecSourceRefKind.HELM_REPOSITORY, name="grafana", namespace="flux-system"
            ),
            interval="12h",
        )
    )


def _storage(chart: Chart) -> None:
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=NAME,
            labels={
                "goldilocks.fairwinds.com/enabled": "true",
                "goldilocks.fairwinds.com/vpa-update-mode": "initial",
                "rbac.ducktape.io/agent-readable-logs": "true",
                "pod-security.kubernetes.io/enforce": "privileged",
                "pod-security.kubernetes.io/audit": "privileged",
                "pod-security.kubernetes.io/warn": "privileged",
            },
        ),
    )
    # S3Credentials owns the credential values; Flux owns only this target shell.
    k8s.KubeSecret(
        chart,
        "legacy-credentials",
        metadata=k8s.ObjectMeta(
            name=_LEGACY_CREDENTIALS_SECRET, namespace=NAME, annotations={"kustomize.toolkit.fluxcd.io/ssa": "Merge"}
        ),
        type="Opaque",
    )
    # Permit only the SeaweedFS operator's S3Credentials resource to populate
    # this exact workload Secret across namespaces.
    ResourceReferenceGrant(
        chart,
        "legacy-credentials-grant",
        metadata=metadata(_LEGACY_CREDENTIALS_SECRET, NAME),
        spec=ResourceReferenceGrantSpec(
            from_=[ResourceReferenceGrantSpecFrom(group=_SEAWEED_GROUP, kind="S3Credentials", namespace=_SEAWEEDFS)],
            to=[ResourceReferenceGrantSpecTo(group="", kind="Secret", name=_LEGACY_CREDENTIALS_SECRET)],
        ),
    )
    S3Identity(
        chart,
        "identity",
        metadata=metadata(NAME, _SEAWEEDFS),
        spec=S3IdentitySpec(
            seaweed_ref=S3IdentitySpecSeaweedRef(name=_SEAWEEDFS), reclaim_policy=S3IdentitySpecReclaimPolicy.RETAIN
        ),
    )
    S3Credentials(
        chart,
        "legacy-credentials-source",
        metadata=metadata(NAME, _SEAWEEDFS),
        spec=S3CredentialsSpec(
            seaweed_ref=S3CredentialsSpecSeaweedRef(name=_SEAWEEDFS),
            identity_ref=S3CredentialsSpecIdentityRef(name=NAME),
            secret_ref=S3CredentialsSpecSecretRef(
                name=_LEGACY_CREDENTIALS_SECRET,
                namespace=NAME,
                access_key_field="AWS_ACCESS_KEY_ID",
                secret_key_field="AWS_SECRET_ACCESS_KEY",
            ),
            reclaim_policy=S3CredentialsSpecReclaimPolicy.RETAIN,
        ),
    )
    # Single bucket "loki" carrying chunks, ruler, and admin sub-paths
    # (Loki splits them internally by key prefix). See the HelmRelease's
    # `storage.bucketNames` — all three point at the same bucket.
    Bucket(
        chart,
        "legacy-bucket",
        metadata=metadata(NAME, _SEAWEEDFS),
        spec=BucketSpec(
            name=NAME,
            cluster_ref=BucketSpecClusterRef(name=_SEAWEEDFS, namespace=_SEAWEEDFS),
            access=[BucketSpecAccess(user=NAME, actions=_BUCKET_ACTIONS)],
        ),
    )
    # Tenant-local ownership for Loki's existing Seaweed bucket and credentials.
    # The old seaweedfs-namespace resources remain until the consumer cutover and
    # data-path verification are complete.
    Bucket(
        chart,
        "bucket",
        metadata=metadata(NAME, NAME, annotations={"description": "Loki chunks, ruler, and admin objects."}),
        spec=BucketSpec(
            name=NAME,
            adopt_existing=True,
            cluster_ref=BucketSpecClusterRef(name=_SEAWEEDFS, namespace=_SEAWEEDFS),
            reclaim_policy=BucketSpecReclaimPolicy.RETAIN,
            access=[BucketSpecAccess(user=NAME, actions=_BUCKET_ACTIONS)],
        ),
    )
    S3Credentials(
        chart,
        "credentials",
        metadata=metadata(NAME, NAME, annotations={"description": "Loki's tenant-local SeaweedFS credentials."}),
        spec=S3CredentialsSpec(
            seaweed_ref=S3CredentialsSpecSeaweedRef(name=_SEAWEEDFS, namespace=_SEAWEEDFS),
            # The IAM identity name is cluster-global. Without a same-namespace
            # S3Identity, the operator uses the existing identity named loki.
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
    # Permit only Loki's tenant-local Bucket and S3Credentials to reference the
    # SeaweedFS cluster in its namespace.
    ResourceReferenceGrant(
        chart,
        "seaweed-grant",
        metadata=metadata(NAME, _SEAWEEDFS),
        spec=ResourceReferenceGrantSpec(
            from_=[
                ResourceReferenceGrantSpecFrom(group=_SEAWEED_GROUP, kind="Bucket", namespace=NAME),
                ResourceReferenceGrantSpecFrom(group=_SEAWEED_GROUP, kind="S3Credentials", namespace=NAME),
            ],
            to=[ResourceReferenceGrantSpecTo(group=_SEAWEED_GROUP, kind="Seaweed", name=_SEAWEEDFS)],
        ),
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
        # All pinned to OVH kimsufi zone so the PVCs land on local-path-ovh.
        "write": {
            "replicas": 2,
            "annotations": _GOLDILOCKS_OFF,
            "nodeSelector": _ZONE_SELECTOR,
            "affinity": _PREFER_WORKERS_AFFINITY,
            "persistence": {"storageClass": "local-path-ovh", "size": "10Gi"},
            "extraEnvFrom": _CREDENTIALS_ENV_FROM,
            "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"cpu": "500m", "memory": "512Mi"}},
        },
        "read": {
            "replicas": 2,
            "annotations": _GOLDILOCKS_OFF,
            "nodeSelector": _ZONE_SELECTOR,
            "affinity": _PREFER_WORKERS_AFFINITY,
            "extraEnvFrom": _CREDENTIALS_ENV_FROM,
            # Loki doesn't derive GOMEMLIMIT from its own cgroup limit yet (open
            # upstream: grafana/loki#23514, grafana/loki#19586), so the Go GC never
            # gets a chance to work harder before the kernel OOM-kills the process —
            # it only ever sees the hard wall. Set it explicitly to ~80% of the
            # limit below so GC pressure ramps up first. `read` runs query-frontend
            # + querier in one process (-target=read) and is the component that hits
            # this: a wide/low-selectivity query has to fetch and decompress every
            # matching chunk before a line filter ever runs, so memory tracks data
            # scanned, not data returned. See ducktape#4763.
            "extraEnv": [{"name": "GOMEMLIMIT", "value": "3277MiB"}],
            "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "4Gi"}},
        },
        "backend": {
            "replicas": 2,
            "annotations": _GOLDILOCKS_OFF,
            "nodeSelector": _ZONE_SELECTOR,
            "affinity": _PREFER_WORKERS_AFFINITY,
            "persistence": {"volumeClaimsEnabled": False},
            "extraEnvFrom": _CREDENTIALS_ENV_FROM,
            # backend runs the compactor + index-gateway + ruler. Its memory tracks
            # the size of the *retained index* (streams ever created x retention),
            # not the active stream count the ingesters see — which is why it was the
            # component that died while loki-write survived. At 256Mi it was OOMKilled
            # 165 times (exit 137), peaking at exactly the limit; that peak is
            # censored, since working-set cannot be observed above the cap.
            # Raised alongside the promtail `filename` labeldrop that cuts index
            # growth at the source; the drop alone does not shrink the already-retained
            # index, which ages out over the retention period.
            "resources": {"requests": {"cpu": "50m", "memory": "256Mi"}, "limits": {"cpu": "500m", "memory": "1Gi"}},
        },
        "gateway": {
            "enabled": True,
            # Loki canary's default target is loki-gateway. Keep the internal gateway
            # enabled for read/write API routing; the chart labels the nginx gateway
            # service out of ServiceMonitor discovery.
            "replicas": 1,
            "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}},
            "nodeSelector": _ZONE_SELECTOR,
            "affinity": _PREFER_WORKERS_AFFINITY,
            # nginx's DNS resolver reuses one fixed UDP source port for every
            # query; Kubernetes' conntrack-based Service NAT then pins that flow
            # to whichever CoreDNS pod answered first and never re-evaluates it,
            # so nginx keeps querying a dead pod IP forever once CoreDNS
            # reschedules — only a gateway restart/reload re-triggers resolution.
            # See ducktape#4750. dnsmasq opens a fresh UDP flow per query instead,
            # so it never falls into this trap; it forwards via this pod's own
            # /etc/resolv.conf (the kube-dns ClusterIP, kubelet-injected), so no
            # DNS server address is hardcoded here.
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
        # Disable test pod
        "test": {"enabled": False},
        # Disable chunk cache (single binary, not needed)
        "chunksCache": {"enabled": False},
        "resultsCache": {"enabled": False},
    }


def _promtail_values() -> dict[str, object]:
    return {
        "updateStrategy": _ROAMING_SAFE_UPDATE_STRATEGY,
        "config": {
            "clients": [
                # SimpleScalable: push to the write StatefulSet's service.
                # The single-binary `loki` Service was removed when we switched
                # off SingleBinary mode.
                {"url": _PUSH_URL}
            ],
            "snippets": {
                # Drop `filename`. It is the chart default and carries the full
                # /var/log/pods/<ns>_<pod>_<uid>/<container>/0.log path, so the pod UID
                # changes on every restart, rollout, Job, and CronJob run — 6194
                # distinct values in 24h, against 59 KB/s of actual ingest. Loki memory
                # scales with stream cardinality, not volume, and the retained index is
                # O(streams created x retention): that churn is what OOMKilled
                # loki-backend 165 times while ingest stayed trivial.
                #
                # It is redundant — namespace + pod + container already identify the
                # source; the UID is the only thing `filename` adds.
                #
                # Must be a pipeline stage, not extraRelabelConfigs: promtail attaches
                # `filename` at file-target discovery, after relabeling, so a
                # relabel-stage drop does not see it. `cri: {}` is restated because it
                # is the chart's default pipelineStages value and overriding the key
                # replaces it wholesale.
                "pipelineStages": [{"cri": {}}, {"labeldrop": ["filename"]}]
            },
        },
        "resources": {
            "requests": {
                "cpu": "50m",
                # Promtail tails container logs; in cgroup v2 the page cache for every
                # tailed file is charged to this container. A tight memory limit forces
                # constant eviction + readahead re-reads from disk (observed 2026-06-08:
                # 128Mi cap -> 23.8 TB physical reads for 66 MB logical, pegging the
                # node's system disk at 94% util and starving co-located etcd's WAL
                # fsync -> apiserver<->etcd latency -> NodeReady flaps). Keep the limit
                # well above the tail working set. See
                # cluster/debug/2026-06-10-etcd-io-contention/promtail-page-cache-etcd-starvation.md.
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
        # Same rollout deadlock as the pod-log promtail, and worse here: the
        # nodeSelector above is NixOS-only, so two of the three candidate nodes are
        # the roaming laptops. Exceeding the desired count is intended — Kubernetes
        # clamps it, and no roaming node should ever gate a journal rollout.
        "updateStrategy": _ROAMING_SAFE_UPDATE_STRATEGY,
        "config": {
            "clients": [{"url": _PUSH_URL}],
            "snippets": {
                # Disable the chart's default Kubernetes pod-log scrape — the main promtail
                # already collects pod logs on every node; this release must not double-collect.
                "scrapeConfigs": "",
                "extraScrapeConfigs": textwrap.dedent(
                    """\
                    - job_name: journal
                      journal:
                        path: /var/log/journal
                        max_age: 12h
                        labels:
                          job: systemd-journal
                      relabel_configs:
                        - source_labels: ['__journal__systemd_unit']
                          target_label: unit
                        - source_labels: ['__journal__hostname']
                          target_label: node
                        - source_labels: ['__journal_priority_keyword']
                          target_label: level
                    """
                ),
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
                # Keep memory well above the tail working set: with a tight cap, cgroup v2
                # page-cache eviction thrashes the node disk and can starve co-located etcd
                # (see the pod-log promtail and
                # cluster/debug/2026-06-10-etcd-io-contention/).
                "memory": "256Mi",
            },
            "limits": {"cpu": "500m", "memory": "512Mi"},
        },
    }


def _helm_releases(chart: Chart) -> None:
    HelmRelease(
        chart,
        "loki",
        metadata=metadata(NAME, NAME),
        spec=HelmReleaseSpec(
            interval="30m",
            install=HelmReleaseSpecInstall(remediation=HelmReleaseSpecInstallRemediation(retries=3)),
            chart=_grafana_chart("loki", "7.x"),
            values=_loki_values(),
        ),
    )
    HelmRelease(
        chart,
        "promtail",
        metadata=metadata("promtail", NAME),
        spec=HelmReleaseSpec(
            interval="30m",
            install=HelmReleaseSpecInstall(remediation=HelmReleaseSpecInstallRemediation(retries=3)),
            upgrade=HelmReleaseSpecUpgrade(
                # DaemonSet runs on roaming nodes (rugged, iguana) that may be offline.
                # Without this, Helm waits for all pods including those stuck Pending/Terminating
                # on offline nodes, causing the HelmRelease to hit RetriesExceeded and stall.
                disable_wait=True
            ),
            chart=_grafana_chart("promtail", "6.x"),
            values=_promtail_values(),
        ),
    )
    # Deviation from the stock promtail chart (whose default is pod-log tailing): this
    # release is journal-only. It scrapes the systemd journal on the NixOS nodes
    # (wyrm2, rugged, iguana) so kernel messages — notably the RTX 5090
    # `Xid 79 "GPU has fallen off the bus"` events — reach Loki with cluster
    # retention instead of dying with the node's ~5-day local journal. Talos nodes
    # have no journald and are handled separately (cluster/k8s/vector-talos-logs/);
    # the main pod-log promtail above is untouched and still runs on every node.
    HelmRelease(
        chart,
        "promtail-journal",
        metadata=metadata("promtail-journal", NAME),
        spec=HelmReleaseSpec(
            interval="30m",
            install=HelmReleaseSpecInstall(remediation=HelmReleaseSpecInstallRemediation(retries=3)),
            upgrade=HelmReleaseSpecUpgrade(
                # Same rationale as the pod-log promtail: roaming nodes (rugged, iguana) may
                # be offline, so don't block the release on their Pending/Terminating pods.
                disable_wait=True
            ),
            chart=_grafana_chart("promtail", "6.x"),
            values=_promtail_journal_values(),
        ),
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
    # Restrict Loki ingress to legitimate sources only.
    #
    # Loki has auth_enabled: false — any pod that can reach port 3100 can read,
    # write, or delete all cluster logs. This policy whitelists the known consumers.
    #
    # Allowed ingress to Loki (port 3100):
    #   - Promtail (loki namespace, log shipping)
    #   - Vector Talos-log receiver (vector-talos-logs namespace, Talos node logs)
    #   - Grafana (monitoring namespace, log queries)
    #   - Alloy (monitoring namespace, OTel log forwarding)
    #   - Gatus (gatus namespace, health checks)
    #   - Authentik proxy outpost (authentik namespace, SSO-protected external access)
    #   - Alloy (monitoring namespace, Loki canary metrics)
    #   - loki-read-proxy (loki-read-proxy namespace, namespace-filtered read-only
    #     queries for the Haku agent, direct to loki-read — see ducktape#4750)
    #   - Loki itself (SimpleScalable: read/backend/write/gateway/canary)
    #
    # Allowed egress from Loki:
    #   - kube-apiserver (k8s-sidecar needs to list/watch secrets for rule sync)
    #   - SeaweedFS S3 gateway (seaweedfs namespace, S3 chunk/index storage)
    #   - Loki itself (intra-cluster gossip, gRPC, HTTP, and gateway)
    CiliumNetworkPolicy(
        chart,
        "network-policy",
        metadata=metadata("loki-ingress", NAME),
        spec=CiliumNetworkPolicySpec(
            endpoint_selector=CiliumNetworkPolicySpecEndpointSelector(match_labels={"app.kubernetes.io/name": NAME}),
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
                # Alloy → Loki (OTel log forwarding)
                CiliumNetworkPolicySpecIngress(
                    from_endpoints=_from_pods({"app.kubernetes.io/name": "alloy", namespace_label: "monitoring"}),
                    to_ports=_ingress_tcp("3100"),
                ),
                # Alloy → Loki canary metrics (ServiceMonitor scraping)
                CiliumNetworkPolicySpecIngress(
                    from_endpoints=_from_pods({"app.kubernetes.io/name": "alloy", namespace_label: "monitoring"}),
                    to_ports=_ingress_tcp("3500"),
                ),
                # Gatus → Loki (health checks)
                CiliumNetworkPolicySpecIngress(
                    from_endpoints=_from_pods({"app.kubernetes.io/name": "gatus", namespace_label: "gatus"}),
                    to_ports=_ingress_tcp("3100"),
                ),
                # Authentik proxy outpost → Loki (SSO-protected external access)
                CiliumNetworkPolicySpecIngress(
                    from_endpoints=_from_pods({namespace_label: "authentik"}), to_ports=_ingress_tcp("3100")
                ),
                # loki-read-proxy → loki-read directly, bypassing the gateway (the proxy
                # enforces the query validator + allowlist). See ducktape#4750: the
                # gateway's nginx resolver can get permanently pinned to a dead CoreDNS
                # pod IP after CoreDNS reschedules, and this proxy only ever queries, so
                # it never needed the gateway's read/write path routing.
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
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    _storage(chart)
    _helm_releases(chart)
    _network_policy(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(root / OUTPUT_DIR / "kustomization.yaml", kustomize_kustomization(resources=[f"{NAME}.k8s.yaml"]))


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

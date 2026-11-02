"""The central ClickHouse: the ClickHouseInstallation (one shard, two replicas), its
three-member Keeper quorum, the client Service, ingress NetworkPolicies, the PodMonitor, and
the secret-free diagnostics grant for Haku and public-coder.

Existing users' credentials are hand-written `*.sops.yaml` Secrets beside the generated
output; the AIQuota history reader's password is minted in-cluster by ESO. Pod templates
and volume claim templates are untyped in the operator's CRD schema, so they are plain dicts here.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from clickhouse_installation_crds.com.altinity.clickhouse import (
    ClickHouseInstallation,
    ClickHouseInstallationSpec,
    ClickHouseInstallationSpecConfiguration,
    ClickHouseInstallationSpecConfigurationClusters,
    ClickHouseInstallationSpecConfigurationClustersLayout,
    ClickHouseInstallationSpecConfigurationClustersLayoutReplicas,
    ClickHouseInstallationSpecConfigurationClustersLayoutShards,
    ClickHouseInstallationSpecConfigurationClustersLayoutShardsReplicas,
    ClickHouseInstallationSpecConfigurationClustersLayoutShardsReplicasTemplates,
    ClickHouseInstallationSpecConfigurationClustersSecret,
    ClickHouseInstallationSpecConfigurationClustersSecretValueFrom,
    ClickHouseInstallationSpecConfigurationClustersSecretValueFromSecretKeyRef,
    ClickHouseInstallationSpecConfigurationZookeeper,
    ClickHouseInstallationSpecConfigurationZookeeperNodes,
    ClickHouseInstallationSpecDefaults,
    ClickHouseInstallationSpecDefaultsTemplates,
    ClickHouseInstallationSpecTemplates,
    ClickHouseInstallationSpecTemplatesPodTemplates,
    ClickHouseInstallationSpecTemplatesVolumeClaimTemplates,
    ClickHouseInstallationSpecTemplatesVolumeClaimTemplatesReclaimPolicy,
)
from clickhouse_keeper_installation_crds.com.altinity.clickhouse_keeper import (
    ClickHouseKeeperInstallation,
    ClickHouseKeeperInstallationSpec,
    ClickHouseKeeperInstallationSpecConfiguration,
    ClickHouseKeeperInstallationSpecConfigurationClusters,
    ClickHouseKeeperInstallationSpecConfigurationClustersLayout,
    ClickHouseKeeperInstallationSpecConfigurationClustersLayoutReplicas,
    ClickHouseKeeperInstallationSpecConfigurationClustersLayoutReplicasTemplates,
    ClickHouseKeeperInstallationSpecDefaults,
    ClickHouseKeeperInstallationSpecDefaultsTemplates,
    ClickHouseKeeperInstallationSpecTemplates,
    ClickHouseKeeperInstallationSpecTemplatesPodTemplates,
    ClickHouseKeeperInstallationSpecTemplatesVolumeClaimTemplates,
    ClickHouseKeeperInstallationSpecTemplatesVolumeClaimTemplatesReclaimPolicy,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthCheckExprs
from prometheus_operator_podmonitor_crds.com.coreos.monitoring import PodMonitorSpecSelector

from cluster.cdk8s import agent_access_profiles as access, cilium, node_scheduling
from cluster.cdk8s.clickhouse import client
from cluster.cdk8s.external_secrets.minted_secret import mint_bearer_secret
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.providers.prometheus_operator.pod_monitor import Endpoint, PodMonitor
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from util.bazel.runfiles import get_required_path, own_repo_rlocation

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/clickhouse/cluster"
SOPS_FILES = (
    "admin-credentials.sops.yaml",
    "aiquota-credentials.sops.yaml",
    "langfuse-credentials.sops.yaml",
    "grafana-credentials.sops.yaml",
    "public-coder-credentials.sops.yaml",
)
_KEEPER_NAME = "clickhouse-keeper"
# The Keeper operator's client Service in front of the quorum.
_KEEPER = ServiceRef(
    name=f"keeper-{_KEEPER_NAME}",
    port=Port(name="zk", number=2181),
    pods=Pods(
        namespace=client.NAMESPACE,
        labels=(("app.kubernetes.io/name", _KEEPER_NAME), ("app.kubernetes.io/instance", _KEEPER_NAME)),
    ),
)
_METRICS = Port(name="metrics", number=9363)
_INTERSERVER_PORT = 9009
_CLICKHOUSE_UID = 101  # the images' `clickhouse` user
_PIRAEUS_HDD_STORAGE_CLASS = "piraeus-hdd-local"
_PIRAEUS_SSD_STORAGE_CLASS = "piraeus-ssd-local"
_CLICKHOUSE_PIRAEUS_103656_POD_TEMPLATE = "clickhouse-piraeus-103656"
_CLICKHOUSE_PIRAEUS_102453_POD_TEMPLATE = "clickhouse-piraeus-102453"
_CLICKHOUSE_PIRAEUS_HDD_CLAIM = "data-piraeus-hdd"
_ANY_ADDRESS = ["0.0.0.0/0", "::/0"]
_HDD_NODE_SELECTOR = {**node_scheduling.HIL_OVH_NODE_SELECTOR, "storage.allegedly.works/tier": "hdd"}
_SSD_NODE_SELECTOR = {**node_scheduling.HIL_OVH_NODE_SELECTOR, "storage.allegedly.works/tier": "ssd"}
_RUNTIME_DEFAULT_SECCOMP = {"type": "RuntimeDefault"}
_CONTAINER_SECURITY_CONTEXT = {
    "allowPrivilegeEscalation": False,
    "capabilities": {"drop": ["ALL"]},
    "runAsNonRoot": True,
    "runAsUser": _CLICKHOUSE_UID,
    "runAsGroup": _CLICKHOUSE_UID,
    "seccompProfile": _RUNTIME_DEFAULT_SECCOMP,
}
_TCP = "TCP"


def _password(secret: str) -> dict[str, object]:
    return {"valueFrom": {"secretKeyRef": {"name": secret, "key": client.PASSWORD_KEY}}}


def _one_per_host(labels: dict[str, str]) -> dict[str, object]:
    return {
        "podAntiAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": [
                {"labelSelector": {"matchLabels": labels}, "topologyKey": "kubernetes.io/hostname"}
            ]
        }
    }


def _claim_spec(storage: str, storage_class: str) -> dict[str, object]:
    return {
        "accessModes": ["ReadWriteOnce"],
        "storageClassName": storage_class,
        "resources": {"requests": {"storage": storage}},
    }


def _clickhouse_pod_template(
    name: str, node_selector: dict[str, str]
) -> ClickHouseInstallationSpecTemplatesPodTemplates:
    return ClickHouseInstallationSpecTemplatesPodTemplates(
        name=name,
        metadata={"labels": client.LABELS},
        spec={
            "nodeSelector": node_selector,
            "affinity": _one_per_host(client.LABELS),
            "terminationGracePeriodSeconds": 120,
            "securityContext": {"fsGroup": _CLICKHOUSE_UID, "seccompProfile": _RUNTIME_DEFAULT_SECCOMP},
            "containers": [
                {
                    "name": "clickhouse",
                    "image": client.IMAGE,
                    "imagePullPolicy": "IfNotPresent",
                    "ports": [_METRICS.k8s_container_port()],
                    "resources": {
                        "requests": {"cpu": "500m", "memory": "2Gi"},
                        "limits": {"cpu": "4", "memory": "8Gi"},
                    },
                    "securityContext": _CONTAINER_SECURITY_CONTEXT,
                }
            ],
        },
    )


def _keeper_pod_template(
    name: str, node_selector: dict[str, str]
) -> ClickHouseKeeperInstallationSpecTemplatesPodTemplates:
    return ClickHouseKeeperInstallationSpecTemplatesPodTemplates(
        name=name,
        metadata={"labels": _KEEPER.pods.selector},
        spec={
            "nodeSelector": node_selector,
            "tolerations": [node_scheduling.CONTROL_PLANE_TOLERATION],
            "affinity": _one_per_host(_KEEPER.pods.selector),
            "securityContext": {"fsGroup": _CLICKHOUSE_UID, "seccompProfile": _RUNTIME_DEFAULT_SECCOMP},
            "containers": [
                {
                    "name": "clickhouse-keeper",
                    "image": (
                        "clickhouse/clickhouse-keeper:26.8.3.105"
                        "@sha256:d9aced52deafda7ca716983bc80766641bcde30c23f3f7abdf6cacc9f244bd74"
                    ),
                    "imagePullPolicy": "IfNotPresent",
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "256Mi"},
                        "limits": {"cpu": "1", "memory": "1Gi"},
                    },
                    "securityContext": _CONTAINER_SECURITY_CONTEXT,
                }
            ],
        },
    )


def _users() -> dict[str, object]:
    return {
        "default/networks/ip": ["127.0.0.1/32", "::1/128"],
        "admin/password": _password(client.ADMIN_CREDENTIALS.name),
        "admin/networks/ip": _ANY_ADDRESS,
        "admin/profile": "default",
        "admin/quota": "default",
        "admin/access_management": 1,
        "langfuse/password": _password("clickhouse-langfuse-credentials"),
        "langfuse/networks/ip": _ANY_ADDRESS,
        "langfuse/grants/query": [
            "GRANT SELECT, INSERT ON langfuse.*",
            "GRANT ALTER UPDATE, ALTER DELETE ON langfuse.*",
            "GRANT CREATE, DROP TABLE, DROP VIEW ON langfuse.*",
            "GRANT ALTER ADD COLUMN, ALTER MODIFY COLUMN, ALTER VIEW MODIFY QUERY ON langfuse.*",
            "GRANT ALTER ADD INDEX, ALTER DROP INDEX, ALTER MATERIALIZE INDEX ON langfuse.*",
            "GRANT SELECT ON system.replicas",
            "GRANT SHOW COLUMNS ON system.replicas",
            "GRANT SELECT(database, table, name, partition, partition_id, active, rows) ON system.parts",
            "GRANT SELECT(database, table, is_done) ON system.mutations",
            "GRANT SELECT(database, name, engine) ON system.tables",
            "GRANT SELECT ON system.processes",
            "GRANT SELECT ON system.query_log*",
            "GRANT READ ON REMOTE",
            "GRANT CLUSTER ON *.*",
            "GRANT SYSTEM SYNC REPLICA ON langfuse.*",
            "GRANT SYSTEM MERGES ON langfuse.*",
            "GRANT ALTER SETTINGS ON langfuse.*",
        ],
        "aiquota_ingest/password": _password("clickhouse-aiquota-credentials"),
        "aiquota_ingest/networks/ip": _ANY_ADDRESS,
        "aiquota_ingest/profile": "ingest",
        "aiquota_ingest/quota": "ingest",
        "aiquota_ingest/grants/query": [
            "GRANT INSERT ON aiquota.raw_http_observations",
            # The materialized views execute under the inserting identity, so they
            # need only these source columns to normalize a raw observation.
            "GRANT SELECT(event_id, observed_at, source, quota_windows, token_activity, reset_credits)"
            " ON aiquota.raw_http_observations",
            # Read/write on the materialized views' own target tables: creating a
            # view is validated against the creator's grants on its target, not
            # just its source.
            "GRANT SELECT, INSERT ON aiquota.aiquota_windows",
            "GRANT SELECT, INSERT ON aiquota.token_activity_daily",
            "GRANT SELECT, INSERT ON aiquota.reset_credits",
            # Scoped DDL so aiquota's API server can own its schema (aiquota/schema.sql),
            # the same pattern as the langfuse user above. The aiquota database itself stays admin-created
            # (cluster/cdk8s/clickhouse/schema.sql).
            "GRANT CREATE, DROP TABLE, DROP VIEW ON aiquota.*",
            "GRANT ALTER ADD COLUMN ON aiquota.*",
            # Required to execute `ON CLUSTER default` DDL, same as the langfuse
            # user above.
            "GRANT CLUSTER ON *.*",
        ],
        "grafana/password": _password("clickhouse-grafana-credentials"),
        "grafana/networks/ip": _ANY_ADDRESS,
        # The Grafana ClickHouse plugin sends max_execution_time during the
        # connection handshake. Keep this account read-only while allowing that
        # session setting to be changed.
        "grafana/profile": "grafana_readonly",
        "grafana/quota": "readonly",
        "grafana/grants/query": ["GRANT SELECT ON aiquota.*"],
        # The public-coder agent receives a non-secret placeholder only. Its
        # Agentplane egress credential replaces that placeholder in Basic auth
        # before forwarding to the private ClusterIP service. Keep the account scoped
        # to the AIQuota tenant tables: ClickHouse system metadata remains
        # unavailable to the agent.
        f"{client.PUBLIC_CODER_USER}/password": _password(client.PUBLIC_CODER_CREDENTIALS),
        f"{client.PUBLIC_CODER_USER}/networks/ip": _ANY_ADDRESS,
        f"{client.PUBLIC_CODER_USER}/profile": "readonly",
        f"{client.PUBLIC_CODER_USER}/quota": "readonly",
        f"{client.PUBLIC_CODER_USER}/grants/query": [
            "GRANT SELECT ON aiquota.aiquota_windows",
            "GRANT SELECT ON aiquota.raw_http_observations",
        ],
        # Agent sandboxes may read the typed history, not raw OAuth
        # responses or ClickHouse's other tenants. POST query bodies bypass proxy
        # path/method narrowing; the database's readonly profile is the barrier.
        f"{client.AIQUOTA_HISTORY_USER}/password": _password(client.AIQUOTA_HISTORY_CREDENTIALS),
        f"{client.AIQUOTA_HISTORY_USER}/networks/ip": _ANY_ADDRESS,
        f"{client.AIQUOTA_HISTORY_USER}/profile": "readonly",
        f"{client.AIQUOTA_HISTORY_USER}/quota": "readonly",
        f"{client.AIQUOTA_HISTORY_USER}/grants/query": [
            "GRANT SELECT ON aiquota.aiquota_windows",
            "GRANT SELECT ON aiquota.token_activity_daily",
            "GRANT SELECT ON aiquota.reset_credits",
        ],
    }


# `/etc/clickhouse-server/config.d` belongs to the operator: it mounts its own
# generated ConfigMap there, over-mounting whatever the podTemplate declares at the
# same path, so a `configMapGenerator` volume never becomes visible to the server.
# `spec.configuration.files` is the only route that lands a file in that directory.
# The key must sort after the operator's own generated
# `01-clickhouse-0*-{query,part,trace}_log.xml`, which set `replace="1"`.
_SYSTEM_LOGS_FILE = "zz-system-logs.xml"
_SYSTEM_LOGS_XML = get_required_path(own_repo_rlocation("cluster/cdk8s/clickhouse/zz-system-logs.xml")).read_text()


def clickhouse_chart(app: App) -> Chart:
    chart = Chart(app, "clickhouse", disable_resource_name_hashes=True)
    mint_bearer_secret(
        chart,
        "aiquota-history-password",
        name=client.AIQUOTA_HISTORY_CREDENTIALS,
        namespace=client.NAMESPACE,
        description="Read-only access to typed AIQuota history; never mounted by a sandbox.",
    )
    pod_template = "clickhouse"
    ClickHouseInstallation(
        chart,
        "installation",
        metadata=ApiObjectMetadata(name=client.NAME, namespace=client.NAMESPACE),
        spec=ClickHouseInstallationSpec(
            configuration=ClickHouseInstallationSpecConfiguration(
                zookeeper=ClickHouseInstallationSpecConfigurationZookeeper(
                    # The CHK operator exposes its three-member ensemble behind this stable
                    # service. Using the service address keeps the CHI compatible with the
                    # versioned CRD schema while Kubernetes removes unavailable endpoints.
                    # The FQDN: any other string is a ZooKeeper config change, which the
                    # operator hot-reloads into both live replicas.
                    nodes=[
                        ClickHouseInstallationSpecConfigurationZookeeperNodes(
                            host=_KEEPER.fqdn, port=_KEEPER.port.number
                        )
                    ],
                    session_timeout_ms=30000,
                    operation_timeout_ms=10000,
                ),
                users=_users(),
                profiles={
                    "grafana_readonly/readonly": 2,
                    "grafana_readonly/max_memory_usage": 1073741824,
                    "grafana_readonly/max_threads": 4,
                    "grafana_readonly/max_execution_time": 60,
                    "readonly/readonly": 1,
                    "readonly/max_memory_usage": 1073741824,
                    "readonly/max_threads": 4,
                    "readonly/max_execution_time": 60,
                    "ingest/async_insert": 1,
                    "ingest/wait_for_async_insert": 1,
                    "ingest/async_insert_busy_timeout_ms": 5000,
                    "ingest/max_memory_usage": 536870912,
                    "ingest/max_threads": 2,
                    "ingest/max_execution_time": 30,
                },
                quotas={
                    "ingest/interval/duration": 3600,
                    "ingest/interval/queries": 100000,
                    "ingest/interval/errors": 1000,
                    "readonly/interval/duration": 3600,
                    "readonly/interval/queries": 10000,
                    "readonly/interval/errors": 1000,
                },
                settings={
                    "prometheus/endpoint": "/metrics",
                    "prometheus/port": _METRICS.number,
                    "prometheus/metrics": "true",
                    "prometheus/events": "true",
                    "prometheus/asynchronous_metrics": "true",
                    "prometheus/status_info": "true",
                },
                files={_SYSTEM_LOGS_FILE: _SYSTEM_LOGS_XML},
                clusters=[
                    ClickHouseInstallationSpecConfigurationClusters(
                        name="default",
                        secret=ClickHouseInstallationSpecConfigurationClustersSecret(
                            value_from=ClickHouseInstallationSpecConfigurationClustersSecretValueFrom(
                                secret_key_ref=ClickHouseInstallationSpecConfigurationClustersSecretValueFromSecretKeyRef(
                                    name=client.ADMIN_CREDENTIALS.name, key="cluster-secret"
                                )
                            )
                        ),
                        layout=ClickHouseInstallationSpecConfigurationClustersLayout(
                            shards_count=1,
                            replicas_count=2,
                            # Preserve the existing StatefulSet selector labels after
                            # removing the full-node host from the end of the layout.
                            replicas=[
                                ClickHouseInstallationSpecConfigurationClustersLayoutReplicas(name="1"),
                                ClickHouseInstallationSpecConfigurationClustersLayoutReplicas(name="2"),
                            ],
                            shards=[
                                ClickHouseInstallationSpecConfigurationClustersLayoutShards(
                                    name="0",
                                    replicas_count=2,
                                    replicas=[
                                        ClickHouseInstallationSpecConfigurationClustersLayoutShardsReplicas(
                                            name="0-1",
                                            templates=(
                                                ClickHouseInstallationSpecConfigurationClustersLayoutShardsReplicasTemplates(
                                                    pod_template=_CLICKHOUSE_PIRAEUS_103656_POD_TEMPLATE,
                                                    data_volume_claim_template=_CLICKHOUSE_PIRAEUS_HDD_CLAIM,
                                                )
                                            ),
                                        ),
                                        # Keep the interrupted replica 0-2's retained PVC and Keeper identity intact.
                                        ClickHouseInstallationSpecConfigurationClustersLayoutShardsReplicas(
                                            name="0-3",
                                            templates=(
                                                ClickHouseInstallationSpecConfigurationClustersLayoutShardsReplicasTemplates(
                                                    pod_template=_CLICKHOUSE_PIRAEUS_102453_POD_TEMPLATE,
                                                    data_volume_claim_template=_CLICKHOUSE_PIRAEUS_HDD_CLAIM,
                                                )
                                            ),
                                        ),
                                    ],
                                )
                            ],
                        ),
                    )
                ],
            ),
            defaults=ClickHouseInstallationSpecDefaults(
                # Each replica Service FQDN resolves to its Pod address, allowing
                # ClickHouse to recognize its own member in the distributed-DDL host list.
                replicas_use_fqdn="yes",
                templates=ClickHouseInstallationSpecDefaultsTemplates(
                    pod_template=pod_template, data_volume_claim_template=_CLICKHOUSE_PIRAEUS_HDD_CLAIM
                ),
            ),
            templates=ClickHouseInstallationSpecTemplates(
                pod_templates=[
                    _clickhouse_pod_template(pod_template, _HDD_NODE_SELECTOR),
                    _clickhouse_pod_template(
                        _CLICKHOUSE_PIRAEUS_103656_POD_TEMPLATE,
                        {**_HDD_NODE_SELECTOR, "kubernetes.io/hostname": "ovh-ns103656"},
                    ),
                    _clickhouse_pod_template(
                        _CLICKHOUSE_PIRAEUS_102453_POD_TEMPLATE,
                        {**_HDD_NODE_SELECTOR, "kubernetes.io/hostname": "ovh-ns102453"},
                    ),
                ],
                volume_claim_templates=[
                    ClickHouseInstallationSpecTemplatesVolumeClaimTemplates(
                        name=_CLICKHOUSE_PIRAEUS_HDD_CLAIM,
                        reclaim_policy=ClickHouseInstallationSpecTemplatesVolumeClaimTemplatesReclaimPolicy.RETAIN,
                        # Raw responses are retained for one year; the generous HDD
                        # volume leaves headroom for MergeTree parts and future tenants.
                        spec=_claim_spec("500Gi", storage_class=_PIRAEUS_HDD_STORAGE_CLASS),
                    )
                ],
            ),
        ),
    )
    PodMonitor(
        chart,
        "podmonitor",
        metadata=ApiObjectMetadata(name=client.NAME, namespace=client.NAMESPACE),
        selector=PodMonitorSpecSelector(match_labels=client.LABELS),
        pod_metrics_endpoints=[Endpoint.plain(port=_METRICS.name, scrape_timeout="15s")],
    )
    return chart


def keeper_chart(app: App) -> Chart:
    chart = Chart(app, "keeper", disable_resource_name_hashes=True)
    pod_template = "keeper-ssd"
    data_claim = "keeper-data-ssd"
    ClickHouseKeeperInstallation(
        chart,
        "installation",
        metadata=ApiObjectMetadata(name=_KEEPER_NAME, namespace=client.NAMESPACE),
        spec=ClickHouseKeeperInstallationSpec(
            configuration=ClickHouseKeeperInstallationSpecConfiguration(
                clusters=[
                    ClickHouseKeeperInstallationSpecConfigurationClusters(
                        name="keeper",
                        layout=ClickHouseKeeperInstallationSpecConfigurationClustersLayout(
                            replicas=[
                                ClickHouseKeeperInstallationSpecConfigurationClustersLayoutReplicas(
                                    name="0",
                                    templates=ClickHouseKeeperInstallationSpecConfigurationClustersLayoutReplicasTemplates(
                                        pod_template=pod_template, data_volume_claim_template=data_claim
                                    ),
                                ),
                                ClickHouseKeeperInstallationSpecConfigurationClustersLayoutReplicas(
                                    name="1",
                                    templates=ClickHouseKeeperInstallationSpecConfigurationClustersLayoutReplicasTemplates(
                                        pod_template=pod_template, data_volume_claim_template=data_claim
                                    ),
                                ),
                                ClickHouseKeeperInstallationSpecConfigurationClustersLayoutReplicas(
                                    name="2",
                                    templates=ClickHouseKeeperInstallationSpecConfigurationClustersLayoutReplicasTemplates(
                                        pod_template=pod_template, data_volume_claim_template=data_claim
                                    ),
                                ),
                            ]
                        ),
                    )
                ],
                settings={
                    "logger/level": "information",
                    "logger/console": "true",
                    "listen_host": "0.0.0.0",
                    "keeper_server/coordination_settings/raft_logs_level": "warning",
                },
            ),
            defaults=ClickHouseKeeperInstallationSpecDefaults(
                templates=ClickHouseKeeperInstallationSpecDefaultsTemplates(
                    pod_template=pod_template, data_volume_claim_template=data_claim
                )
            ),
            templates=ClickHouseKeeperInstallationSpecTemplates(
                pod_templates=[_keeper_pod_template(pod_template, _SSD_NODE_SELECTOR)],
                volume_claim_templates=[
                    ClickHouseKeeperInstallationSpecTemplatesVolumeClaimTemplates(
                        name=data_claim,
                        reclaim_policy=ClickHouseKeeperInstallationSpecTemplatesVolumeClaimTemplatesReclaimPolicy.RETAIN,
                        spec=_claim_spec("2Gi", storage_class=_PIRAEUS_SSD_STORAGE_CLASS),
                    )
                ],
            ),
        ),
    )
    return chart


def service_chart(app: App) -> Chart:
    chart = Chart(app, "clickhouse-service", disable_resource_name_hashes=True)
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(
            name=client.HTTP.name,
            namespace=client.NAMESPACE,
            annotations={"description": "Stable client endpoint for the shared ClickHouse installation."},
        ),
        spec=k8s.ServiceSpec(
            selector=client.HTTP.pods.selector,
            ports=[client.HTTP.port.k8s_service_port(), client.NATIVE.port.k8s_service_port()],
        ),
    )
    return chart


def _ports(*ports: int) -> list[k8s.NetworkPolicyPort]:
    return [k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(port), protocol=_TCP) for port in ports]


def _from_namespace(namespace: str, pod_labels: dict[str, str]) -> list[k8s.NetworkPolicyPeer]:
    return [
        k8s.NetworkPolicyPeer(
            namespace_selector=k8s.LabelSelector(match_labels={"kubernetes.io/metadata.name": namespace}),
            pod_selector=k8s.LabelSelector(match_labels=pod_labels),
        )
    ]


def networkpolicy_chart(app: App) -> Chart:
    chart = Chart(app, "networkpolicy", disable_resource_name_hashes=True)
    same_namespace = [k8s.NetworkPolicyPeer(pod_selector=k8s.LabelSelector())]
    k8s.KubeNetworkPolicy(
        chart,
        "clickhouse",
        metadata=k8s.ObjectMeta(name="ingress", namespace=client.NAMESPACE),
        spec=k8s.NetworkPolicySpec(
            pod_selector=k8s.LabelSelector(match_labels=client.LABELS),
            policy_types=["Ingress"],
            ingress=[
                k8s.NetworkPolicyIngressRule(
                    from_=same_namespace,
                    ports=_ports(client.HTTP.pod_port, client.NATIVE.pod_port, _INTERSERVER_PORT, _METRICS.number),
                ),
                k8s.NetworkPolicyIngressRule(
                    from_=_from_namespace("cli-proxy-api", {"app.kubernetes.io/name": "aiquota"}),
                    # CLEANUP(added 2026-10-20): Drop the native port once no aiquota-api Pod
                    #   still has a `migrate` init container (the Deployment lists none and
                    #   its old ReplicaSet is gone); the API server applies its schema over HTTP.
                    ports=_ports(client.HTTP.pod_port, client.NATIVE.pod_port),
                ),
                # The agent app itself cannot reach ClickHouse. Agentplane egress is the only
                # public-coder-side caller admitted for its native read-only account.
                k8s.NetworkPolicyIngressRule(
                    from_=_from_namespace(
                        cilium.AGENTPLANE_STAGING_PROXY.namespace, {"app.kubernetes.io/name": "egress"}
                    ),
                    ports=_ports(client.HTTP.pod_port),
                ),
                k8s.NetworkPolicyIngressRule(
                    # Grafana Operator's generated Deployment uses app=grafana.
                    from_=_from_namespace("monitoring", {"app": "grafana"}),
                    ports=_ports(client.HTTP.pod_port, _METRICS.number),
                ),
                k8s.NetworkPolicyIngressRule(
                    from_=_from_namespace("monitoring", {"app.kubernetes.io/name": "alloy"}),
                    ports=_ports(client.HTTP.pod_port, _METRICS.number),
                ),
                # Scraped by the Alloy on each ClickHouse pod's node.
                k8s.NetworkPolicyIngressRule(
                    from_=_from_namespace("monitoring", {"app.kubernetes.io/name": "alloy-node"}),
                    ports=_ports(_METRICS.number),
                ),
                k8s.NetworkPolicyIngressRule(
                    from_=_from_namespace("langfuse", {"app.kubernetes.io/name": "langfuse"}),
                    ports=_ports(client.HTTP.pod_port, client.NATIVE.pod_port),
                ),
            ],
        ),
    )
    k8s.KubeNetworkPolicy(
        chart,
        "keeper",
        metadata=k8s.ObjectMeta(name="keeper-ingress", namespace=client.NAMESPACE),
        spec=k8s.NetworkPolicySpec(
            pod_selector=k8s.LabelSelector(match_labels=_KEEPER.pods.selector),
            policy_types=["Ingress"],
            ingress=[k8s.NetworkPolicyIngressRule(from_=same_namespace)],
        ),
    )
    return chart


def agent_diagnostics_rbac_chart(app: App) -> Chart:
    """Public, secret-free ClickHouse control-plane diagnostics for Haku and public-coder.

    The main resources include their observed status. This intentionally grants no
    Secrets, pod exec, writes, or status-subresource mutation.
    """
    chart = Chart(app, "agent-diagnostics-rbac", disable_resource_name_hashes=True)
    name = "agent-clickhouse-diagnostics-reader"
    read = ["get", "list", "watch"]
    k8s.KubeRole(
        chart,
        "role",
        metadata=k8s.ObjectMeta(
            name=name,
            namespace=client.NAMESPACE,
            annotations={"description": "Read-only ClickHouse operator and observability resource status."},
        ),
        rules=[
            k8s.PolicyRule(api_groups=["clickhouse.altinity.com"], resources=["clickhouseinstallations"], verbs=read),
            k8s.PolicyRule(
                api_groups=["clickhouse-keeper.altinity.com"], resources=["clickhousekeeperinstallations"], verbs=read
            ),
            k8s.PolicyRule(api_groups=["helm.toolkit.fluxcd.io"], resources=["helmreleases"], verbs=read),
            k8s.PolicyRule(api_groups=["batch"], resources=["jobs"], verbs=read),
            k8s.PolicyRule(api_groups=["policy"], resources=["poddisruptionbudgets"], verbs=read),
            k8s.PolicyRule(
                api_groups=["grafana.integreatly.org"],
                resources=["grafanadashboards", "grafanadatasources"],
                verbs=read,
            ),
            k8s.PolicyRule(
                api_groups=["monitoring.coreos.com"], resources=["podmonitors", "servicemonitors"], verbs=read
            ),
        ],
    )
    k8s.KubeRoleBinding(
        chart,
        "binding",
        metadata=k8s.ObjectMeta(
            name=name,
            namespace=client.NAMESPACE,
            annotations={"description": "Binds Haku and public-coder to secret-free ClickHouse status."},
        ),
        role_ref=access.role_ref("clickhouse-diagnostics"),
        subjects=[subject.k8s() for subject in access.profile_subjects("clickhouse-diagnostics")],
    )
    return chart


CHARTS = (agent_diagnostics_rbac_chart, keeper_chart, clickhouse_chart, service_chart, networkpolicy_chart)


def clickhouse(
    chart: Chart,
    directory: RenderedDirectory,
    clickhouse_operator: Kustomization,
    external_secrets_operator: Kustomization,
) -> Kustomization:
    name = "clickhouse"
    return flux_kustomization(
        chart,
        name,
        directory,
        timeout="20m",
        health_check_exprs=[
            KustomizationSpecHealthCheckExprs(
                api_version="clickhouse-keeper.altinity.com/v1",
                kind="ClickHouseKeeperInstallation",
                current="status.status == 'Completed'",
                failed="status.status == 'Aborted'",
                in_progress="status.status != 'Completed' && status.status != 'Aborted'",
            ),
            KustomizationSpecHealthCheckExprs(
                api_version="clickhouse.altinity.com/v1",
                kind="ClickHouseInstallation",
                current="status.status == 'Completed'",
                failed="status.status == 'Aborted'",
                in_progress="status.status != 'Completed' && status.status != 'Aborted'",
            ),
        ],
        depends_on=[
            flux_kustomization_depends_on(clickhouse_operator),
            flux_kustomization_depends_on(external_secrets_operator),
        ],
    )

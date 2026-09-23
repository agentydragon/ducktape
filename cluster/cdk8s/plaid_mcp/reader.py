"""plaid-mcp's `reader/`: the read-only Postgres MCP over the Plaid sync database, fronted by
mcp-oauth-facade, with its config, Service, HTTPRoute and ingress policy; and
`servicemonitor/`, which scrapes the facade.

The facade's image tag is the placeholder "unset"; the hand-written
`reader/image-pins/kustomization.yaml` overrides it at `kustomize build` time via Flux's
image-automation marker (cluster/cdk8s/AGENTS.md § the `:tag` Setters marker). Also
hand-written in `reader/`: `kustomization.yaml`.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from prometheus_operator_crds.com.coreos.monitoring import (
    ServiceMonitor,
    ServiceMonitorSpec,
    ServiceMonitorSpecEndpoints,
    ServiceMonitorSpecSelector,
)
from redis_operator_redisreplication_crds.in_.opstreelabs.redis.redis import (
    RedisReplication,
    RedisReplicationSpec,
    RedisReplicationSpecAffinity,
    RedisReplicationSpecAffinityNodeAffinity,
    RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecution,
    RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreference,
    RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreferenceMatchExpressions,
    RedisReplicationSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecution,
    RedisReplicationSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTerms,
    RedisReplicationSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions,
    RedisReplicationSpecAffinityPodAntiAffinity,
    RedisReplicationSpecAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecution,
    RedisReplicationSpecAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecutionLabelSelector,
    RedisReplicationSpecKubernetesConfig,
    RedisReplicationSpecKubernetesConfigResources,
    RedisReplicationSpecKubernetesConfigResourcesLimits,
    RedisReplicationSpecKubernetesConfigResourcesRequests,
    RedisReplicationSpecStorage,
    RedisReplicationSpecStorageVolumeClaimTemplate,
    RedisReplicationSpecStorageVolumeClaimTemplateSpec,
    RedisReplicationSpecStorageVolumeClaimTemplateSpecResources,
    RedisReplicationSpecStorageVolumeClaimTemplateSpecResourcesRequests,
)

from cluster.cdk8s import cilium
from cluster.cdk8s.flux import kustomize_kustomization
from cluster.cdk8s.forgejo_images import SECRET_NAME
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.plaid_mcp.app import NAMESPACE

OUTPUT_DIR = "cluster/k8s/agents/plaid-mcp/reader"
SERVICEMONITOR_DIR = "cluster/k8s/agents/plaid-mcp/servicemonitor"
_NAME = "plaid-db-mcp"
_LABELS = {"app.kubernetes.io/name": _NAME}
_CONFIG_MAP = "plaid-db-mcp-config"
_OIDC_SECRET = "plaid-db-mcp-oidc"
_UPSTREAM_PORT = 8000
_HTTP_PORT = 8765
_METRICS_PORT = 9090
_VALKEY = "plaid-valkey-kimsufi"


def _secret_env(name: str, secret: str, key: str) -> k8s.EnvVar:
    return k8s.EnvVar(
        name=name, value_from=k8s.EnvVarSource(secret_key_ref=k8s.SecretKeySelector(name=secret, key=key))
    )


def _container_security_context() -> k8s.SecurityContext:
    return k8s.SecurityContext(
        allow_privilege_escalation=False,
        capabilities=k8s.Capabilities(drop=["ALL"]),
        run_as_non_root=True,
        run_as_group=1000,
        run_as_user=1000,
    )


def _deployment(chart: Chart) -> None:
    upstream = k8s.TcpSocketAction(port=k8s.IntOrString.from_number(_UPSTREAM_PORT))
    health = k8s.HttpGetAction(path="/healthz", port=k8s.IntOrString.from_number(_HTTP_PORT))
    k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=_NAME,
            namespace=NAMESPACE,
            labels=_LABELS,
            annotations={
                "description": (
                    "Read-only Postgres MCP over the Plaid sync database, fronted by mcp-oauth-facade. The"
                    " upstream MCP uses Streamable HTTP and connects with the plaid_ro role."
                ),
                "reloader.stakater.com/auto": "true",
            },
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS),
                spec=k8s.PodSpec(
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    automount_service_account_token=False,
                    security_context=k8s.PodSecurityContext(seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault")),
                    containers=[
                        k8s.Container(
                            name="postgres-mcp",
                            image=(
                                "enterprisedb/pg-airman-mcp:latest"
                                "@sha256:99fb30356e66b7ebd816dbbbf70a29f091bcc1db09cf952bc719f10a05818c04"
                            ),
                            image_pull_policy="IfNotPresent",
                            args=[
                                "--access-mode=restricted",
                                "--transport=streamable-http",
                                "--streamable-http-host=0.0.0.0",
                                f"--streamable-http-port={_UPSTREAM_PORT}",
                            ],
                            ports=[k8s.ContainerPort(name="upstream", container_port=_UPSTREAM_PORT, protocol="TCP")],
                            # The read-only credentials db.py mints.
                            env=[_secret_env("AIRMAN_MCP_DATABASE_URL", "plaid-mcp-db-readonly", "DATABASE_URL")],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "memory": k8s.Quantity.from_string("128Mi"),
                                    "cpu": k8s.Quantity.from_string("50m"),
                                },
                                limits={"memory": k8s.Quantity.from_string("512Mi")},
                            ),
                            security_context=_container_security_context(),
                            readiness_probe=k8s.Probe(tcp_socket=upstream, initial_delay_seconds=5, period_seconds=10),
                            liveness_probe=k8s.Probe(tcp_socket=upstream, initial_delay_seconds=20, period_seconds=20),
                        ),
                        k8s.Container(
                            name="facade",
                            image="git.allegedly.works/ducktape-ci/mcp-oauth-facade:unset",
                            image_pull_policy="Always",
                            ports=[
                                k8s.ContainerPort(name="http", container_port=_HTTP_PORT, protocol="TCP"),
                                # Prometheus metrics, cluster-internal only (not on the HTTPRoute).
                                k8s.ContainerPort(name="metrics", container_port=_METRICS_PORT, protocol="TCP"),
                            ],
                            env_from=[k8s.EnvFromSource(config_map_ref=k8s.ConfigMapEnvSource(name=_CONFIG_MAP))],
                            env=[
                                _secret_env("MCP_FACADE_AUTH__OIDC_CLIENT_ID", _OIDC_SECRET, "client_id"),
                                _secret_env("MCP_FACADE_AUTH__OIDC_CLIENT_SECRET", _OIDC_SECRET, "client_secret"),
                            ],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "memory": k8s.Quantity.from_string("128Mi"),
                                    "cpu": k8s.Quantity.from_string("50m"),
                                },
                                limits={"memory": k8s.Quantity.from_string("256Mi")},
                            ),
                            security_context=_container_security_context(),
                            readiness_probe=k8s.Probe(http_get=health, initial_delay_seconds=5, period_seconds=10),
                            liveness_probe=k8s.Probe(http_get=health, initial_delay_seconds=20, period_seconds=20),
                        ),
                    ],
                ),
            ),
        ),
    )


def _valkey(chart: Chart) -> None:
    RedisReplication(
        chart,
        "valkey",
        metadata=metadata(
            _VALKEY, NAMESPACE, annotations={"description": "Kimsufi Valkey for the Plaid DB MCP OAuth facade state"}
        ),
        spec=RedisReplicationSpec(
            cluster_size=2,
            kubernetes_config=RedisReplicationSpecKubernetesConfig(
                image="valkey/valkey:9-alpine",
                image_pull_policy="IfNotPresent",
                resources=RedisReplicationSpecKubernetesConfigResources(
                    requests={
                        "cpu": RedisReplicationSpecKubernetesConfigResourcesRequests.from_string("50m"),
                        "memory": RedisReplicationSpecKubernetesConfigResourcesRequests.from_string("64Mi"),
                    },
                    limits={
                        "cpu": RedisReplicationSpecKubernetesConfigResourcesLimits.from_string("200m"),
                        "memory": RedisReplicationSpecKubernetesConfigResourcesLimits.from_string("128Mi"),
                    },
                ),
            ),
            storage=RedisReplicationSpecStorage(
                volume_claim_template=RedisReplicationSpecStorageVolumeClaimTemplate(
                    spec=RedisReplicationSpecStorageVolumeClaimTemplateSpec(
                        access_modes=["ReadWriteOnce"],
                        storage_class_name="local-path-ovh",
                        resources=RedisReplicationSpecStorageVolumeClaimTemplateSpecResources(
                            requests={
                                "storage": RedisReplicationSpecStorageVolumeClaimTemplateSpecResourcesRequests.from_string(
                                    "1Gi"
                                )
                            }
                        ),
                    )
                )
            ),
            affinity=RedisReplicationSpecAffinity(
                node_affinity=RedisReplicationSpecAffinityNodeAffinity(
                    required_during_scheduling_ignored_during_execution=RedisReplicationSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecution(
                        node_selector_terms=[
                            RedisReplicationSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTerms(
                                match_expressions=[
                                    RedisReplicationSpecAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions(
                                        key="topology.kubernetes.io/zone", operator="In", values=["hil-ovh"]
                                    )
                                ]
                            )
                        ]
                    ),
                    # Prefer ordinary workers when this workload tolerates control planes.
                    preferred_during_scheduling_ignored_during_execution=[
                        RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecution(
                            weight=100,
                            preference=RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreference(
                                match_expressions=[
                                    RedisReplicationSpecAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreferenceMatchExpressions(
                                        key="node-role.kubernetes.io/control-plane", operator="DoesNotExist"
                                    )
                                ]
                            ),
                        )
                    ],
                ),
                pod_anti_affinity=RedisReplicationSpecAffinityPodAntiAffinity(
                    required_during_scheduling_ignored_during_execution=[
                        RedisReplicationSpecAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecution(
                            label_selector=RedisReplicationSpecAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecutionLabelSelector(
                                match_labels={"app": _VALKEY}
                            ),
                            topology_key="kubernetes.io/hostname",
                        )
                    ]
                ),
            ),
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    k8s.KubeConfigMap(
        chart,
        "config",
        metadata=k8s.ObjectMeta(name=_CONFIG_MAP, namespace=NAMESPACE),
        data={
            "MCP_FACADE_AUTH__OIDC_ISSUER": "https://auth.allegedly.works/application/o/plaid-db-mcp/",
            "MCP_FACADE_AUTH__PUBLIC_BASE_URL": "https://plaid-db.allegedly.works",
            "MCP_FACADE_FACADE_NAME": "Plaid DB MCP Facade",
            "MCP_FACADE_UPSTREAM__KIND": "http",
            "MCP_FACADE_UPSTREAM__URL": f"http://localhost:{_UPSTREAM_PORT}/mcp",
            "MCP_FACADE_PERSISTENCE__KIND": "valkey",
            # The RedisReplication's primary Service.
            "MCP_FACADE_PERSISTENCE__HOST": f"{_VALKEY}-master.{NAMESPACE}.svc.cluster.local",
            "MCP_FACADE_PERSISTENCE__DB": "0",
        },
    )
    _deployment(chart)
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(name=_NAME, namespace=NAMESPACE, labels=_LABELS),
        spec=k8s.ServiceSpec(
            selector=_LABELS,
            ports=[
                k8s.ServicePort(
                    name="http", port=_HTTP_PORT, target_port=k8s.IntOrString.from_string("http"), protocol="TCP"
                ),
                k8s.ServicePort(
                    name="metrics",
                    port=_METRICS_PORT,
                    target_port=k8s.IntOrString.from_string("metrics"),
                    protocol="TCP",
                ),
            ],
            type="ClusterIP",
        ),
    )
    https_route(
        chart,
        "httproute",
        metadata=metadata(
            _NAME,
            NAMESPACE,
            annotations={
                "description": (
                    "Authentik-gated Postgres MCP for querying the synced Plaid database through the read-only"
                    " plaid_ro role."
                )
            },
        ),
        hostname="plaid-db.allegedly.works",
        backend=_NAME,
        port=_HTTP_PORT,
        timeout="60s",
        hsts=False,
        listener=None,
    )
    cilium.network_policy(
        chart,
        "ingress-policy",
        metadata=metadata(
            "plaid-db-mcp-ingress",
            NAMESPACE,
            annotations={
                "description": (
                    "Default-deny ingress for plaid-db-mcp pods. Only Gateway ingress may reach the OAuth facade;"
                    " postgres-mcp stays loopback-only."
                )
            },
        ),
        selector=_LABELS,
        ingress=[
            cilium.ingress_from_gateway(_HTTP_PORT),
            # monitoring: Prometheus metrics scraping
            cilium.ingress_from({"k8s:io.kubernetes.pod.namespace": "monitoring"}, ports=[_METRICS_PORT]),
        ],
    )
    _valkey(chart)
    return chart


def servicemonitor_chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    ServiceMonitor(
        chart,
        "servicemonitor",
        metadata=metadata(_NAME, NAMESPACE),
        spec=ServiceMonitorSpec(
            selector=ServiceMonitorSpecSelector(match_labels=_LABELS),
            endpoints=[ServiceMonitorSpecEndpoints(port="metrics", path="/metrics", scrape_timeout="10s")],
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_charts(root, SERVICEMONITOR_DIR, servicemonitor_chart)
    write_yaml(
        root / SERVICEMONITOR_DIR / "kustomization.yaml", kustomize_kustomization(resources=[f"{_NAME}.k8s.yaml"])
    )

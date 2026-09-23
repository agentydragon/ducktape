"""The Grocy MCP server: the household-independent `mcp-base` (Deployment, Service) and
`mcp-servicemonitor-base`, and each household's `<household>/mcp` (pull credentials and
the public HTTPRoute).

The server's image tag is the placeholder "unset"; the hand-written
`mcp-base/image-pins/kustomization.yaml` overrides it at `kustomize build` time via Flux's
image-automation marker (cluster/cdk8s/AGENTS.md § the `:tag` Setters marker).

Hand-written beside the generated output in each household's `mcp/`: the
`kustomization.yaml` (its configMapGenerator renders `config.yaml`, its patch points the
secret env at the household's OIDC Secret).
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
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

from cluster.cdk8s.flux import kustomize_kustomization
from cluster.cdk8s.forgejo_images import SECRET_NAME, forgejo_images_creds_external_secret
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.metadata import metadata

BASE_DIR = "cluster/k8s/grocy/mcp-base"
SERVICEMONITOR_BASE_DIR = "cluster/k8s/grocy/mcp-servicemonitor-base"
_NAME = "grocy-mcp-server"
_LABELS = {"app.kubernetes.io/name": "grocy-mcp", "app.kubernetes.io/component": "server"}
_IMAGE = "git.allegedly.works/ducktape-ci/grocy-mcp:unset"
_HTTP_PORT = 8765
_METRICS_PORT = 9090
# The base's placeholder; each household's kustomization.yaml patches in its own Secret.
_OIDC_SECRET = "grocy-mcp-oidc"
_CONTROL_PLANE = "node-role.kubernetes.io/control-plane"


def _secret_env(name: str, key: str) -> k8s.EnvVar:
    return k8s.EnvVar(
        name=name, value_from=k8s.EnvVarSource(secret_key_ref=k8s.SecretKeySelector(name=_OIDC_SECRET, key=key))
    )


def base_chart(app: App) -> Chart:
    """The server every household runs; the household overlay supplies the namespace."""
    chart = Chart(app, "grocy-mcp", disable_resource_name_hashes=True)
    http_port = k8s.IntOrString.from_number(_HTTP_PORT)
    k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=_NAME,
            labels=_LABELS,
            annotations={
                "description": (
                    "FastMCP server generating Grocy tools from Grocy's OpenAPI spec. Per-request token"
                    " exchange swaps the caller's Authentik JWT for a Grocy-proxy-scoped JWT before calling"
                    " Grocy."
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
                    # The OAuth state Valkey instances use local-path-ovh and are pinned to
                    # hil-ovh. Keep the MCP client in the same site: valkey-glide's default
                    # 250 ms request timeout is too small for the current cross-site path.
                    node_selector={"topology.kubernetes.io/zone": "hil-ovh"},
                    # Stateless (config only, no PVC). Allow control-plane nodes as overflow
                    # capacity, but prefer workers to keep ordinary application I/O away from
                    # etcd disks.
                    tolerations=[k8s.Toleration(key=_CONTROL_PLANE, operator="Exists", effect="NoSchedule")],
                    affinity=k8s.Affinity(
                        node_affinity=k8s.NodeAffinity(
                            preferred_during_scheduling_ignored_during_execution=[
                                k8s.PreferredSchedulingTerm(
                                    weight=100,
                                    preference=k8s.NodeSelectorTerm(
                                        match_expressions=[
                                            k8s.NodeSelectorRequirement(key=_CONTROL_PLANE, operator="DoesNotExist")
                                        ]
                                    ),
                                )
                            ]
                        )
                    ),
                    containers=[
                        k8s.Container(
                            name="server",
                            image=_IMAGE,
                            image_pull_policy="Always",
                            ports=[
                                k8s.ContainerPort(name="http", container_port=_HTTP_PORT, protocol="TCP"),
                                # Prometheus metrics, cluster-internal only (not on the HTTPRoute).
                                k8s.ContainerPort(name="metrics", container_port=_METRICS_PORT, protocol="TCP"),
                            ],
                            env=[
                                k8s.EnvVar(name="LOG_LEVEL", value="INFO"),
                                # Non-secret structured config (grocy_url, auth issuer/URLs/
                                # direct_jwt_trusts, persistence) comes from this YAML file, supplied
                                # per household by the overlay's grocy-mcp-config configMap. Secrets
                                # stay in env below (per-household secret via overlay patch).
                                k8s.EnvVar(name="GROCY_MCP_CONFIG_FILE", value="/etc/grocy-mcp/config.yaml"),
                                _secret_env("GROCY_MCP_AUTH__OIDC_CLIENT_ID", "client_id"),
                                _secret_env("GROCY_MCP_AUTH__OIDC_CLIENT_SECRET", "client_secret"),
                                _secret_env("GROCY_MCP_AUTH__PROXY_CLIENT_ID", "grocy_proxy_client_id"),
                            ],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "memory": k8s.Quantity.from_string("128Mi"),
                                    "cpu": k8s.Quantity.from_string("50m"),
                                },
                                limits={
                                    # TODO(vpa-memory-audit): 256Mi -> 768Mi. VPA observed 335Mi
                                    # request / 355Mi upper in grocy-sf (256Mi/256Mi in vallejo),
                                    # both at or over the old limit. This base is shared by both
                                    # overlays, so the higher of the two governs.
                                    "memory": k8s.Quantity.from_string("768Mi"),
                                    "cpu": k8s.Quantity.from_string("200m"),
                                },
                            ),
                            volume_mounts=[k8s.VolumeMount(name="config", mount_path="/etc/grocy-mcp", read_only=True)],
                            readiness_probe=k8s.Probe(
                                tcp_socket=k8s.TcpSocketAction(port=http_port),
                                initial_delay_seconds=3,
                                period_seconds=10,
                            ),
                            liveness_probe=k8s.Probe(
                                tcp_socket=k8s.TcpSocketAction(port=http_port),
                                initial_delay_seconds=15,
                                period_seconds=20,
                            ),
                        )
                    ],
                    volumes=[k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name="grocy-mcp-config"))],
                ),
            ),
        ),
    )
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(name=_NAME, labels=_LABELS),
        spec=k8s.ServiceSpec(
            selector=_LABELS,
            ports=[
                k8s.ServicePort(name="http", port=_HTTP_PORT, target_port=http_port, protocol="TCP"),
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
    return chart


def servicemonitor_base_chart(app: App) -> Chart:
    chart = Chart(app, "grocy-mcp-servicemonitor", disable_resource_name_hashes=True)
    ServiceMonitor(
        chart,
        "servicemonitor",
        metadata=ApiObjectMetadata(name=_NAME),
        spec=ServiceMonitorSpec(
            selector=ServiceMonitorSpecSelector(match_labels=_LABELS),
            endpoints=[ServiceMonitorSpecEndpoints(port="metrics", path="/metrics", scrape_timeout="10s")],
        ),
    )
    return chart


def _valkey(chart: Chart, *, household: str, display_name: str, namespace: str) -> None:
    name = f"grocy-{household}-valkey-ovh"
    RedisReplication(
        chart,
        "valkey",
        metadata=metadata(
            name,
            namespace,
            annotations={"description": f"Replacement OVH Valkey for Grocy {display_name} MCP OAuth state"},
        ),
        spec=RedisReplicationSpec(
            cluster_size=2,
            kubernetes_config=RedisReplicationSpecKubernetesConfig(
                # renovate: datasource=docker
                image="valkey/valkey:9-alpine",
                image_pull_policy="IfNotPresent",
                resources=RedisReplicationSpecKubernetesConfigResources(
                    requests={
                        "cpu": RedisReplicationSpecKubernetesConfigResourcesRequests.from_string("50m"),
                        "memory": RedisReplicationSpecKubernetesConfigResourcesRequests.from_string("64Mi"),
                    },
                    limits={
                        "cpu": RedisReplicationSpecKubernetesConfigResourcesLimits.from_string("200m"),
                        # TODO(vpa-memory-audit): 128Mi -> 384Mi. VPA observed 256Mi for both
                        # request and upper bound — double the old limit. This valkey backs the
                        # Grocy MCP cache; 256Mi resident suggests unbounded key growth rather
                        # than a working set, so check the eviction policy.
                        "memory": RedisReplicationSpecKubernetesConfigResourcesLimits.from_string("384Mi"),
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
                                match_labels={"app": name}
                            ),
                            topology_key="kubernetes.io/hostname",
                        )
                    ]
                ),
            ),
        ),
    )


def household_chart(app: App, *, household: str, display_name: str) -> Chart:
    namespace = f"grocy-{household}"
    chart = Chart(app, f"grocy-mcp-{household}", disable_resource_name_hashes=True)
    forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=namespace)
    # NOT behind the Authentik outpost — OIDCProxy runs inside the pod and drives the full MCP
    # OAuth dance (DCR, PKCE, resource metadata).
    https_route(
        chart,
        "httproute",
        metadata=metadata(f"grocy-mcp-{household}-server", namespace),
        hostname=f"grocy-mcp-{household}.allegedly.works",
        backend=_NAME,
        port=_HTTP_PORT,
        timeout="60s",
        hsts=False,
        listener=None,
    )
    _valkey(chart, household=household, display_name=display_name, namespace=namespace)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, BASE_DIR, base_chart)
    write_yaml(
        root / BASE_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=["grocy-mcp.k8s.yaml"], components=["./image-pins"]),
    )
    write_charts(root, SERVICEMONITOR_BASE_DIR, servicemonitor_base_chart)
    write_yaml(
        root / SERVICEMONITOR_BASE_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=["grocy-mcp-servicemonitor.k8s.yaml"]),
    )
    write_charts(root, "cluster/k8s/grocy/sf/mcp", lambda app: household_chart(app, household="sf", display_name="SF"))
    write_charts(
        root,
        "cluster/k8s/grocy/vallejo/mcp",
        lambda app: household_chart(app, household="vallejo", display_name="Vallejo"),
    )

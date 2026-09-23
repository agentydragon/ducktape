"""The Zot OCI pull-through cache: its Namespace, Deployment (Zot plus the nginx basic-auth
sidecar for the public endpoint), Service, HTTPRoute, ServiceMonitor and dedupe-cache
`RedisReplication`.

Hand-written beside the generated output (cluster/k8s/oci-cache): `kustomization.yaml`
(its configMapGenerator renders `config.json` and `public-auth-proxy.conf`) and
`puller-credential.sops.yaml`.
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
    RedisReplicationSpecRedisConfig,
    RedisReplicationSpecStorage,
    RedisReplicationSpecStorageVolumeClaimTemplate,
    RedisReplicationSpecStorageVolumeClaimTemplateSpec,
    RedisReplicationSpecStorageVolumeClaimTemplateSpecResources,
    RedisReplicationSpecStorageVolumeClaimTemplateSpecResourcesRequests,
)

from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

OUTPUT_DIR = "cluster/k8s/oci-cache"
_NAMESPACE = "oci-cache"
_NAME = "zot"
_LABELS = {"app.kubernetes.io/name": _NAME}
_PUBLIC_AUTH_PORT = 8080
_VALKEY = "oci-cache-valkey"


def _s3_secret_env(name: str, key: str) -> k8s.EnvVar:
    return k8s.EnvVar(
        name=name,
        value_from=k8s.EnvVarSource(
            secret_key_ref=k8s.SecretKeySelector(name="registry-cache-s3-credentials", key=key)
        ),
    )


def _tcp_probe(port: str, *, initial_delay_seconds: int, period_seconds: int) -> k8s.Probe:
    return k8s.Probe(
        tcp_socket=k8s.TcpSocketAction(port=k8s.IntOrString.from_string(port)),
        initial_delay_seconds=initial_delay_seconds,
        period_seconds=period_seconds,
    )


def _deployment(chart: Chart) -> None:
    k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=_NAME,
            namespace=_NAMESPACE,
            labels=_LABELS,
            annotations={
                "description": (
                    "Zot OCI pull-through cache. On-demand mirror for docker.io, ghcr.io, quay.io,"
                    " registry.k8s.io and gcr.io addressed by path prefix (/docker-hub, /ghcr, /quay, /k8s,"
                    " /gcr). Durable content in the SeaweedFS registry-cache bucket (S3); dedupe index in the"
                    " oci-cache-valkey RedisReplication. No PVC — the only local state is ephemeral upload"
                    " staging on emptyDir, so the pod reschedules freely. The in-cluster Service is"
                    " intentionally unauthenticated for Docker registry-mirror compatibility; the public"
                    " endpoint is authenticated by the nginx sidecar."
                ),
                "reloader.stakater.com/auto": "true",
            },
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            selector=k8s.LabelSelector(match_labels=_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    security_context=k8s.PodSecurityContext(
                        fs_group=65532, seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault")
                    ),
                    containers=[
                        k8s.Container(
                            name=_NAME,
                            image="ghcr.io/project-zot/zot-linux-amd64:v2.1.21",
                            image_pull_policy="IfNotPresent",
                            args=["serve", "/etc/zot/config.json"],
                            ports=[k8s.ContainerPort(name="http", container_port=5000, protocol="TCP")],
                            # docker/distribution S3 driver reads the AWS default credential
                            # chain when accesskey/secretkey are omitted from config.json.
                            env=[
                                _s3_secret_env("AWS_ACCESS_KEY_ID", "accessKey"),
                                _s3_secret_env("AWS_SECRET_ACCESS_KEY", "secretKey"),
                            ],
                            volume_mounts=[
                                k8s.VolumeMount(name="config", mount_path="/etc/zot", read_only=True),
                                k8s.VolumeMount(name="cache", mount_path="/var/lib/zot"),
                            ],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "memory": k8s.Quantity.from_string("128Mi"),
                                    "cpu": k8s.Quantity.from_string("50m"),
                                },
                                limits={
                                    "memory": k8s.Quantity.from_string("512Mi"),
                                    "cpu": k8s.Quantity.from_string("1"),
                                },
                            ),
                            # TCP probe only confirms Zot is listening. Pull-through behavior is
                            # covered by the README smoke tests.
                            readiness_probe=_tcp_probe("http", initial_delay_seconds=5, period_seconds=10),
                            liveness_probe=_tcp_probe("http", initial_delay_seconds=20, period_seconds=20),
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False,
                                run_as_non_root=True,
                                run_as_user=65532,
                                run_as_group=65532,
                                read_only_root_filesystem=True,
                                capabilities=k8s.Capabilities(drop=["ALL"]),
                            ),
                        ),
                        # Public basic-auth wrapper. Zot itself must stay unauthenticated on the
                        # in-cluster Service because dockerd's Docker Hub registry-mirror probe
                        # does not send client Docker-config credentials for the mirror host.
                        k8s.Container(
                            name="public-auth-proxy",
                            image="nginxinc/nginx-unprivileged:1.31-alpine",
                            ports=[
                                k8s.ContainerPort(name="public-auth", container_port=_PUBLIC_AUTH_PORT, protocol="TCP")
                            ],
                            volume_mounts=[
                                k8s.VolumeMount(
                                    name="public-auth-config", mount_path="/etc/nginx/conf.d", read_only=True
                                ),
                                k8s.VolumeMount(name="public-auth", mount_path="/etc/nginx/auth", read_only=True),
                            ],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("10m"),
                                    "memory": k8s.Quantity.from_string("32Mi"),
                                },
                                limits={"memory": k8s.Quantity.from_string("64Mi")},
                            ),
                            readiness_probe=_tcp_probe("public-auth", initial_delay_seconds=5, period_seconds=10),
                            liveness_probe=_tcp_probe("public-auth", initial_delay_seconds=20, period_seconds=20),
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False,
                                run_as_non_root=True,
                                run_as_user=101,
                                run_as_group=101,
                                capabilities=k8s.Capabilities(drop=["ALL"]),
                            ),
                        ),
                    ],
                    volumes=[
                        k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name="oci-cache-zot-config")),
                        k8s.Volume(
                            name="public-auth-config",
                            config_map=k8s.ConfigMapVolumeSource(name="oci-cache-public-auth-proxy"),
                        ),
                        k8s.Volume(
                            name="public-auth",
                            secret=k8s.SecretVolumeSource(
                                secret_name="puller-credential", items=[k8s.KeyToPath(key="htpasswd", path="htpasswd")]
                            ),
                        ),
                        k8s.Volume(
                            name="cache", empty_dir=k8s.EmptyDirVolumeSource(size_limit=k8s.Quantity.from_string("5Gi"))
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
            _VALKEY,
            _NAMESPACE,
            annotations={
                "description": (
                    "Shared dedupe/metadata cache for the Zot OCI pull-through cache. Zot's cacheDriver points here"
                    " (remoteCache); the durable content lives in S3, so this holds only rebuildable metadata/dedupe"
                    " state. Losing it is acceptable but may cause brief cache misses or require a Zot restart/Valkey"
                    " flush for stale metadb entries. Uses OVH HDD node-local storage so the operator can rebuild a"
                    " fresh Valkey replica without depending on the SeaweedFS CSI path."
                )
            },
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
                        "memory": RedisReplicationSpecKubernetesConfigResourcesLimits.from_string("256Mi"),
                    },
                ),
            ),
            redis_config=RedisReplicationSpecRedisConfig(max_memory_percent_of_limit=80),
            storage=RedisReplicationSpecStorage(
                volume_claim_template=RedisReplicationSpecStorageVolumeClaimTemplate(
                    spec=RedisReplicationSpecStorageVolumeClaimTemplateSpec(
                        access_modes=["ReadWriteOnce"],
                        storage_class_name="local-path-ovh-hdd",
                        resources=RedisReplicationSpecStorageVolumeClaimTemplateSpecResources(
                            requests={
                                "storage": RedisReplicationSpecStorageVolumeClaimTemplateSpecResourcesRequests.from_string(
                                    "2Gi"
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
                    # Prefer ordinary workers; this workload writes node-local cache state.
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
    chart = Chart(app, _NAMESPACE, disable_resource_name_hashes=True)
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=_NAMESPACE,
            labels={
                "goldilocks.fairwinds.com/enabled": "true",
                "goldilocks.fairwinds.com/vpa-update-mode": "auto",
                "rbac.ducktape.io/agent-readable-logs": "true",
            },
        ),
    )
    _deployment(chart)
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(name=_NAMESPACE, namespace=_NAMESPACE, labels=_LABELS),
        spec=k8s.ServiceSpec(
            selector=_LABELS,
            ports=[
                # Exposed on 80 (→ container 5000) for conventional registry addressing by
                # unrestricted consumers. NOTE: this does NOT let a port-restricted egress
                # policy reach the mirror on :80 — Cilium's socket-LB enforces egress on the
                # backend targetPort (5000), not this Service port. So haku-ci's force-proxy
                # egress allows 5000 explicitly (see haku_ci/runner.py).
                # Plain HTTP.
                k8s.ServicePort(name="http", port=80, target_port=k8s.IntOrString.from_string("http"), protocol="TCP"),
                # Authenticated public entrypoint. The HTTPRoute for oci-cache.allegedly.works
                # targets this port; in-cluster Docker mirrors must use the unauthenticated
                # `http` port above.
                k8s.ServicePort(
                    name="public-auth",
                    port=_PUBLIC_AUTH_PORT,
                    target_port=k8s.IntOrString.from_string("public-auth"),
                    protocol="TCP",
                ),
            ],
        ),
    )
    https_route(
        chart,
        "httproute",
        metadata=metadata(
            _NAMESPACE,
            _NAMESPACE,
            annotations={
                "description": (
                    "Authenticated public endpoint for the Zot pull-through cache. The cluster-gateway"
                    " terminates TLS for *.allegedly.works; this route targets the nginx sidecar on Service"
                    " port 8080, which enforces the puller-credential htpasswd before proxying to Zot."
                )
            },
        ),
        hostname="oci-cache.allegedly.works",
        backend=_NAMESPACE,
        port=_PUBLIC_AUTH_PORT,
        hsts=False,
        listener=None,
    )
    ServiceMonitor(
        chart,
        "servicemonitor",
        metadata=metadata(
            _NAME,
            _NAMESPACE,
            annotations={"description": "Zot OCI-cache application metrics scraped into Mimir by Alloy."},
        ),
        spec=ServiceMonitorSpec(
            selector=ServiceMonitorSpecSelector(match_labels=_LABELS),
            endpoints=[ServiceMonitorSpecEndpoints(port="http", path="/metrics", scrape_timeout="10s")],
        ),
    )
    _valkey(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)

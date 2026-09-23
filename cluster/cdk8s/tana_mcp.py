"""tana-mcp: Tana Desktop under Xvfb with its MCP proxy and Firebase re-signing sidecars, and the
public Authentik-backed MCP facade in front of it, with their config, RBAC, credentials,
Services, HTTPRoute and ServiceMonitor.

The images' tags are the placeholder "unset"; the hand-written `image-pins/kustomization.yaml`
overrides them at `kustomize build` time via Flux's image-automation markers
(cluster/cdk8s/AGENTS.md § the `:tag` Setters marker). Also hand-written beside the generated
output: `kustomization.yaml` (its configMapGenerator renders `nginx-auth-proxy.conf`), the
refresh-token SOPS Secret, the facade's OAuth-state `RedisReplication` and its
`PrometheusRule`, which have no binding.
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

from cluster.cdk8s.external_creds import add_external_secret
from cluster.cdk8s.forgejo_images import SECRET_NAME, forgejo_images_creds_external_secret
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

OUTPUT_DIR = "cluster/k8s/agents/tana-mcp"
_NAMESPACE = "tana-mcp"
_NAME = "tana-mcp"
_LABELS = {"app.kubernetes.io/name": _NAME}
_FACADE = "tana-mcp-facade"
_FACADE_LABELS = {"app.kubernetes.io/name": _FACADE}
_RESIGNER = "tana-firebase-resigner"
_RESIGNER_CONFIG = "tana-firebase-resigner-config"
_REFRESH_TOKEN_SECRET = "tana-firebase-refresh-token"
_PAT_SECRET = "tana-agentydragon-gmail-com-account-pat"
_FACADE_OIDC_SECRET = "tana-mcp-facade-oidc"
_TANA_PORT = 8262
_PROXY_PORT = 8263
_NOVNC_PORT = 6080
_FACADE_PORT = 8765
_METRICS_PORT = 9090
# Tana only serves /health on loopback inside the container.
_TANA_HEALTH = f"http://127.0.0.1:{_TANA_PORT}/health"


def _secret_env(name: str, secret: str, key: str) -> k8s.EnvVar:
    return k8s.EnvVar(
        name=name, value_from=k8s.EnvVarSource(secret_key_ref=k8s.SecretKeySelector(name=secret, key=key))
    )


def _tana_health_check() -> k8s.ExecAction:
    return k8s.ExecAction(command=["/usr/bin/curl", "--fail", "--silent", _TANA_HEALTH])


def _proxy_health_check() -> k8s.HttpGetAction:
    return k8s.HttpGetAction(path="/health", port=k8s.IntOrString.from_number(_PROXY_PORT))


def _tana_deployment(chart: Chart) -> None:
    k8s.KubeDeployment(
        chart,
        "tana-deployment",
        metadata=k8s.ObjectMeta(
            name=_NAME, namespace=_NAMESPACE, labels=_LABELS, annotations={"reloader.stakater.com/auto": "true"}
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            selector=k8s.LabelSelector(match_labels=_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS),
                spec=k8s.PodSpec(
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    node_selector={"topology.kubernetes.io/zone": "hil-ovh"},
                    service_account_name=_RESIGNER,
                    containers=[
                        # Tana Desktop running under Xvfb with noVNC for graphical admin access
                        k8s.Container(
                            name="tana-desktop",
                            image="git.allegedly.works/ducktape-ci/tana-desktop:unset",
                            ports=[
                                k8s.ContainerPort(name="mcp", container_port=_TANA_PORT, protocol="TCP"),
                                k8s.ContainerPort(name="novnc", container_port=_NOVNC_PORT, protocol="TCP"),
                            ],
                            env=[k8s.EnvVar(name="RESOLUTION", value="1280x800x24")],
                            volume_mounts=[k8s.VolumeMount(name="tana-config", mount_path="/home/tana/.config/tana")],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("200m"),
                                    "memory": k8s.Quantity.from_string("1Gi"),
                                },
                                limits={"memory": k8s.Quantity.from_string("4Gi")},
                            ),
                            # Kubelet httpGet probes target the pod IP, which stays unreachable
                            # even when Tana is healthy, so probe from inside the container instead.
                            # Generous startup probe: /health won't respond until Tana is logged in
                            # and MCP server is enabled (done manually via noVNC).
                            startup_probe=k8s.Probe(
                                exec=_tana_health_check(), failure_threshold=120, period_seconds=10
                            ),
                            liveness_probe=k8s.Probe(exec=_tana_health_check(), period_seconds=30, failure_threshold=5),
                            readiness_probe=k8s.Probe(exec=_tana_health_check(), period_seconds=15),
                        ),
                        # nginx sidecar: rewrites Host/Origin to localhost so Tana's MCP server
                        # accepts requests from cluster clients. Probe the proxy over HTTP so
                        # readiness reflects the actual cluster-facing path on :8263, not just
                        # that the port is open.
                        k8s.Container(
                            name="proxy",
                            image="nginx:alpine",
                            ports=[k8s.ContainerPort(name="mcp-proxy", container_port=_PROXY_PORT, protocol="TCP")],
                            volume_mounts=[
                                k8s.VolumeMount(name="nginx-config", mount_path="/etc/nginx/conf.d", read_only=True)
                            ],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("10m"),
                                    "memory": k8s.Quantity.from_string("32Mi"),
                                },
                                limits={"memory": k8s.Quantity.from_string("64Mi")},
                            ),
                            liveness_probe=k8s.Probe(http_get=_proxy_health_check(), period_seconds=30),
                            readiness_probe=k8s.Probe(http_get=_proxy_health_check(), period_seconds=10),
                        ),
                        # firebase_resigner: when the in-pod Tana's Firebase session is dead
                        # (probes /health on loopback), swap the SOPS-managed refresh token
                        # for an ID token, mint a Tana custom token via the fetchCustomToken
                        # Cloud Function, and deliver it to the desktop container's reseed
                        # receiver as a tana://auth deep-link. See
                        # cluster/docs/plans/tana_mcp_sane_signin.md.
                        k8s.Container(
                            name="firebase-resigner",
                            image="git.allegedly.works/ducktape-ci/tana-firebase-resigner:unset",
                            env_from=[k8s.EnvFromSource(config_map_ref=k8s.ConfigMapEnvSource(name=_RESIGNER_CONFIG))],
                            # Server-held PAT: readiness then also requires that Tana's MCP
                            # accepts it (POST /mcp initialize -> 200), so a renderer that
                            # drifts off the matching account drives a re-sign instead of
                            # silently leaving the facade serving zero tools.
                            env=[_secret_env("PAT", _PAT_SECRET, "token")],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("10m"),
                                    "memory": k8s.Quantity.from_string("64Mi"),
                                },
                                # TODO(vpa-memory-audit): 128Mi -> 256Mi. VPA observed a 121Mi
                                # request / 153Mi upper bound, i.e. already over the old limit.
                                limits={"memory": k8s.Quantity.from_string("256Mi")},
                            ),
                        ),
                    ],
                    volumes=[
                        # Tana's ~/.config/tana is just workspace cache + Firebase auth
                        # IndexedDB. Both are re-derivable on cold start: workspace from the
                        # upstream Tana RTDB, Firebase session re-minted by the
                        # firebase_resigner sidecar from the SOPS-managed refresh token. So
                        # this can stay an emptyDir — see
                        # cluster/docs/plans/tana_mcp_sane_signin.md.
                        k8s.Volume(name="tana-config", empty_dir=k8s.EmptyDirVolumeSource()),
                        # Rendered from nginx-auth-proxy.conf by the kustomization.yaml's configMapGenerator.
                        k8s.Volume(
                            name="nginx-config", config_map=k8s.ConfigMapVolumeSource(name="tana-mcp-nginx-config")
                        ),
                    ],
                ),
            ),
        ),
    )


def _resigner(chart: Chart) -> None:
    k8s.KubeConfigMap(
        chart,
        "resigner-config",
        metadata=k8s.ObjectMeta(name=_RESIGNER_CONFIG, namespace=_NAMESPACE),
        data={
            # Tana web bundle's REACT_APP_FIREBASE_API_KEY for project tagr-prod. This
            # Firebase web API key is a public identifier, not a secret.
            "API_KEY": "AIzaSyA9LtJM6Ga9VAwCfj9w_mNORdOaq2yLshQ",
            "SECRET_NAMESPACE": _NAMESPACE,
            "SECRET_NAME": _REFRESH_TOKEN_SECRET,
            "SECRET_KEY": "refresh_token",
            "FETCH_CUSTOM_TOKEN_URL": "https://app.tana.inc/functions/fetchCustomToken",
            "TANA_HEALTH_URL": _TANA_HEALTH,
            "TANA_MCP_URL": f"http://127.0.0.1:{_TANA_PORT}/mcp",
            "RESEED_URL": "http://127.0.0.1:9090/reseed",
            "HEALTHY_POLL_SECONDS": "60.0",
            "UNHEALTHY_POLL_SECONDS": "5.0",
            "UNHEALTHY_THRESHOLD": "3",
            "REQUEST_TIMEOUT_SECONDS": "30.0",
        },
    )
    k8s.KubeServiceAccount(
        chart, "resigner-service-account", metadata=k8s.ObjectMeta(name=_RESIGNER, namespace=_NAMESPACE)
    )
    k8s.KubeRole(
        chart,
        "resigner-role",
        metadata=k8s.ObjectMeta(name=_RESIGNER, namespace=_NAMESPACE),
        rules=[
            k8s.PolicyRule(
                api_groups=[""], resources=["secrets"], resource_names=[_REFRESH_TOKEN_SECRET], verbs=["get", "patch"]
            )
        ],
    )
    k8s.KubeRoleBinding(
        chart,
        "resigner-role-binding",
        metadata=k8s.ObjectMeta(name=_RESIGNER, namespace=_NAMESPACE),
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name=_RESIGNER),
        subjects=[k8s.Subject(kind="ServiceAccount", name=_RESIGNER, namespace=_NAMESPACE)],
    )


def _facade(chart: Chart) -> None:
    k8s.KubeConfigMap(
        chart,
        "facade-config",
        metadata=k8s.ObjectMeta(name="tana-mcp-facade-config", namespace=_NAMESPACE),
        data={
            "MCP_FACADE_AUTH__OIDC_ISSUER": "https://auth.allegedly.works/application/o/tana-mcp-facade/",
            "MCP_FACADE_AUTH__PUBLIC_BASE_URL": "https://tana-mcp-facade.allegedly.works",
            "MCP_FACADE_FACADE_NAME": "Tana MCP Facade",
            "MCP_FACADE_UPSTREAM__KIND": "http",
            "MCP_FACADE_UPSTREAM__URL": f"http://{_NAME}.{_NAMESPACE}.svc.cluster.local:{_PROXY_PORT}/mcp",
            "MCP_FACADE_PERSISTENCE__KIND": "valkey",
            # The hand-written mcp-valkey-ovh RedisReplication's primary Service.
            "MCP_FACADE_PERSISTENCE__HOST": f"mcp-valkey-ovh-master.{_NAMESPACE}.svc.cluster.local",
            "MCP_FACADE_PERSISTENCE__DB": "0",
            # Let Uvicorn's existing access log report the original client when requests
            # arrive through trusted in-cluster Gateway/Envoy paths.
            "FORWARDED_ALLOW_IPS": "10.42.0.0/16,10.244.0.0/16,127.0.0.1",
            "FASTMCP_ENABLE_RICH_LOGGING": "false",
            "FASTMCP_LOG_LEVEL": "INFO",
            "MCP_FACADE_LOGGING__MCP_MESSAGES": "true",
            "MCP_FACADE_LOGGING__MCP_PAYLOADS": "false",
            "MCP_FACADE_LOGGING__MCP_PAYLOAD_LENGTH": "true",
        },
    )
    healthz = k8s.HttpGetAction(path="/healthz", port=k8s.IntOrString.from_number(_FACADE_PORT))
    k8s.KubeDeployment(
        chart,
        "facade-deployment",
        metadata=k8s.ObjectMeta(
            name=_FACADE,
            namespace=_NAMESPACE,
            labels=_FACADE_LABELS,
            annotations={
                "description": (
                    "Public Authentik-backed MCP facade for Tana, running the shared mcp-oauth-facade image."
                    " Access is enforced by Authentik group membership; the server injects a static downstream"
                    " PAT."
                ),
                "reloader.stakater.com/auto": "true",
                # CPU: VPA manages requests only — no CPU limit so cold-start bursts aren't
                # throttled. fastmcp takes ~6 CPU-seconds to import; at a 60m limit that's
                # 100s wall time even on an idle node (cgroups CFS is a hard rate limiter
                # regardless of node load). CPU is compressible: without a limit the pod
                # bursts freely on idle capacity and gets its fair share when contended.
                # controlledValues applies to every resource in the policy, so memory
                # limits are NOT VPA-managed here either — the declared limit below is what
                # applies.
                "goldilocks.fairwinds.com/vpa-resource-policy": (
                    '{"containerPolicies":[{"containerName":"server","minAllowed":{"cpu":"25m"},'
                    '"controlledValues":"RequestsOnly"}]}\n'
                ),
            },
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=_FACADE_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_FACADE_LABELS),
                spec=k8s.PodSpec(
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    containers=[
                        k8s.Container(
                            name="server",
                            image="git.allegedly.works/ducktape-ci/mcp-oauth-facade:unset",
                            image_pull_policy="Always",
                            ports=[
                                k8s.ContainerPort(name="http", container_port=_FACADE_PORT, protocol="TCP"),
                                # Prometheus metrics, cluster-internal only (not on the HTTPRoute).
                                k8s.ContainerPort(name="metrics", container_port=_METRICS_PORT, protocol="TCP"),
                            ],
                            env_from=[
                                k8s.EnvFromSource(config_map_ref=k8s.ConfigMapEnvSource(name="tana-mcp-facade-config"))
                            ],
                            env=[
                                _secret_env("MCP_FACADE_AUTH__OIDC_CLIENT_ID", _FACADE_OIDC_SECRET, "client_id"),
                                _secret_env(
                                    "MCP_FACADE_AUTH__OIDC_CLIENT_SECRET", _FACADE_OIDC_SECRET, "client_secret"
                                ),
                                _secret_env("MCP_FACADE_UPSTREAM__BEARER_TOKEN", _PAT_SECRET, "token"),
                            ],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "memory": k8s.Quantity.from_string("128Mi"),
                                    "cpu": k8s.Quantity.from_string("50m"),
                                },
                                # TODO(vpa-memory-audit): 256Mi -> 512Mi. VPA observed a 259Mi
                                # request / 272Mi upper bound, i.e. already over the old limit.
                                limits={"memory": k8s.Quantity.from_string("512Mi")},
                            ),
                            startup_probe=k8s.Probe(
                                http_get=healthz, initial_delay_seconds=5, period_seconds=5, failure_threshold=60
                            ),
                            # Readiness reflects upstream tool availability, not just process
                            # liveness: /readyz is 200 only when the background probe recently
                            # listed >0 tools from the upstream. When Tana rejects the PAT (the
                            # recurring failure) the facade goes NotReady instead of silently
                            # serving an empty tool list. failureThreshold rides over a single
                            # probe blip; the /readyz staleness window debounces flapping.
                            readiness_probe=k8s.Probe(
                                http_get=k8s.HttpGetAction(
                                    path="/readyz", port=k8s.IntOrString.from_number(_FACADE_PORT)
                                ),
                                period_seconds=15,
                                failure_threshold=4,
                            ),
                            liveness_probe=k8s.Probe(http_get=healthz, period_seconds=20),
                        )
                    ],
                ),
            ),
        ),
    )
    https_route(
        chart,
        "facade-httproute",
        metadata=metadata(_FACADE, _NAMESPACE),
        hostname="tana-mcp-facade.allegedly.works",
        backend=_FACADE,
        port=_FACADE_PORT,
        timeout="60s",
        hsts=False,
        listener=None,
    )
    k8s.KubeService(
        chart,
        "facade-service",
        metadata=k8s.ObjectMeta(name=_FACADE, namespace=_NAMESPACE, labels=_FACADE_LABELS),
        spec=k8s.ServiceSpec(
            selector=_FACADE_LABELS,
            ports=[
                k8s.ServicePort(
                    name="http",
                    port=_FACADE_PORT,
                    target_port=k8s.IntOrString.from_number(_FACADE_PORT),
                    protocol="TCP",
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
    ServiceMonitor(
        chart,
        "facade-servicemonitor",
        metadata=metadata(_FACADE, _NAMESPACE),
        spec=ServiceMonitorSpec(
            selector=ServiceMonitorSpecSelector(match_labels=_FACADE_LABELS),
            endpoints=[ServiceMonitorSpecEndpoints(port="metrics", path="/metrics", scrape_timeout="10s")],
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
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
    k8s.KubeServiceAccount(
        chart,
        "external-creds-reader",
        metadata=k8s.ObjectMeta(
            name="external-creds-reader",
            namespace=_NAMESPACE,
            annotations={"description": "ESO referent identity for credentials approved by external-creds."},
        ),
        automount_service_account_token=False,
    )
    add_external_secret(
        chart,
        "tana-pat",
        namespace=_NAMESPACE,
        source_name=_PAT_SECRET,
        property_name="token",
        description="ESO copy of the canonical Tana PAT from external-creds.",
    )
    forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=_NAMESPACE)
    _resigner(chart)
    _tana_deployment(chart)
    k8s.KubeService(
        chart,
        "tana-service",
        metadata=k8s.ObjectMeta(name=_NAME, namespace=_NAMESPACE),
        spec=k8s.ServiceSpec(
            type="ClusterIP",
            selector=_LABELS,
            ports=[
                k8s.ServicePort(
                    name="mcp-proxy",
                    port=_PROXY_PORT,
                    target_port=k8s.IntOrString.from_string("mcp-proxy"),
                    protocol="TCP",
                ),
                k8s.ServicePort(
                    name="novnc", port=_NOVNC_PORT, target_port=k8s.IntOrString.from_string("novnc"), protocol="TCP"
                ),
            ],
        ),
    )
    _facade(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)

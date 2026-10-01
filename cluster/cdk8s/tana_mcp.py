"""tana-mcp: Tana Desktop under Xvfb with its MCP proxy and Firebase re-signing sidecars, and the
public Authentik-backed MCP facade in front of it, with their config, RBAC, credentials,
Services, HTTPRoute, ServiceMonitor and PrometheusRule.

The images' tags are the placeholder "unset"; the hand-written `image-pins/kustomization.yaml`
overrides them at `kustomize build` time via Flux's image-automation markers
(cluster/cdk8s/AGENTS.md § the `:tag` Setters marker). Also hand-written beside the generated
output: `kustomization.yaml` (its configMapGenerator renders `nginx-auth-proxy.conf`) and the
refresh-token SOPS Secret.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector

from cluster.cdk8s import external_creds, namespaces, node_scheduling
from cluster.cdk8s.forgejo_images import SECRET_NAME, forgejo_images_creds_external_secret
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.mcp_oauth_state import CONSUMER_SECRET, TANA, add_consumer_credentials
from cluster.cdk8s.namespaces import AgentReadable, Vpa
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, remote_data
from cluster.cdk8s.providers.prometheus_operator.prometheus_rule import PrometheusRule, Rule, group
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/agents/tana-mcp"
_NAMESPACE = "tana-mcp"
_NAME = "tana-mcp"
_FACADE = "tana-mcp-facade"
_RESIGNER = "tana-firebase-resigner"
_RESIGNER_CONFIG = "tana-firebase-resigner-config"
# The Secret the resigner rewrites: its Role and its config both read this one reference.
_REFRESH_TOKEN = SecretRef(namespace=_NAMESPACE, name="tana-firebase-refresh-token").key("refresh_token")
# The ESO copy of the external-creds Secret of the same name.
_PAT = SecretRef(namespace=_NAMESPACE, name="tana-agentydragon-gmail-com-account-pat").key("token")
_FACADE_OIDC = SecretRef(namespace=_NAMESPACE, name="tana-mcp-facade-oidc")
# Tana Desktop's own MCP server; it answers only on loopback inside the container.
_TANA = Port(name="mcp", number=8262)
# Tana's MCP through the nginx sidecar, which rewrites Host/Origin so Tana accepts cluster
# clients: the facade's upstream, and agentplane-staging's Action Service calls it too.
MCP_PROXY = ServiceRef(
    name=_NAME,
    port=Port(name="mcp-proxy", number=8263),
    pods=Pods(namespace=_NAMESPACE, labels=(("app.kubernetes.io/name", _NAME),)),
)
_NOVNC = ServiceRef(name=MCP_PROXY.name, port=Port(name="novnc", number=6080), pods=MCP_PROXY.pods)
_FACADE_HTTP = ServiceRef(
    name=_FACADE,
    port=Port(name="http", number=8765),
    pods=Pods(namespace=_NAMESPACE, labels=(("app.kubernetes.io/name", _FACADE),)),
)
_FACADE_METRICS = ServiceRef(name=_FACADE_HTTP.name, port=Port(name="metrics", number=9090), pods=_FACADE_HTTP.pods)
_TANA_HEALTH = f"http://127.0.0.1:{_TANA.number}/health"


def _tana_health_check() -> k8s.ExecAction:
    return k8s.ExecAction(command=["/usr/bin/curl", "--fail", "--silent", _TANA_HEALTH])


def _proxy_health_check() -> k8s.HttpGetAction:
    return k8s.HttpGetAction(path="/health", port=k8s.IntOrString.from_number(MCP_PROXY.pod_port))


def _tana_deployment(chart: Chart) -> None:
    k8s.KubeDeployment(
        chart,
        "tana-deployment",
        metadata=k8s.ObjectMeta(name=_NAME, namespace=_NAMESPACE, labels=MCP_PROXY.pods.selector),
        spec=k8s.DeploymentSpec(
            replicas=1,
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            selector=k8s.LabelSelector(match_labels=MCP_PROXY.pods.selector),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=MCP_PROXY.pods.selector),
                spec=k8s.PodSpec(
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    node_selector=node_scheduling.HIL_OVH_NODE_SELECTOR,
                    service_account_name=_RESIGNER,
                    # The firebase-resigner sidecar reads and patches the refresh-token Secret
                    # through load_incluster_config() (tana/firebase_resigner/resigner.py).
                    automount_service_account_token=True,
                    containers=[
                        # Tana Desktop running under Xvfb with noVNC for graphical admin access
                        k8s.Container(
                            name="tana-desktop",
                            image="git.allegedly.works/ducktape-ci/tana-desktop:unset",
                            ports=[_TANA.k8s_container_port(), _NOVNC.port.k8s_container_port()],
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
                            ports=[MCP_PROXY.port.k8s_container_port()],
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
                            env=[_PAT.env_var("PAT")],
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
            "SECRET_NAMESPACE": _REFRESH_TOKEN.secret.namespace,
            "SECRET_NAME": _REFRESH_TOKEN.secret.name,
            "SECRET_KEY": _REFRESH_TOKEN.key,
            "FETCH_CUSTOM_TOKEN_URL": "https://app.tana.inc/functions/fetchCustomToken",
            "TANA_HEALTH_URL": _TANA_HEALTH,
            "TANA_MCP_URL": f"http://127.0.0.1:{_TANA.number}/mcp",
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
                api_groups=[""],
                resources=["secrets"],
                resource_names=[_REFRESH_TOKEN.secret.name],
                verbs=["get", "patch"],
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
            "MCP_FACADE_UPSTREAM__URL": f"{MCP_PROXY.url}/mcp",
            "MCP_FACADE_PERSISTENCE__KIND": "postgres",
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
    healthz = k8s.HttpGetAction(path="/healthz", port=k8s.IntOrString.from_number(_FACADE_HTTP.pod_port))
    k8s.KubeDeployment(
        chart,
        "facade-deployment",
        metadata=k8s.ObjectMeta(
            name=_FACADE,
            namespace=_NAMESPACE,
            labels=_FACADE_HTTP.pods.selector,
            annotations={
                "description": (
                    "Public Authentik-backed MCP facade for Tana, running the shared mcp-oauth-facade image."
                    " Access is enforced by Authentik group membership; the server injects a static downstream"
                    " PAT."
                ),
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
            selector=k8s.LabelSelector(match_labels=_FACADE_HTTP.pods.selector),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_FACADE_HTTP.pods.selector),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    containers=[
                        k8s.Container(
                            name="server",
                            image="git.allegedly.works/ducktape-ci/mcp-oauth-facade:unset",
                            image_pull_policy="Always",
                            ports=[
                                _FACADE_HTTP.port.k8s_container_port(),
                                # Prometheus metrics, cluster-internal only (not on the HTTPRoute).
                                _FACADE_METRICS.port.k8s_container_port(),
                            ],
                            env_from=[
                                k8s.EnvFromSource(config_map_ref=k8s.ConfigMapEnvSource(name="tana-mcp-facade-config"))
                            ],
                            env=[
                                _FACADE_OIDC.key("client_id").env_var("MCP_FACADE_AUTH__OIDC_CLIENT_ID"),
                                _FACADE_OIDC.key("client_secret").env_var("MCP_FACADE_AUTH__OIDC_CLIENT_SECRET"),
                                _PAT.env_var("MCP_FACADE_UPSTREAM__BEARER_TOKEN"),
                                SecretRef(namespace=_NAMESPACE, name=CONSUMER_SECRET)
                                .key("uri")
                                .env_var("MCP_FACADE_PERSISTENCE__URL"),
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
                                    path="/readyz", port=k8s.IntOrString.from_number(_FACADE_HTTP.pod_port)
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
        metadata=ApiObjectMetadata(name=_FACADE, namespace=_NAMESPACE),
        hostnames=["tana-mcp-facade.allegedly.works"],
        backend=_FACADE_HTTP,
        timeout="60s",
        hsts=False,
        listener=None,
    )
    k8s.KubeService(
        chart,
        "facade-service",
        metadata=k8s.ObjectMeta(name=_FACADE_HTTP.name, namespace=_NAMESPACE, labels=_FACADE_HTTP.labels),
        spec=k8s.ServiceSpec(
            selector=_FACADE_HTTP.pods.selector,
            ports=[_FACADE_HTTP.port.k8s_service_port(), _FACADE_METRICS.port.k8s_service_port()],
            type="ClusterIP",
        ),
    )
    ServiceMonitor(
        chart,
        "facade-servicemonitor",
        metadata=ApiObjectMetadata(name=_FACADE, namespace=_NAMESPACE),
        selector=ServiceMonitorSpecSelector(match_labels=_FACADE_HTTP.labels),
        endpoints=[Endpoint.plain(port=_FACADE_METRICS.port.name, scrape_timeout="10s")],
    )
    PrometheusRule(
        chart,
        "facade-prometheusrule",
        metadata=ApiObjectMetadata(name=_FACADE, namespace=_NAMESPACE),
        groups=[
            group(
                _FACADE,
                [
                    # The facade can be "up" (process healthy) while serving zero tools
                    # because the upstream Tana MCP rejects the server-held PAT. These
                    # alerts fire on that condition — the recurring failure that silently
                    # leaves claude.ai with no Tana tools.
                    Rule.alert(
                        "TanaMcpFacadeUpstreamDown",
                        "mcp_facade_upstream_up == 0",
                        for_="5m",
                        labels={"severity": "warning"},
                        summary="MCP facade {{ $labels.facade }} cannot reach its upstream",
                        description=(
                            "The upstream tools/list probe for facade {{ $labels.facade }} has been failing for >5m. Clients see no "
                            "tools. For Tana this usually means the desktop renderer is rejecting the PAT (validateToken); check the "
                            "firebase-resigner logs and the tana-mcp pod sign-in.\n"
                        ),
                    ),
                    Rule.alert(
                        "TanaMcpFacadeNoTools",
                        "mcp_facade_upstream_up == 1 and mcp_facade_upstream_tools == 0",
                        for_="5m",
                        labels={"severity": "warning"},
                        summary="MCP facade {{ $labels.facade }} reachable but exposes zero tools",
                        description=(
                            "Facade {{ $labels.facade }} reached its upstream but it advertised no tools for >5m. The upstream MCP "
                            "server is up but empty.\n"
                        ),
                    ),
                    Rule.alert(
                        "TanaMcpFacadeProbeStale",
                        "time() - mcp_facade_upstream_last_success_timestamp_seconds > 600",
                        for_="5m",
                        labels={"severity": "warning"},
                        summary="MCP facade {{ $labels.facade }} has no recent successful probe",
                        description=(
                            "No successful upstream probe for facade {{ $labels.facade }} in >10m (probe loop wedged or upstream "
                            "persistently failing).\n"
                        ),
                    ),
                ],
            )
        ],
    )


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=_NAMESPACE, vpa=Vpa.AUTO, agent_readable=AgentReadable.LOGS)
    add_consumer_credentials(chart, TANA)
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
    ExternalSecret(
        chart,
        "tana-pat",
        metadata=ApiObjectMetadata(
            name=_PAT.secret.name,
            namespace=_PAT.secret.namespace,
            annotations={"description": "ESO copy of the canonical Tana PAT from external-creds."},
        ),
        refresh_interval="1h",
        secret_store_ref=external_creds.STORE,
        data=[remote_data(_PAT.secret.name, _PAT.key)],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
    )
    forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=_NAMESPACE)
    _resigner(chart)
    _tana_deployment(chart)
    k8s.KubeService(
        chart,
        "tana-service",
        metadata=k8s.ObjectMeta(name=MCP_PROXY.name, namespace=_NAMESPACE, labels=MCP_PROXY.labels),
        spec=k8s.ServiceSpec(
            type="ClusterIP",
            selector=MCP_PROXY.pods.selector,
            ports=[MCP_PROXY.port.k8s_service_port(), _NOVNC.port.k8s_service_port()],
        ),
    )
    _facade(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)

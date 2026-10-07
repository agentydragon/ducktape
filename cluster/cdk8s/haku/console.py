"""The Haku Console API and its public shell: the API Deployment (reviewed FastAPI code) with
its ServiceAccount, RBAC, config ConfigMap, Service and ServiceMonitor; the separate
`static` nginx Deployment/Service the public HTTPRoute reaches, which proxies
backend paths to the API.

Trust boundary: the console runs in its OWN `haku-console` namespace, not haku-sandbox. As
reviewed/released ducktape code it sits outside Haku's RBAC (Haku cannot read its secrets or
logs or patch it) and outside the haku-egress-proxy fence, so it gets ordinary egress and can
hold secrets Haku may not read (haku/PLAN.md, "The agent-authored console").

The API and static shell are separate Deployments so a frontend-only image update never
restarts operator authentication, MCP streams, or background workers. Both images carry the
`unset` placeholder tag image-pins/ overrides (cluster/docs/cdk8s.md); the API's own tag also
reaches the Settings panel through the hand-written `image-metadata.yaml` ConfigMap, which
carries the Flux marker a generated file cannot.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ApiResource,
    ClusterRole,
    ClusterRoleBinding,
    ConfigMap,
    ContainerPort,
    ContainerResources,
    ContainerSecurityContextProps,
    Cpu,
    CpuResources,
    Deployment,
    DeploymentStrategy,
    EnvValue,
    ImagePullPolicy,
    ISecret,
    LabelSelector,
    MemoryResources,
    PercentOrAbsolute,
    Pods,
    PodSecurityContextProps,
    Probe,
    Protocol,
    Role,
    RoleBinding,
    RolePolicyRule,
    Secret,
    SecretValue,
    Service,
    ServiceAccount,
    ServicePort,
    Volume,
    k8s,
)
from constructs import Construct
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector

from cluster.cdk8s import agent_access_profiles as access, node_scheduling, pod_policy, service_ref
from cluster.cdk8s.forgejo_images import forgejo_images_creds_secret_ref
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.haku import console_config, database
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor
from cluster.cdk8s.secret_ref import SecretKey, SecretRef
from cluster.cdk8s.settings_file import SettingsFile
from haku.console.config import CONFIG_FILE_ENV
from haku.console.mcp_config import ConsoleConfigFile
from haku.console.settings import Settings
from util.settings_contract import checked_value, env_name

NAMESPACE = "haku-console"
NAME = "haku-console"
HOSTNAME = "haku.allegedly.works"
PUBLIC_BASE_URL = f"https://{HOSTNAME}"
IMAGE = "git.allegedly.works/ducktape-ci/haku-console"
_STATIC_IMAGE = "git.allegedly.works/ducktape-ci/haku-console-static"
PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
# The public route targets the static shell; this port serves nginx's in-cluster proxy and any
# trusted in-cluster API consumer.
_API = service_ref.ServiceRef(
    name=NAME,
    port=service_ref.Port(name="api", number=8080),
    pods=service_ref.Pods(namespace=NAMESPACE, labels=(("app.kubernetes.io/name", NAME),)),
)
# The ServiceMonitor's target: nginx deliberately does not proxy /metrics, so this names the API
# container without making metrics public.
_METRICS = service_ref.ServiceRef(
    name=_API.name, port=service_ref.Port(name="metrics", number=9090), pods=_API.pods, target_port=_API.pod_port
)
_STATIC = service_ref.ServiceRef(
    name="static",
    port=service_ref.Port(name="http", number=8080),
    pods=service_ref.Pods(namespace=NAMESPACE, labels=(("app.kubernetes.io/name", "haku-console-static"),)),
    target_port=8081,
)
# Hand-written siblings carrying Flux image-automation markers: the static image's tag,
# projected as a file so /api/deployment reports the frontend revision without a
# frontend-only release rolling the API, and the API image's own tag as an env var.
STATIC_METADATA_CONFIG_MAP = "static-metadata"
IMAGE_METADATA_CONFIG_MAP = "image-metadata"
_IMAGE_TAG_KEY = "image-tag"
_STATIC_METADATA_DIR = "/etc/haku-console/static-metadata"
_AUTHENTIK = "https://auth.allegedly.works"

_DB_ENV = {
    "HAKU_CONSOLE_DB_USER": "username",
    "HAKU_CONSOLE_DB_PASSWORD": "password",
    "HAKU_CONSOLE_DB_HOST": "host",
    "HAKU_CONSOLE_DB_PORT": "port",
    "HAKU_CONSOLE_DB_NAME": "dbname",
}
_DB_AUTHORITY = (
    "$(HAKU_CONSOLE_DB_USER):$(HAKU_CONSOLE_DB_PASSWORD)@$(HAKU_CONSOLE_DB_HOST):$(HAKU_CONSOLE_DB_PORT)"
    "/$(HAKU_CONSOLE_DB_NAME)"
)


def database_env(scope: Construct) -> dict[str, EnvValue]:
    """The CNPG app credential's parts and the SQLAlchemy asyncpg URL assembled from them, as the
    API and the migration Job both read them."""
    return {
        **{
            name: database.POSTGRES.app_secret.key(key).env_value(scope, f"db-app-{key}")
            for name, key in _DB_ENV.items()
        },
        env_name(Settings, "database_url"): EnvValue.from_value(f"postgresql+asyncpg://{_DB_AUTHORITY}"),
    }


class Console(Construct):
    """The API and static-shell Deployments with everything they need."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        self._secrets: dict[str, ISecret] = {}
        # Settings-file leaves the Deployment supplies from Secrets, so the file validates as
        # the whole model with those filled in (SettingsFile's `supplied`).
        self._supplied: list[tuple[str, ...]] = []

        # Narrow runtime identity: SubjectAccessReview for the Kubernetes-grant flow, plus
        # claim/exec RBAC on the haku-sandbox pool (haku/workspaces.py). No Secret, log, exec, or SandboxTemplate access
        # outside that pool.
        service_account = ServiceAccount(
            self, "serviceaccount", metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE), automount_token=True
        )
        self._add_rbac(service_account)
        env = self._container_env()
        config = SettingsFile(
            self,
            "config",
            metadata=ApiObjectMetadata(name="config", namespace=NAMESPACE),
            model=ConsoleConfigFile,
            content=console_config.config(),
            path="/etc/haku-console/config/config.yaml",
            supplied=[path for path in self._supplied if path[0] in ConsoleConfigFile.model_fields],
        )
        self._add_deployment(service_account, env, config)
        self._add_service()
        self._add_service_monitor()
        self._add_static()
        self._add_http_route()

    def _add_rbac(self, service_account: ServiceAccount) -> None:
        # SubjectAccessReview is advisory only: it cannot mutate cluster state and gives the
        # console none of the reviewed subject's authority. The proxy ServiceAccount
        # (kube_api_proxy.py) is the only identity that executes an allowed request.
        sar_reviewer = "haku-console-subject-access-reviewer"
        k8s.KubeClusterRole(
            self,
            "sar-reviewer-role",
            metadata=k8s.ObjectMeta(
                name=sar_reviewer,
                annotations={"description": "Allows Haku Console to evaluate configured Kubernetes SAR subjects."},
            ),
            rules=[
                k8s.PolicyRule(
                    api_groups=["authorization.k8s.io"], resources=["subjectaccessreviews"], verbs=["create"]
                )
            ],
        )
        ClusterRoleBinding(
            self,
            "sar-reviewer-binding",
            metadata=ApiObjectMetadata(
                name=sar_reviewer,
                annotations={
                    "description": "Binds only the Haku Console ServiceAccount to SubjectAccessReview creation."
                },
            ),
            role=ClusterRole.from_cluster_role_name(self, "sar-reviewer-role-ref", sar_reviewer),
        ).add_subjects(service_account)
        # Narrow metadata/configuration diagnostics for the agents. Not
        # namespace-diagnostics-reader: that broader role includes pods/log, and console
        # logs can contain operator and personal-service data.
        diagnostics = "agent-haku-console-metadata-reader"
        Role(
            self,
            "diagnostics-role",
            metadata=ApiObjectMetadata(
                name=diagnostics,
                namespace=NAMESPACE,
                annotations={"description": "Read-only Console workload, event, and public ConfigMap metadata."},
            ),
            rules=[
                RolePolicyRule(
                    resources=[ApiResource.PODS, ApiResource.EVENTS, ApiResource.CONFIG_MAPS],
                    verbs=["get", "list", "watch"],
                ),
                RolePolicyRule(resources=[ApiResource.DEPLOYMENTS], verbs=["get", "list", "watch"]),
            ],
        )
        RoleBinding(
            self,
            "diagnostics-rolebinding",
            metadata=ApiObjectMetadata(
                name=diagnostics,
                namespace=NAMESPACE,
                annotations={"description": "Binds Haku and public-coder to narrow Console metadata diagnostics."},
            ),
            role=Role.from_role_name(self, "diagnostics-role-ref", diagnostics),
        ).add_subjects(
            *[
                subject.imported(self, f"diagnostics-subject-{index}")
                for index, subject in enumerate(access.profile_subjects("haku-console-metadata"))
            ]
        )
        # Consumer-owned referent identity for source-approved external credentials.
        ServiceAccount(
            self,
            "external-creds-reader",
            metadata=ApiObjectMetadata(name="external-creds-reader", namespace=NAMESPACE),
            automount_token=False,
        )

    def _secret(self, name: str) -> ISecret:
        if name not in self._secrets:
            self._secrets[name] = Secret.from_secret_name(self, f"secret-{name}", name)
        return self._secrets[name]

    def _from_secret(self, key: SecretKey, *path: str, optional: bool = False) -> tuple[str, EnvValue]:
        """The env var for the settings leaf at `path`, read from `key`."""
        self._supplied.append(path)
        return env_name(Settings, *path), EnvValue.from_secret_value(
            SecretValue(secret=self._secret(key.secret.name), key=key.key), optional=optional
        )

    def _container_env(self) -> dict[str, EnvValue]:
        oidc = SecretRef(namespace=NAMESPACE, name="haku-console-oidc")
        image_metadata = ConfigMap.from_config_map_name(self, "image-metadata-ref", IMAGE_METADATA_CONFIG_MAP)
        return dict(
            [
                (env_name(Settings, "image_tag"), EnvValue.from_config_map(image_metadata, _IMAGE_TAG_KEY)),
                (
                    env_name(Settings, "aiquota_url"),
                    EnvValue.from_value("http://aiquota-api.cli-proxy-api.svc.cluster.local:8080"),
                ),
                self._from_secret(
                    SecretRef(namespace=NAMESPACE, name="aiquota-api-bearer-haku-console").key("bearer-token"),
                    "aiquota_bearer_token",
                ),
                # Capability tier: the launch-routine action. The console builds both the fire
                # URL and the claude.ai/code deep-link from this routine (trigger) id; the bearer
                # lives only in this namespace, so Haku cannot read it.
                (
                    env_name(Settings, "launch_routine", "routine_id"),
                    EnvValue.from_value("trig_0158pCMU1XBhoALBWikwSyK4"),
                ),
                self._from_secret(
                    SecretRef(namespace=NAMESPACE, name="haku-routine-launch-token").key("token"),
                    "launch_routine",
                    "token",
                ),
                # Web Push: the VAPID key the console signs approval notifications with. Its
                # public half is derived at startup and handed to each browser at subscribe
                # time, so rotating it makes every enrolled device re-subscribe.
                self._from_secret(
                    SecretRef(namespace=NAMESPACE, name="haku-console-web-push-vapid").key("private-key-pem"),
                    "web_push",
                    "private_key_pem",
                ),
                (env_name(Settings, "web_push", "subject"), EnvValue.from_value("mailto:agentydragon@gmail.com")),
                # The Authentik-gated origin of Haku's own UI, framed as a sandboxed cross-origin
                # iframe (haku/console/docs/containment.md).
                (env_name(Settings, "haku_ui_url"), EnvValue.from_value("https://haku-ui.allegedly.works")),
                (env_name(Settings, "auth_origin"), EnvValue.from_value(_AUTHENTIK)),
                (env_name(Settings, "public_base_url"), EnvValue.from_value(PUBLIC_BASE_URL)),
                (
                    env_name(Settings, "static_image_tag_file"),
                    EnvValue.from_value(f"{_STATIC_METADATA_DIR}/{_IMAGE_TAG_KEY}"),
                ),
                # Must leave margin below the public route's request timeout.
                (
                    env_name(Settings, "max_wait_for_result_ms"),
                    EnvValue.from_value(str(checked_value(Settings, "max_wait_for_result_ms", 60_000))),
                ),
                *database_env(self).items(),
                # The static Agents' bearers; the durable Agent UUIDs and display names are in
                # the config file.
                self._from_secret(
                    SecretRef(namespace=NAMESPACE, name="haku-console-agent-api").key("token"),
                    "static_agents",
                    "haku",
                    "token",
                ),
                # public-coder-agent's bearer reaches only its iron-proxy; the OpenClaw
                # container sees a non-secret placeholder.
                self._from_secret(
                    SecretRef(namespace=NAMESPACE, name="haku-console-public-coder-agent").key("token"),
                    "static_agents",
                    "public_coder",
                    "token",
                ),
                # The Operator each static Agent acts as: the controller-fed Authentik user id,
                # resolved through the identity trust domain to a canonical Operator UUID and never
                # live request authority.
                self._from_secret(oidc.key("operator_subject"), "static_agents", "haku", "operator_subject"),
                self._from_secret(oidc.key("operator_subject"), "static_agents", "public_coder", "operator_subject"),
                # Agent-facing MCP OAuth: an Authentik-backed OIDCProxy (DCR + PKCE) on /mcp,
                # composed with the static bearers via MultiAuth. Provider and client secret are
                # minted by tf/gitops/agent-machine-access (application slug haku-console-mcp);
                # the public MCP URL is derived from public_base_url in-app.
                (
                    env_name(Settings, "mcp_oauth", "oidc_issuer"),
                    EnvValue.from_value(f"{_AUTHENTIK}/application/o/haku-console-mcp/"),
                ),
                self._from_secret(oidc.key("mcp_client_id"), "mcp_oauth", "oidc_client_id"),
                self._from_secret(oidc.key("mcp_client_secret"), "mcp_oauth", "oidc_client_secret"),
                # DCR + token state shared across the replicas, in the console's own Postgres
                # (py-key-value's PostgreSQLStore auto-creates its table). The asyncpg DSN, not
                # the SQLAlchemy `+asyncpg` URL the ORM uses.
                (env_name(Settings, "mcp_oauth", "persistence", "kind"), EnvValue.from_value("postgres")),
                (
                    env_name(Settings, "mcp_oauth", "persistence", "url"),
                    EnvValue.from_value(f"postgresql://{_DB_AUTHORITY}"),
                ),
                # Operator browser login: the console authenticates the operator itself
                # (authorization-code -> signed session cookie) with the provider tf/gitops/
                # agent-machine-access mints under application slug haku-console. Required.
                (
                    env_name(Settings, "operator_oidc", "issuer"),
                    EnvValue.from_value(f"{_AUTHENTIK}/application/o/haku-console/"),
                ),
                self._from_secret(oidc.key("operator_client_id"), "operator_oidc", "client_id"),
                self._from_secret(oidc.key("operator_client_secret"), "operator_oidc", "client_secret"),
                self._from_secret(oidc.key("operator_session_secret"), "operator_oidc", "session_secret"),
                # The Authentik user-id namespace both providers above share (sub_mode=user_id).
                (
                    env_name(Settings, "operator_identity", "trust_domain"),
                    EnvValue.from_value("auth.allegedly.works/authentik-user-id/v1"),
                ),
            ]
        )

    def _add_deployment(self, service_account: ServiceAccount, env: dict[str, EnvValue], config: SettingsFile) -> None:
        deployment = Deployment(
            self,
            "deployment",
            metadata=ApiObjectMetadata(
                name=NAME,
                namespace=NAMESPACE,
                labels=_API.pods.selector,
                annotations={
                    # Only the API's own mounted ConfigMap and credentials, never reloader's
                    # blanket auto mode: the projected static-image metadata changes on every
                    # frontend release and must not restart API/MCP/background work.
                    "configmap.reloader.stakater.com/reload": config.config_map.name,
                    "secret.reloader.stakater.com/reload": ",".join(sorted(self._secrets)),
                },
            ),
            pod_metadata=ApiObjectMetadata(labels=_API.pods.selector),
            select=False,
            replicas=2,
            # Keep one replica serving while the controller frees capacity for its replacement:
            # requiring a surge pod deadlocked secret-triggered rollouts when the eligible nodes
            # lacked spare CPU or memory.
            strategy=DeploymentStrategy.rolling_update(
                max_surge=PercentOrAbsolute.absolute(0), max_unavailable=PercentOrAbsolute.absolute(1)
            ),
            service_account=service_account,
            automount_service_account_token=True,
            docker_registry_auth=forgejo_images_creds_secret_ref(self, "forgejo-images-creds-ref"),
            # Above uvicorn's 10s graceful wait (app.main) plus the lifespan handing chat-session
            # leases back; the default 30s hid the dependency.
            termination_grace_period=Duration.seconds(25),
            security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000, fs_group=1000),
        )
        deployment.select(LabelSelector.of(labels=_API.pods.selector))
        container = deployment.add_container(
            name="server",
            image=f"{IMAGE}:{PLACEHOLDER_TAG}",
            image_pull_policy=ImagePullPolicy.ALWAYS,
            ports=[_API.port.container_port()],
            env_variables=env,
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50), limit=Cpu.millis(500)),
                # TODO(vpa-memory-audit): 512Mi -> 3Gi. VPA observed a 1.29Gi request / 1.74Gi
                # upper bound; a Node static-file + API server wanting 1.3Gi resident looks
                # wrong -- check for a leak before accepting.
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.gibibytes(3)),
            ),
            # The image-coupled migration Job completes before this workload reconciles; normal
            # boot still constructs authenticated clients and validates schema read
            # compatibility before binding its port. Five minutes separates "still starting"
            # from "wedged".
            startup=Probe.from_tcp_socket(port=_API.pod_port, failure_threshold=60, period_seconds=Duration.seconds(5)),
            # httpGet /healthz, not tcpSocket: the port stays open after FastMCP's
            # StreamableHTTPSessionManager task group wedges (haku/console/identity/
            # fastmcp_adapter.py, `mcp_session_manager_liveness`), so only /healthz notices a
            # replica stuck 500ing every /mcp request.
            liveness=http_probe("/healthz", port=_API.pod_port, initial_delay_seconds=10, period_seconds=30),
            readiness=http_probe("/healthz", port=_API.pod_port, initial_delay_seconds=5, period_seconds=10),
            # The aspect py_binary launcher materializes its venv on the rootfs at startup.
            security_context=ContainerSecurityContextProps(read_only_root_filesystem=False),
        )
        container.mount("/tmp", Volume.from_empty_dir(self, "tmp-volume", "tmp"))
        config.mount_into(container, env=CONFIG_FILE_ENV)
        static_metadata = ConfigMap.from_config_map_name(self, "static-metadata-ref", STATIC_METADATA_CONFIG_MAP)
        container.mount(
            _STATIC_METADATA_DIR,
            Volume.from_config_map(self, "static-metadata-volume", static_metadata),
            read_only=True,
        )
        # Same zone as this console's Postgres and the public ingress: unpinned, a replica
        # landed 166ms from the database, which every read pays several round trips of.
        pod_policy.place(deployment, node_scheduling.HIL_OVH)
        pod_policy.harden(deployment)

    def _add_service(self) -> None:
        Service(
            self,
            "service",
            metadata=ApiObjectMetadata(name=_API.name, namespace=NAMESPACE, labels=_API.labels),
            selector=Pods.select(self, "pods", labels=_API.pods.selector),
            ports=[
                _API.port.service_port(),
                ServicePort(
                    name=_METRICS.port.name,
                    port=_METRICS.port.number,
                    target_port=_METRICS.pod_port,
                    protocol=Protocol.TCP,
                ),
            ],
        )

    def _add_service_monitor(self) -> None:
        ServiceMonitor(
            self,
            "servicemonitor",
            metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
            selector=ServiceMonitorSpecSelector(match_labels=_API.labels),
            endpoints=[Endpoint.plain(port=_METRICS.port.name)],
        )

    def _add_static(self) -> None:
        """The public, unprivileged shell: no Kubernetes, database, OAuth, or MCP credential."""
        deployment = Deployment(
            self,
            "static-deployment",
            metadata=ApiObjectMetadata(name=_STATIC.name, namespace=NAMESPACE, labels=_STATIC.pods.selector),
            pod_metadata=ApiObjectMetadata(labels=_STATIC.pods.selector),
            select=False,
            replicas=2,
            strategy=DeploymentStrategy.rolling_update(
                max_surge=PercentOrAbsolute.absolute(1), max_unavailable=PercentOrAbsolute.absolute(0)
            ),
            automount_service_account_token=False,
            docker_registry_auth=forgejo_images_creds_secret_ref(self, "static-forgejo-images-creds-ref"),
            security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000, fs_group=1000),
        )
        deployment.select(LabelSelector.of(labels=_STATIC.pods.selector))
        container = deployment.add_container(
            name="static",
            image=f"{_STATIC_IMAGE}:{PLACEHOLDER_TAG}",
            image_pull_policy=ImagePullPolicy.ALWAYS,
            # The nginx entrypoint renders haku/console/default.conf.template with these: the
            # frame-src origins, and the API Service nginx proxies backend paths to (a name that
            # resolves before the API has Ready endpoints, so the shell rolls independently).
            env_variables={
                "HAKU_CONSOLE_HAKU_UI_URL": EnvValue.from_value("https://haku-ui.allegedly.works"),
                "HAKU_CONSOLE_AUTH_ORIGIN": EnvValue.from_value(_AUTHENTIK),
                "HAKU_CONSOLE_API_UPSTREAM": EnvValue.from_value(f"{_API.host}:{_API.port.number}"),
            },
            ports=[ContainerPort(name=_STATIC.port.name, number=_STATIC.pod_port, protocol=Protocol.TCP)],
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(10), limit=Cpu.millis(100)),
                memory=MemoryResources(request=Size.mebibytes(32), limit=Size.mebibytes(128)),
            ),
            # nginx's own SPA response, not /healthz, which is proxied to the API.
            liveness=http_probe("/", port=_STATIC.pod_port, initial_delay_seconds=10, period_seconds=30),
            readiness=http_probe("/", port=_STATIC.pod_port, initial_delay_seconds=5, period_seconds=10),
            # Writable: its root filesystem writes are unaudited.
            security_context=ContainerSecurityContextProps(read_only_root_filesystem=False),
        )
        # The entrypoint writes the envsubst-rendered config here before nginx starts.
        container.mount("/etc/nginx/conf.d", Volume.from_empty_dir(self, "nginx-conf-volume", "nginx-conf"))
        pod_policy.harden(deployment)
        Service(
            self,
            "static-service",
            metadata=ApiObjectMetadata(name=_STATIC.name, namespace=NAMESPACE, labels=_STATIC.labels),
            selector=Pods.select(self, "static-pods", labels=_STATIC.pods.selector),
            ports=[
                ServicePort(
                    name=_STATIC.port.name,
                    port=_STATIC.port.number,
                    target_port=_STATIC.pod_port,
                    protocol=Protocol.TCP,
                )
            ],
        )

    def _add_http_route(self) -> None:
        # Gateway -> static shell -> API, with no forward-auth outpost: the console owns its
        # auth. The timeout clears the `sandbox` server's exec budget (max_exec_timeout_seconds
        # plus the 15s the console waits beyond it) and the MCP/OAuth streams.
        https_route(
            self,
            "httproute",
            metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
            hostnames=[HOSTNAME],
            backend=_STATIC,
            timeout="360s",
        )

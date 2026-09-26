"""The Haku Console API and its public shell: the API Deployment (reviewed FastAPI code) with
its ServiceAccount, RBAC, config ConfigMap, Service and ServiceMonitor; the separate
`haku-console-static` nginx Deployment/Service the public HTTPRoute reaches, which proxies
backend paths to the API; and the Job granting the indexer role its object privileges.

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

from cdk8s import ApiObject, ApiObjectMetadata, Duration, JsonPatch, Size
from cdk8s_plus_34 import (
    ApiResource,
    ClusterRole,
    ClusterRoleBinding,
    ConfigMap,
    ContainerPort,
    ContainerResources,
    Cpu,
    CpuResources,
    Deployment,
    DeploymentStrategy,
    EnvValue,
    Group,
    ImagePullPolicy,
    ISecret,
    Job,
    LabelSelector,
    MemoryResources,
    PercentOrAbsolute,
    Pods,
    PodSecurityContextProps,
    Probe,
    Protocol,
    RestartPolicy,
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

from cluster.cdk8s.agentplane import container_security, node_scheduling
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.forgejo_images import forgejo_images_creds_secret_ref
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.haku import console_config, database
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.pod_spec_patches import runtime_default_seccomp_patch
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor
from haku.console.config import CONFIG_FILE_ENV
from haku.console.mcp_config import ConsoleConfigFile
from haku.console.settings import Settings
from util.settings_contract import checked_value, env_name, settings_file

NAMESPACE = "haku-console"
NAME = "haku-console"
HOSTNAME = "haku.allegedly.works"
PUBLIC_BASE_URL = f"https://{HOSTNAME}"
IMAGE = "git.allegedly.works/ducktape-ci/haku-console"
_STATIC_IMAGE = "git.allegedly.works/ducktape-ci/haku-console-static"
PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
API_PORT = 8080
_METRICS_PORT = 9090
STATIC_NAME = "haku-console-static"
_STATIC_PORT = 8081
_STATIC_SERVICE_PORT = 8080
LABELS = {"app.kubernetes.io/name": NAME}
_STATIC_LABELS = {"app.kubernetes.io/name": STATIC_NAME}
_CONFIG_MAP_NAME = "haku-console-config"
_CONFIG_DIR = "/etc/haku-console/config"
# Hand-written siblings carrying Flux image-automation markers: the static image's tag,
# projected as a file so /api/deployment reports the frontend revision without a
# frontend-only release rolling the API, and the API image's own tag as an env var.
STATIC_METADATA_CONFIG_MAP = "haku-console-static-metadata"
IMAGE_METADATA_CONFIG_MAP = "haku-console-image-metadata"
_IMAGE_TAG_KEY = "image-tag"
_STATIC_METADATA_DIR = "/etc/haku-console/static-metadata"
# kustomize builds it from indexer-role.sql; a changed script re-hashes the name, and the
# Job's `force` annotation recreates it.
INDEXER_SQL_CONFIG_MAP = "haku-console-db-indexer-sql"
_INDEXER_SQL_DIR = "/sql"
_INDEXER_PROVISIONER_NAME = "haku-console-db-indexer-provisioner"
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
    secret = Secret.from_secret_name(scope, "db-app-secret", database.APP_SECRET)
    return {
        **{name: EnvValue.from_secret_value(SecretValue(secret=secret, key=key)) for name, key in _DB_ENV.items()},
        env_name(Settings, "database_url"): EnvValue.from_value(f"postgresql+asyncpg://{_DB_AUTHORITY}"),
    }


class Console(Construct):
    """The API and static-shell Deployments with everything they need, plus the indexer
    provisioner Job."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        self._secrets: dict[str, ISecret] = {}
        # Settings-file leaves the Deployment supplies from Secrets, so the file validates as
        # the whole model with those filled in (settings_file's `supplied`).
        self._supplied: list[tuple[str, ...]] = []

        # Narrow runtime identity: SubjectAccessReview for the Kubernetes-grant flow, plus
        # claim/exec RBAC on the haku-sandbox pool (haku/workspaces.py). No Secret, log, exec, or SandboxTemplate access
        # outside that pool.
        service_account = ServiceAccount(
            self, "serviceaccount", metadata=metadata(NAME, NAMESPACE), automount_token=True
        )
        self._add_rbac(service_account)
        env = self._container_env()
        config_map = self._add_config()
        self._add_deployment(service_account, env, config_map)
        self._add_service()
        self._add_service_monitor()
        self._add_static()
        self._add_http_route()
        self._add_indexer_provisioner()

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
            metadata=metadata(
                diagnostics,
                NAMESPACE,
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
            metadata=metadata(
                diagnostics,
                NAMESPACE,
                annotations={"description": "Binds Haku and public-coder to narrow Console metadata diagnostics."},
            ),
            role=Role.from_role_name(self, "diagnostics-role-ref", diagnostics),
        ).add_subjects(
            Group.from_name(self, "group-haku", "oidc-ksbx-groups:haku"),
            Group.from_name(self, "group-profile-haku", "haku:access-profile:haku"),
            ServiceAccount.from_service_account_name(self, "sa-haku", "haku", namespace_name="haku-sandbox"),
            Group.from_name(self, "group-profile-public-coder", console_config.PUBLIC_CODER_GROUP),
        )
        # Consumer-owned referent identity for source-approved external credentials.
        ServiceAccount(
            self, "external-creds-reader", metadata=metadata("external-creds-reader", NAMESPACE), automount_token=False
        )

    def _secret(self, name: str) -> ISecret:
        if name not in self._secrets:
            self._secrets[name] = Secret.from_secret_name(self, f"secret-{name}", name)
        return self._secrets[name]

    def _from_secret(self, secret: str, key: str, *path: str, optional: bool = False) -> tuple[str, EnvValue]:
        """The env var for the settings leaf at `path`, read from `secret`'s `key`."""
        self._supplied.append(path)
        return env_name(Settings, *path), EnvValue.from_secret_value(
            SecretValue(secret=self._secret(secret), key=key), optional=optional
        )

    def _container_env(self) -> dict[str, EnvValue]:
        oidc = "haku-console-oidc"
        image_metadata = ConfigMap.from_config_map_name(self, "image-metadata-ref", IMAGE_METADATA_CONFIG_MAP)
        return dict(
            [
                (env_name(Settings, "image_tag"), EnvValue.from_config_map(image_metadata, _IMAGE_TAG_KEY)),
                (
                    env_name(Settings, "aiquota_url"),
                    EnvValue.from_value("http://aiquota-api.cli-proxy-api.svc.cluster.local:8080"),
                ),
                self._from_secret("aiquota-api-bearer-haku-console", "bearer-token", "aiquota_bearer_token"),
                # Capability tier: the launch-routine action. The console builds both the fire
                # URL and the claude.ai/code deep-link from this routine (trigger) id; the bearer
                # lives only in this namespace, so Haku cannot read it.
                (
                    env_name(Settings, "launch_routine", "routine_id"),
                    EnvValue.from_value("trig_0158pCMU1XBhoALBWikwSyK4"),
                ),
                self._from_secret("haku-routine-launch-token", "token", "launch_routine", "token"),
                # Web Push: the VAPID key the console signs approval notifications with. Its
                # public half is derived at startup and handed to each browser at subscribe
                # time, so rotating it makes every enrolled device re-subscribe.
                self._from_secret("haku-console-web-push-vapid", "private-key-pem", "web_push", "private_key_pem"),
                (env_name(Settings, "web_push", "subject"), EnvValue.from_value("mailto:agentydragon@gmail.com")),
                # The Authentik-gated origin of Haku's own UI, framed as a sandboxed cross-origin
                # iframe (haku/console/docs/containment.md).
                (env_name(Settings, "haku_ui_url"), EnvValue.from_value("https://haku-ui.allegedly.works")),
                (env_name(Settings, "auth_origin"), EnvValue.from_value(_AUTHENTIK)),
                (env_name(Settings, "public_base_url"), EnvValue.from_value(PUBLIC_BASE_URL)),
                (CONFIG_FILE_ENV, EnvValue.from_value(f"{_CONFIG_DIR}/config.yaml")),
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
                self._from_secret("haku-console-agent-api", "token", "static_agents", "haku", "token"),
                # public-coder-agent's bearer reaches only its iron-proxy; the OpenClaw
                # container sees a non-secret placeholder.
                self._from_secret("haku-console-public-coder-agent", "token", "static_agents", "public_coder", "token"),
                # The Operator each static Agent acts as: the controller-fed Authentik user id,
                # resolved through the identity trust domain to a canonical Operator UUID and never
                # live request authority.
                self._from_secret(oidc, "operator_subject", "static_agents", "haku", "operator_subject"),
                self._from_secret(oidc, "operator_subject", "static_agents", "public_coder", "operator_subject"),
                # Agent-facing MCP OAuth: an Authentik-backed OIDCProxy (DCR + PKCE) on /mcp,
                # composed with the static bearers via MultiAuth. Provider and client secret are
                # minted by tf/gitops/agent-machine-access (application slug haku-console-mcp);
                # the public MCP URL is derived from public_base_url in-app.
                (
                    env_name(Settings, "mcp_oauth", "oidc_issuer"),
                    EnvValue.from_value(f"{_AUTHENTIK}/application/o/haku-console-mcp/"),
                ),
                self._from_secret(oidc, "mcp_client_id", "mcp_oauth", "oidc_client_id"),
                self._from_secret(oidc, "mcp_client_secret", "mcp_oauth", "oidc_client_secret"),
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
                self._from_secret(oidc, "operator_client_id", "operator_oidc", "client_id"),
                self._from_secret(oidc, "operator_client_secret", "operator_oidc", "client_secret"),
                self._from_secret(oidc, "operator_session_secret", "operator_oidc", "session_secret"),
                # The Authentik user-id namespace both providers above share (sub_mode=user_id).
                (
                    env_name(Settings, "operator_identity", "trust_domain"),
                    EnvValue.from_value("auth.allegedly.works/authentik-user-id/v1"),
                ),
            ]
        )

    def _add_config(self) -> ConfigMap:
        supplied = [path for path in self._supplied if path[0] in ConsoleConfigFile.model_fields]
        content = settings_file(ConsoleConfigFile, console_config.config(), supplied=supplied)
        return ConfigMap(
            self, "config", metadata=metadata(_CONFIG_MAP_NAME, NAMESPACE), data={"config.yaml": yaml_config(content)}
        )

    def _add_deployment(self, service_account: ServiceAccount, env: dict[str, EnvValue], config_map: ConfigMap) -> None:
        deployment = Deployment(
            self,
            "deployment",
            metadata=metadata(
                NAME,
                NAMESPACE,
                labels=LABELS,
                annotations={
                    # Only the API's own mounted ConfigMap and credentials, never reloader's
                    # blanket auto mode: the projected static-image metadata changes on every
                    # frontend release and must not restart API/MCP/background work.
                    "configmap.reloader.stakater.com/reload": _CONFIG_MAP_NAME,
                    "secret.reloader.stakater.com/reload": ",".join(sorted(self._secrets)),
                },
            ),
            pod_metadata=ApiObjectMetadata(labels=LABELS),
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
        deployment.select(LabelSelector.of(labels=LABELS))
        container = deployment.add_container(
            name="server",
            image=f"{IMAGE}:{PLACEHOLDER_TAG}",
            image_pull_policy=ImagePullPolicy.ALWAYS,
            ports=[ContainerPort(name="api", number=API_PORT)],
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
            startup=Probe.from_tcp_socket(port=API_PORT, failure_threshold=60, period_seconds=Duration.seconds(5)),
            # httpGet /healthz, not tcpSocket: the port stays open after FastMCP's
            # StreamableHTTPSessionManager task group wedges (haku/console/identity/
            # fastmcp_adapter.py, `mcp_session_manager_liveness`), so only /healthz notices a
            # replica stuck 500ing every /mcp request.
            liveness=http_probe("/healthz", port=API_PORT, initial_delay_seconds=10, period_seconds=30),
            readiness=http_probe("/healthz", port=API_PORT, initial_delay_seconds=5, period_seconds=10),
            # The aspect py_binary launcher materializes its venv on the rootfs at startup.
            security_context=container_security.WRITABLE_ROOT,
        )
        container.mount("/tmp", Volume.from_empty_dir(self, "tmp-volume", "tmp"))
        container.mount(_CONFIG_DIR, Volume.from_config_map(self, "config-volume", config_map), read_only=True)
        static_metadata = ConfigMap.from_config_map_name(self, "static-metadata-ref", STATIC_METADATA_CONFIG_MAP)
        container.mount(
            _STATIC_METADATA_DIR,
            Volume.from_config_map(self, "static-metadata-volume", static_metadata),
            read_only=True,
        )
        # Same zone as this console's Postgres and the public ingress: unpinned, a replica
        # landed 166ms from the database, which every read pays several round trips of.
        node_scheduling.attract_to_zone(deployment)
        ApiObject.of(deployment).add_json_patch(runtime_default_seccomp_patch())

    def _add_service(self) -> None:
        Service(
            self,
            "service",
            metadata=metadata(NAME, NAMESPACE, labels=LABELS),
            selector=Pods.select(self, "pods", labels=LABELS),
            ports=[
                # The public route targets the static shell; this port serves nginx's
                # in-cluster proxy and any trusted in-cluster API consumer.
                ServicePort(name="api", port=API_PORT, target_port=API_PORT, protocol=Protocol.TCP),
                # The ServiceMonitor's target: nginx deliberately does not proxy /metrics, so
                # this names the API container without making metrics public.
                ServicePort(name="metrics", port=_METRICS_PORT, target_port=API_PORT, protocol=Protocol.TCP),
            ],
        )

    def _add_service_monitor(self) -> None:
        ServiceMonitor(
            self,
            "servicemonitor",
            metadata=metadata(NAME, NAMESPACE),
            selector=LABELS,
            endpoints=[Endpoint.plain(port="metrics")],
        )

    def _add_static(self) -> None:
        """The public, unprivileged shell: no Kubernetes, database, OAuth, or MCP credential."""
        deployment = Deployment(
            self,
            "static-deployment",
            metadata=metadata(STATIC_NAME, NAMESPACE, labels=_STATIC_LABELS),
            pod_metadata=ApiObjectMetadata(labels=_STATIC_LABELS),
            select=False,
            replicas=2,
            strategy=DeploymentStrategy.rolling_update(
                max_surge=PercentOrAbsolute.absolute(1), max_unavailable=PercentOrAbsolute.absolute(0)
            ),
            automount_service_account_token=False,
            docker_registry_auth=forgejo_images_creds_secret_ref(self, "static-forgejo-images-creds-ref"),
            security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000, fs_group=1000),
        )
        deployment.select(LabelSelector.of(labels=_STATIC_LABELS))
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
                "HAKU_CONSOLE_API_UPSTREAM": EnvValue.from_value(f"{NAME}.{NAMESPACE}.svc.cluster.local:{API_PORT}"),
            },
            ports=[ContainerPort(name="http", number=_STATIC_PORT)],
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(10), limit=Cpu.millis(100)),
                memory=MemoryResources(request=Size.mebibytes(32), limit=Size.mebibytes(128)),
            ),
            # nginx's own SPA response, not /healthz, which is proxied to the API.
            liveness=http_probe("/", port=_STATIC_PORT, initial_delay_seconds=10, period_seconds=30),
            readiness=http_probe("/", port=_STATIC_PORT, initial_delay_seconds=5, period_seconds=10),
            security_context=container_security.WRITABLE_ROOT,
        )
        # The entrypoint writes the envsubst-rendered config here before nginx starts.
        container.mount("/etc/nginx/conf.d", Volume.from_empty_dir(self, "nginx-conf-volume", "nginx-conf"))
        ApiObject.of(deployment).add_json_patch(runtime_default_seccomp_patch())
        Service(
            self,
            "static-service",
            metadata=metadata(STATIC_NAME, NAMESPACE, labels=_STATIC_LABELS),
            selector=Pods.select(self, "static-pods", labels=_STATIC_LABELS),
            ports=[
                ServicePort(name="http", port=_STATIC_SERVICE_PORT, target_port=_STATIC_PORT, protocol=Protocol.TCP)
            ],
        )

    def _add_http_route(self) -> None:
        # Gateway -> static shell -> API, with no forward-auth outpost: the console owns its
        # auth. The timeout clears the `sandbox` server's exec budget (max_exec_timeout_seconds
        # plus the 15s the console waits beyond it) and the MCP/OAuth streams.
        https_route(
            self,
            "httproute",
            metadata=metadata(NAME, NAMESPACE),
            hostname=HOSTNAME,
            backend=STATIC_NAME,
            port=_STATIC_SERVICE_PORT,
            timeout="360s",
        )

    def _add_indexer_provisioner(self) -> None:
        """Applies indexer-role.sql's object GRANTs for `haku_indexer`. The role itself is
        CNPG-managed (database.py); the grants need the recall_index schema, which the
        migration Job creates in this same Kustomization with nothing sequencing the two --
        so this retries until that schema exists. Each attempt keeps its own Pod, so a real
        SQL error is still readable. No TTL: the TTL controller deleting a finished Job would
        make Flux recreate and re-run it on schedule."""
        job = Job(
            self,
            "indexer-provisioner",
            metadata=metadata(
                _INDEXER_PROVISIONER_NAME,
                NAMESPACE,
                annotations={
                    "description": (
                        "Applies the object GRANTs for haku_indexer (indexer-role.sql). The role itself is "
                        "managed declaratively by CNPG (haku-console-db managed.roles); this runs in the app "
                        "layer because the recall_index schema exists only after the migration Job."
                    ),
                    "kustomize.toolkit.fluxcd.io/force": "enabled",
                },
            ),
            pod_metadata=ApiObjectMetadata(labels={"app.kubernetes.io/name": _INDEXER_PROVISIONER_NAME}),
            select=False,
            backoff_limit=10,
            active_deadline=Duration.minutes(20),
            restart_policy=RestartPolicy.NEVER,
            automount_service_account_token=False,
        )
        app_secret = Secret.from_secret_name(self, "indexer-db-app-secret", database.APP_SECRET)
        container = job.add_container(
            name="psql",
            image="ghcr.io/cloudnative-pg/postgresql:18.6-system-trixie",
            image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
            command=["psql"],
            args=["--set=ON_ERROR_STOP=1", "-f", f"{_INDEXER_SQL_DIR}/indexer-role.sql"],
            # As the database owner: object-level GRANTs need owner privileges, not superuser.
            env_variables={
                "PGUSER": EnvValue.from_secret_value(SecretValue(secret=app_secret, key="username")),
                "PGPASSWORD": EnvValue.from_secret_value(SecretValue(secret=app_secret, key="password")),
                "PGHOST": EnvValue.from_value(database.RW_HOST),
                "PGDATABASE": EnvValue.from_value(database.DATABASE),
            },
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(10)),
                memory=MemoryResources(request=Size.mebibytes(32), limit=Size.mebibytes(128)),
            ),
            security_context=container_security.WRITABLE_ROOT,
        )
        sql = ConfigMap.from_config_map_name(self, "indexer-sql-ref", INDEXER_SQL_CONFIG_MAP)
        container.mount(_INDEXER_SQL_DIR, Volume.from_config_map(self, "indexer-sql-volume", sql), read_only=True)
        node_scheduling.attract_to_zone(job)
        ApiObject.of(job).add_json_patch(runtime_default_seccomp_patch())
        ApiObject.of(job).add_json_patch(
            JsonPatch.add("/spec/template/spec/containers/0/terminationMessagePolicy", "FallbackToLogsOnError")
        )

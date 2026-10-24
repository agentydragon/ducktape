"""The integration app: its Deployment (+ Alembic migrate initContainer), RBAC,
HTTPRoute, NetworkPolicy, and the agent and app ServiceAccounts.
"""

from __future__ import annotations

import json
from typing import cast
from urllib.parse import urlsplit

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ApiResource,
    ContainerResources,
    Cpu,
    CpuResources,
    EnvValue,
    IApiResource,
    ImagePullPolicy,
    MemoryResources,
    Role,
    RoleBinding,
    RolePolicyRule,
    ServiceAccount,
)
from cilium_crds.io.cilium import CiliumNetworkPolicySpecEgress
from constructs import Construct

from agentplane.app.oidc import OIDCSettings
from agentplane.app.settings import Settings
from agentplane.crd_group import GROUP
from cluster.cdk8s import cilium, node_scheduling
from cluster.cdk8s.agentplane import (
    actions,
    database,
    egress,
    history_service,
    notifications,
    runner_gateway,
    session_dispatcher,
    telemetry,
)
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.migrate_container import migrate_init_container
from cluster.cdk8s.agentplane.service_workload import ServiceWorkload, add_service_workload
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.forgejo_registry.pull_credentials import forgejo_images_creds_external_secret
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, IngressRule, NetworkPolicy
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac
from util.settings_contract import checked_value, cli_args, env_name

_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
NAME = "app"
_APP_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-app"
_MIGRATE_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-app-migrate"
_LABELS = {"app.kubernetes.io/name": NAME}


_DEFAULT_MEMORY_LIMIT = Size.mebibytes(512)
_DEFAULT_MIGRATE_MEMORY_LIMIT = Size.mebibytes(256)


def service(namespace: str) -> ServiceRef:
    """The app's Service in one environment's namespace."""
    return ServiceRef(
        name=NAME, port=Port(name="http", number=8080), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


def oidc_secret(namespace: str) -> SecretRef:
    """The OIDC client Secret the app reads."""
    return SecretRef(namespace=namespace, name="agentplane-oidc")


class App(Construct):
    """ServiceAccounts, RBAC, the config ConfigMap, Deployment (+ migrate initContainer), Service,
    and optional PodDisruptionBudget. The fleet route, network policy, and credential
    resources are added by `add_platform_resources`.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        electric: ServiceRef | None,
        database_url: EnvValue,
        placement: node_scheduling.Placement | None = node_scheduling.HIL_OVH,
        registry_secret_name: str | None = "forgejo-images-creds",
        runner_control_target: str | None = None,
        memory_limit: Size | None = _DEFAULT_MEMORY_LIMIT,
        migrate_pull_policy: ImagePullPolicy = ImagePullPolicy.ALWAYS,
        migrate_memory_limit: Size | None = _DEFAULT_MIGRATE_MEMORY_LIMIT,
    ) -> None:
        super().__init__(scope, id)
        self.env = env
        self.electric = electric
        self.database_url = database_url
        self.placement = placement
        self.registry_secret_name = registry_secret_name
        self.runner_control_target = runner_control_target or runner_gateway.control_target(env.namespace)
        self.memory_limit = memory_limit
        self.migrate_pull_policy = migrate_pull_policy
        self.migrate_memory_limit = migrate_memory_limit
        self.service = service(env.namespace)
        self.oidc = oidc_secret(env.namespace)

        self._add_agent_service_account()
        config = SettingsFile(
            self,
            "config",
            metadata=ApiObjectMetadata(name="app-config", namespace=env.namespace),
            model=Settings,
            # The service-specific Connection Service endpoint comes from the environment below,
            # keeping one authoritative value for the Go runtime.
            content=env.app_config.model_dump(mode="json", exclude_unset=True, exclude={"connection_service_url"}),
            path="/etc/agentplane/config.yaml",
        )
        workload = self._add_workload(config)
        self._add_rbac(workload.account)

    def add_platform_resources(self) -> None:
        """Add the public route and Cilium policy used by fleet environments."""
        forgejo_images_creds_external_secret(self, "forgejo-images-creds", namespace=self.env.namespace)
        self._add_http_route()
        self._add_network_policy()

    def _add_agent_service_account(self) -> None:
        # The identity an agent presents to the app's own API; no Pod runs as it, so no
        # mounted token.
        ServiceAccount(
            self,
            "serviceaccount-agent",
            metadata=ApiObjectMetadata(name="agentplane-agent", namespace=self.env.namespace),
            automount_token=False,
        )

    def _add_rbac(self, app_service_account: ServiceAccount) -> None:
        namespace = self.env.namespace
        token_reviewer_cluster_rbac(
            self,
            "token-reviewer",
            name=f"{namespace}-app-token-reviewer",
            service_account_name=NAME,
            namespace=namespace,
        )
        role = Role(
            self,
            "role",
            metadata=ApiObjectMetadata(name=NAME.removeprefix("agentplane-"), namespace=namespace),
            rules=[
                RolePolicyRule(resources=[custom_resource(GROUP, "sandboxes")], verbs=["get", "list", "watch"]),
                # The app lists names to present in the operator's create form.
                RolePolicyRule(resources=[custom_resource(GROUP, "sandboxtemplates")], verbs=["list"]),
                RolePolicyRule(resources=[cast(IApiResource, ApiResource.PODS)], verbs=["get", "list", "watch"]),
                # The operator's console stream attaches to the Sandbox VM's serial console.
                RolePolicyRule(
                    resources=[custom_resource("subresources.kubevirt.io", "virtualmachineinstances/console")],
                    verbs=["get"],
                ),
                RolePolicyRule(
                    resources=[
                        custom_resource(GROUP, resource)
                        for resource in (
                            "egresscredentials",
                            # The create form lists the sets; the live views watch both.
                            "policysets",
                            "policybindings",
                        )
                    ],
                    verbs=["get", "list", "watch"],
                ),
                # The operator's grants from the sandbox page, created and revoked by the app itself.
                RolePolicyRule(resources=[custom_resource(GROUP, "policybindings")], verbs=["create", "delete"]),
                # Sandboxes the operator creates, edits and deletes from the app.
                RolePolicyRule(resources=[custom_resource(GROUP, "sandboxes")], verbs=["create", "patch", "delete"]),
            ],
        )
        RoleBinding(
            self,
            "rolebinding",
            metadata=ApiObjectMetadata(name=NAME.removeprefix("agentplane-"), namespace=namespace),
            role=role,
        ).add_subjects(app_service_account)

    def _container_env(self) -> dict[str, EnvValue]:
        namespace = self.env.namespace
        token_subjects = json.dumps([f"system:serviceaccount:{namespace}:agentplane-agent"])
        return {
            env_name(Settings, "database_url"): self.database_url,
            **(
                {env_name(Settings, "electric_url"): EnvValue.from_value(self.electric.url)}
                if self.electric is not None
                else {}
            ),
            env_name(OIDCSettings, "issuer"): EnvValue.from_value(self.env.app.oidc_issuer),
            env_name(OIDCSettings, "public_base_url"): EnvValue.from_value(self.env.app.origin),
            env_name(OIDCSettings, "client_id"): self.oidc.key("client-id").env_value(self, "oidc-client-id-ref"),
            env_name(OIDCSettings, "client_secret"): self.oidc.key("client-secret").env_value(
                self, "oidc-client-secret-ref"
            ),
            env_name(OIDCSettings, "session_secret"): SecretRef(
                namespace=namespace, name=self.env.app.oidc_session_secret_name
            )
            .key("session-secret")
            .env_value(self, "oidc-session-secret-ref"),
            env_name(Settings, "token_subjects"): EnvValue.from_value(token_subjects),
            env_name(Settings, "connection_service_url"): EnvValue.from_value(
                str(checked_value(Settings, "connection_service_url", self.env.app_config.connection_service_url))
            ),
        }

    def _add_workload(self, config: SettingsFile) -> ServiceWorkload:
        namespace = self.env.namespace
        env = self._container_env()
        control_target = EnvValue.from_value(self.runner_control_target)
        oidc_secret_reload = self.oidc.name
        if self.env.app.oidc_session_secret_name != self.oidc.name:
            oidc_secret_reload += f",{self.env.app.oidc_session_secret_name}"
        if (
            self.env.app_config.title_generation is not None
            and self.env.app_config.title_generation.token_secret_name is not None
        ):
            oidc_secret_reload += f",{self.env.app_config.title_generation.token_secret_name}"
        workload = add_service_workload(
            self,
            self.env,
            serves=[self.service],
            container="app",
            image=f"{_APP_IMAGE}:{_PLACEHOLDER_TAG}",
            args=cli_args(
                Settings,
                # TODO: a separate Sandbox namespace, holding the Sandboxes, their
                # template and the runner ServiceAccount.
                sandbox_namespace=namespace,
                runner_gateway_target=runner_gateway.target(namespace),
                runner_gateway_token_file="/var/run/secrets/agentplane-runner-gateway/token",
                history_service_target=f"{history_service.service(namespace).fqdn}:{history_service.service(namespace).pod_port}",
                session_dispatcher_target=(
                    f"{session_dispatcher.service(namespace).fqdn}:{session_dispatcher.service(namespace).port.number}"
                ),
                session_dispatcher_http_url=(
                    f"http://{session_dispatcher.http_service(namespace).fqdn}:"
                    f"{session_dispatcher.http_service(namespace).port.number}"
                ),
                notifications_url=f"http://{notifications.service(namespace).fqdn}:{notifications.service(namespace).port.number}",
                notifications_token_file="/var/run/secrets/agentplane-notifications/token",
                host="0.0.0.0",
                port=self.service.pod_port,
            ),
            env_variables={
                **env,
                env_name(Settings, "runner_gateway_control_target"): control_target,
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": EnvValue.from_value(telemetry.OTLP_HTTP_ENDPOINT + "/v1/traces"),
                "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": EnvValue.from_value("http/protobuf"),
            },
            readiness=http_probe("/readyz", port=self.service.pod_port, initial_delay_seconds=3, period_seconds=10),
            liveness=http_probe("/healthz", port=self.service.pod_port, initial_delay_seconds=20, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=self.memory_limit),
            ),
            # The Go HTTP/SSE drain is bounded by --shutdown-timeout (5s by default); the rest
            # of the grace period allows background workers and stores to close cleanly.
            termination_grace=Duration.seconds(60),
            # The app's Alembic history. The app itself creates no tables; it verifies
            # the migrated schema at startup and fails if this hasn't run.
            migrate=migrate_init_container(
                f"{_MIGRATE_IMAGE}:{_PLACEHOLDER_TAG}",
                name="migrate",
                env_variables=env,
                image_pull_policy=self.migrate_pull_policy,
                memory_limit=self.migrate_memory_limit,
            ),
            placement=self.placement,
            telemetry_enabled=True,
            registry_secret_name=self.registry_secret_name,
            annotations={
                # A re-minted client secret otherwise leaves the pod on the old
                # one, and every login 401s.
                "secret.reloader.stakater.com/reload": oidc_secret_reload,
                "configmap.reloader.stakater.com/reload": config.config_map.name,
            },
        )
        config.mount_into(workload.container, env=Settings.config_file_env)
        if (
            title_generation := self.env.app_config.title_generation
        ) is not None and title_generation.token_secret_name is not None:
            workload.mount_secret(
                "title-generation-token",
                title_generation.token_secret_name,
                "/var/run/secrets/agentplane-title-generation",
                key="token",
            )
        for volume, audience, mount_path in [
            ("runner-gateway-token", runner_gateway.TOKEN_AUDIENCE, "/var/run/secrets/agentplane-runner-gateway"),
            ("history-service-token", history_service.TOKEN_AUDIENCE, "/var/run/secrets/agentplane-history-service"),
            ("egress-admin-token", egress.ADMIN_GRPC_AUDIENCE, "/var/run/secrets/agentplane-egress-admin"),
            (
                "session-dispatcher-token",
                session_dispatcher.TOKEN_AUDIENCE,
                "/var/run/secrets/agentplane-session-dispatcher",
            ),
            ("notifications-token", notifications.TOKEN_AUDIENCE, "/var/run/secrets/agentplane-notifications"),
        ]:
            workload.project_tokens(volume, mount_path, {"token": audience})
        return workload

    def _add_http_route(self) -> None:
        namespace = self.env.namespace
        https_route(
            self,
            "httproute",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            hostnames=[self.env.app.hostname],
            backend=self.service,
            # A session stream stays attached for as long as the tab is open.
            timeout="3600s",
        )

    def _oidc_egress_rules(self) -> list[CiliumNetworkPolicySpecEgress]:
        server_name = urlsplit(self.env.app.oidc_issuer).hostname
        assert server_name is not None, f"OIDC issuer has no hostname: {self.env.app.oidc_issuer!r}"
        rules = [cilium.egress_via_gateway(server_name)]
        if self.env.app.reach_incluster_authentik:
            rules.append(EgressRule.to_endpoints(cilium.AUTHENTIK_SERVER_LABELS, 9000, server_names=[server_name]))
        return rules

    def _add_network_policy(self) -> None:
        namespace = self.env.namespace
        # The app takes browser traffic straight from the gateway and reaches DNS, the
        # API server, the OIDC provider, Runner Gateway, Session Dispatcher, History Service, the egress
        # proxy's admin port, the Action Service, and the trajectory store.
        title_egress = []
        if (title_generation := self.env.app_config.title_generation) is not None:
            endpoint = title_generation.endpoint
            hostname = endpoint.host
            assert hostname is not None
            port = endpoint.port or (443 if endpoint.scheme == "https" else 80)
            labels = hostname.split(".")
            if len(labels) == 5 and labels[2:] == ["svc", "cluster", "local"]:
                title_egress.append(EgressRule.to_endpoints(cilium.endpoint_labels(labels[1], labels[0]), port))
            elif endpoint.scheme == "https":
                title_egress.extend([cilium.dns_egress(resolves=[hostname]), EgressRule.to_fqdns(hostname, port=port)])
        NetworkPolicy(
            self,
            "networkpolicy-app",
            metadata=ApiObjectMetadata(name=NAME.removeprefix("agentplane-"), namespace=namespace),
            endpoint_selector=self.service.pods.selector,
            ingress=[IngressRule.from_gateway(self.service.pod_port)],
            egress=[
                cilium.dns_egress(),
                EgressRule.to_entities(Entity.KUBE_APISERVER),
                *self._oidc_egress_rules(),
                runner_gateway.service(namespace).egress(),
                session_dispatcher.service(namespace).egress(),
                session_dispatcher.http_service(namespace).egress(),
                history_service.service(namespace).egress(),
                notifications.service(namespace).egress(),
                egress.admin(namespace).egress(),
                # Separate BFF/operator transport boundary. The Action Service
                # still requires its own configured operator authenticator;
                # network reachability grants no review authority.
                actions.service(namespace).egress(),
                *([self.electric.egress()] if self.electric is not None else []),
                # The browser's same-origin OTLP relay is served by this App Pod and sends HTTP OTLP.
                telemetry.ALLOY_OTLP_HTTP.egress(),
                database.egress(namespace),
                *title_egress,
            ],
        )

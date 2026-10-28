"""Notification-owned persistence and workload API; only the Runner Gateway reaches runners, and
receipts are read from the History Service."""

from collections.abc import Mapping
from pathlib import Path

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ContainerResources,
    Cpu,
    CpuResources,
    EnvValue,
    ImagePullPolicy,
    MemoryResources,
    Role,
    RoleBinding,
    RolePolicyRule,
)
from constructs import Construct
from pydantic import AnyHttpUrl

from agentplane.crd_group import GROUP
from agentplane.notification_service.settings import (
    TOKEN_AUDIENCE,
    ActionsSettings,
    HistoryServiceSettings,
    NoticeDebounceSettings,
    RunnerGatewaySettings,
    SessionDispatcherSettings,
    Settings,
)
from agentplane.workload_auth.audience import WORKLOAD_TOKEN_AUDIENCE
from cluster.cdk8s import cilium, node_scheduling
from cluster.cdk8s.agentplane import database
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.migrate_container import migrate_init_container
from cluster.cdk8s.agentplane.service_workload import add_service_workload
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.probes import grpc_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac
from util.settings_contract import env_name, validate_settings_fragment

NAME = "notifications"
WORKLOAD_CREDENTIAL = "agentplane-notifications-workload"
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-notification-service"
_LABELS = {"app.kubernetes.io/name": NAME}
# The health service whose verdict is "the process answers"; the empty name is readiness. Keep equal to
# `livenessService` in agentplane/notification_service/main.go.
_LIVENESS_HEALTH_SERVICE = "livez"


_DEFAULT_MEMORY_LIMIT = Size.mebibytes(512)
_DEFAULT_MIGRATE_MEMORY_LIMIT = Size.mebibytes(256)


def service(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME, port=Port(name="http", number=8080), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


class Notifications(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        database_url: EnvValue,
        actions: ServiceRef,
        gateway: ServiceRef,
        gateway_audience: str,
        gateway_control_target: str,
        history: ServiceRef,
        history_audience: str,
        dispatcher: ServiceRef,
        dispatcher_audience: str,
        callers: list[Pods],
        placement: node_scheduling.Placement | None = node_scheduling.HIL_OVH,
        registry_secret_name: str | None = "forgejo-images-creds",
        container_env: Mapping[str, EnvValue] | None = None,
        memory_limit: Size | None = _DEFAULT_MEMORY_LIMIT,
        migrate_pull_policy: ImagePullPolicy = ImagePullPolicy.ALWAYS,
        migrate_memory_limit: Size | None = _DEFAULT_MIGRATE_MEMORY_LIMIT,
    ) -> None:
        """Any ServiceAccount in the namespace may call with its workload token, a Sandbox's through the
        egress proxy, so `callers` are the Pods the requests come from, not accounts."""
        super().__init__(scope, id)
        self.env = env
        self.callers = callers
        self.actions = actions
        self.gateway = gateway
        self.history = history
        self.dispatcher = dispatcher
        endpoint = service(env.namespace)
        token_reviewer_cluster_rbac(
            self,
            "token-reviewer",
            name=f"{env.namespace}-notifications-token-reviewer",
            service_account_name=NAME,
            namespace=env.namespace,
        )
        variables = {"AGENTPLANE_NOTIFICATIONS_DATABASE_URL": database_url, **(container_env or {})}
        github = env.notifications_github
        content = {
            "namespace": env.namespace,
            "token_audience": TOKEN_AUDIENCE,
            "operator_reader_account": "app",
            "notice_debounce": NoticeDebounceSettings(quiet_seconds=60, max_wait_seconds=120),
            "actions": ActionsSettings(
                url=AnyHttpUrl(f"http://{actions.fqdn}:{actions.port.number}"),
                token_file=Path("/var/run/secrets/notifications/actions"),
            ),
            "runner_gateway": RunnerGatewaySettings(
                control_target=gateway_control_target, token_file=Path("/var/run/secrets/notifications/gateway")
            ),
            "history_service": HistoryServiceSettings(
                target=f"{history.fqdn}:{history.pod_port}", token_file=Path("/var/run/secrets/notifications/history")
            ),
            "session_dispatcher": SessionDispatcherSettings(
                target=f"{dispatcher.fqdn}:{dispatcher.port.number}",
                token_file=Path("/var/run/secrets/notifications/dispatcher"),
            ),
            "github": {"app_id": github.app_id} if github is not None else None,
        }
        settings = SettingsFile(
            self,
            "settings",
            metadata=ApiObjectMetadata(name=f"{NAME}-settings", namespace=env.namespace),
            path="/etc/agentplane-notifications/settings.yaml",
            content=yaml_config(validate_settings_fragment(Settings, content)),
        )
        readiness = grpc_probe(port=endpoint.pod_port, initial_delay_seconds=3, period_seconds=10)
        liveness = grpc_probe(
            port=endpoint.pod_port, service=_LIVENESS_HEALTH_SERVICE, initial_delay_seconds=20, period_seconds=30
        )
        workload = add_service_workload(
            self,
            env,
            serves=[endpoint],
            container="notifications",
            image=f"{_IMAGE}:unset",
            env_variables=variables,
            readiness=readiness,
            liveness=liveness,
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=memory_limit),
            ),
            termination_grace=Duration.seconds(30),
            migrate=migrate_init_container(
                f"{_IMAGE}-migrate:unset",
                name="migrate",
                env_variables=variables,
                image_pull_policy=migrate_pull_policy,
                memory_limit=migrate_memory_limit,
            ),
            placement=placement,
            telemetry_enabled=True,
            registry_secret_name=registry_secret_name,
        )
        role_name = NAME.removeprefix("agentplane-")
        Role(
            self,
            "role",
            metadata=ApiObjectMetadata(name=role_name, namespace=env.namespace),
            rules=[
                # Resolving a destination reads its Sandbox, not the Pod.
                RolePolicyRule(resources=[custom_resource(GROUP, "sandboxes")], verbs=["get"]),
                # Authorizing a peer send watches the caller's session_inbox_access rules and reports
                # a set it cannot parse on each binding that names it, as egress and Actions do.
                RolePolicyRule(
                    resources=[custom_resource(GROUP, resource) for resource in ("policysets", "policybindings")],
                    verbs=["get", "list", "watch"],
                ),
                RolePolicyRule(resources=[custom_resource(GROUP, "policybindings/status")], verbs=["patch"]),
            ],
        )
        RoleBinding(
            self,
            "rolebinding",
            metadata=ApiObjectMetadata(name=role_name, namespace=env.namespace),
            role=Role.from_role_name(self, "role-ref", role_name),
        ).add_subjects(workload.account)
        if github is not None:
            secret = SecretRef(namespace=env.namespace, name=github.secret_name)
            for field, key in [("private_key", "private-key"), ("webhook_secret", "webhook-secret")]:
                workload.container.env.add_variable(
                    env_name(Settings, "github", field), secret.key(key).env_value(self, f"github-{key}")
                )
        settings.mount_into(workload.container, env=Settings.config_file_env)
        workload.project_tokens(
            "service-tokens",
            "/var/run/secrets/notifications",
            {
                "actions": WORKLOAD_TOKEN_AUDIENCE,
                "gateway": gateway_audience,
                "history": history_audience,
                "dispatcher": dispatcher_audience,
            },
        )

    def add_platform_resources(self) -> None:
        """Add fleet Cilium network admission."""
        env = self.env
        endpoint = service(env.namespace)
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=NAME.removeprefix("agentplane-"), namespace=env.namespace),
            endpoint_selector=_LABELS,
            ingress=[pods.admit(endpoint.pod_port) for pods in self.callers],
            egress=[
                cilium.dns_egress(resolves=["*"]),
                EgressRule.to_fqdns("api.github.com"),
                EgressRule.to_entities(Entity.KUBE_APISERVER),
                self.actions.egress(),
                self.gateway.egress(),
                self.history.egress(),
                self.dispatcher.egress(),
                database.egress(env.namespace),
            ],
        )

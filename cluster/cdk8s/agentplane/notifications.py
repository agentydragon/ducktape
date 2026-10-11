"""Notification-owned persistence and workload API; only the Runner Gateway reaches runners, and
receipts are read from the History Service."""

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import ContainerResources, Cpu, CpuResources, MemoryResources
from constructs import Construct

from agentplane.notification_service.settings import CONFIG_FILE_ENV, TOKEN_AUDIENCE, Settings
from agentplane.workload_auth.audience import WORKLOAD_TOKEN_AUDIENCE
from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane import database
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.migrate_container import migrate_init_container
from cluster.cdk8s.agentplane.service_workload import add_service_workload
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, IngressRule, NetworkPolicy
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac
from util.settings_contract import env_name

NAME = "agentplane-notifications"
WORKLOAD_CREDENTIAL = "agentplane-notifications-workload"
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-notification-service"
_LABELS = {"app.kubernetes.io/name": NAME}


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
        actions: ServiceRef,
        sandboxes: ServiceRef,
        sandboxes_audience: str,
        gateway: ServiceRef,
        gateway_audience: str,
        gateway_control_target: str,
        history: ServiceRef,
        history_audience: str,
    ) -> None:
        super().__init__(scope, id)
        endpoint = service(env.namespace)
        token_reviewer_cluster_rbac(
            self,
            "token-reviewer",
            name=f"{env.namespace}-notifications-token-reviewer",
            service_account_name=NAME,
            namespace=env.namespace,
        )
        variables = {
            "AGENTPLANE_NOTIFICATIONS_DATABASE_URL": SecretRef(namespace=env.namespace, name="postgres-notifications")
            .key("uri")
            .env_value(self, "database")
        }
        github = env.notifications_github
        supplied: list[tuple[str, ...]] = [("database_url",)]
        if github is not None:
            supplied.extend([("github", "private_key"), ("github", "webhook_secret")])
        settings = SettingsFile(
            self,
            "settings",
            metadata=ApiObjectMetadata(name=f"{NAME}-settings", namespace=env.namespace),
            model=Settings,
            path="/etc/agentplane-notifications/settings.yaml",
            content={
                "namespace": env.namespace,
                "token_audience": TOKEN_AUDIENCE,
                "operator_reader_account": "agentplane-app",
                "notice_debounce": {"quiet_seconds": 60, "max_wait_seconds": 120},
                "actions": {
                    "url": f"http://{actions.fqdn}:{actions.port.number}",
                    "token_file": "/var/run/secrets/notifications/actions",
                },
                "sandbox_service": {
                    "target": f"{sandboxes.fqdn}:{sandboxes.port.number}",
                    "gateway_target": f"{gateway.fqdn}:{gateway.port.number}",
                    "control_target": gateway_control_target,
                    "grpc_channel_options": env.app_config.sandbox_service_grpc_channel_options,
                    "token_file": "/var/run/secrets/notifications/sandboxes",
                    "gateway_token_file": "/var/run/secrets/notifications/gateway",
                },
                "history_service": {
                    "target": f"{history.fqdn}:{history.pod_port}",
                    "grpc_channel_options": env.app_config.history_service_grpc_channel_options,
                    "token_file": "/var/run/secrets/notifications/history",
                },
                "github": {"app_id": github.app_id} if github is not None else None,
            },
            supplied=supplied,
        )
        workload = add_service_workload(
            self,
            env,
            serves=[endpoint],
            container="notifications",
            image=f"{_IMAGE}:unset",
            env_variables=variables,
            ports=[endpoint.port.container_port()],
            readiness=http_probe("/readyz", port=8080, initial_delay_seconds=3, period_seconds=10),
            liveness=http_probe("/healthz", port=8080, initial_delay_seconds=20, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(512)),
            ),
            termination_grace=Duration.seconds(30),
            migrate=migrate_init_container(f"{_IMAGE}-migrate:unset", name="migrate", env_variables=variables),
        )
        if github is not None:
            secret = SecretRef(namespace=env.namespace, name=github.secret_name)
            for field, key in [("private_key", "private-key"), ("webhook_secret", "webhook-secret")]:
                workload.container.env.add_variable(
                    env_name(Settings, "github", field), secret.key(key).env_value(self, f"github-{key}")
                )
        settings.mount_into(workload.container, env=CONFIG_FILE_ENV)
        workload.project_tokens(
            "service-tokens",
            "/var/run/secrets/notifications",
            {
                "actions": WORKLOAD_TOKEN_AUDIENCE,
                "sandboxes": sandboxes_audience,
                "gateway": gateway_audience,
                "history": history_audience,
            },
        )
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace),
            endpoint_selector=_LABELS,
            ingress=[
                IngressRule.from_endpoints(cilium.endpoint_labels(env.namespace, "agentplane-egress"), ports=[8080]),
                IngressRule.from_endpoints(cilium.endpoint_labels(env.namespace, "agentplane-app"), ports=[8080]),
            ],
            egress=[
                cilium.dns_egress(resolves=["*"]),
                EgressRule.to_fqdns("api.github.com"),
                EgressRule.to_entities(Entity.KUBE_APISERVER),
                actions.egress(),
                sandboxes.egress(),
                gateway.egress(),
                history.egress(),
                database.egress(env.namespace),
            ],
        )

"""The durable command queue between senders and the Runner Gateway."""

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ContainerResources,
    Cpu,
    CpuResources,
    MemoryResources,
    Role,
    RoleBinding,
    RolePolicyRule,
    ServiceAccount,
)
from constructs import Construct

from agentplane.crd_group import GROUP
from agentplane.session_dispatcher.settings import Settings
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane import database
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.migrate_container import migrate_init_container
from cluster.cdk8s.agentplane.service_workload import add_service_workload
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.probes import grpc_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac
from util.settings_contract import env_name

NAME = "agentplane-session-dispatcher"
TOKEN_AUDIENCE = NAME
_LABELS = {"app.kubernetes.io/name": NAME}
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-session-dispatcher"
_MIGRATION_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-session-dispatcher-migrate"
_RUNNER_TOKEN_FILE = "/var/run/secrets/agentplane-runner-gateway/token"


def service(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME, port=Port(name="grpc", number=8080), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


class SessionDispatcher(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        callers: dict[ServiceAccountRef, Pods],
        runner_gateway: ServiceRef,
        runner_control_target: str,
        runner_gateway_audience: str,
    ) -> None:
        super().__init__(scope, id)
        endpoint = service(env.namespace)
        config = SettingsFile(
            self,
            "config",
            metadata=ApiObjectMetadata(name=f"{NAME}-config", namespace=env.namespace),
            model=Settings,
            content={
                "namespace": env.namespace,
                "caller_accounts": [
                    caller.model_dump()
                    for caller in sorted(callers, key=lambda caller: (caller.namespace, caller.name))
                ],
                "token_audience": TOKEN_AUDIENCE,
                "runner_control_target": runner_control_target,
                "runner_token_file": _RUNNER_TOKEN_FILE,
                "port": endpoint.port.number,
            },
            path="/etc/agentplane-session-dispatcher/config.yaml",
            supplied=[("database_url",)],
        )
        database_url = (
            SecretRef(namespace=env.namespace, name="postgres-session-dispatcher")
            .key("uri")
            .env_value(self, "database")
        )
        workload = add_service_workload(
            self,
            env,
            serves=[endpoint],
            container="session-dispatcher",
            image=f"{_IMAGE}:unset",
            env_variables={env_name(Settings, "database_url"): database_url},
            ports=[endpoint.port.container_port()],
            readiness=grpc_probe(port=endpoint.port.number, initial_delay_seconds=3, period_seconds=10),
            liveness=grpc_probe(port=endpoint.port.number, initial_delay_seconds=20, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(512)),
            ),
            termination_grace=Duration.seconds(30),
            migrate=migrate_init_container(
                f"{_MIGRATION_IMAGE}:unset",
                name="migrate",
                env_variables={env_name(Settings, "database_url"): database_url},
            ),
        )
        config.mount_into(workload.container, env=Settings.config_file_env)
        workload.project_tokens(
            "runner-gateway-token", "/var/run/secrets/agentplane-runner-gateway", {"token": runner_gateway_audience}
        )
        self._add_rbac(workload.account, env.namespace)
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace),
            endpoint_selector=endpoint.pods.selector,
            ingress=[caller.admit(endpoint.pod_port) for caller in callers.values()],
            egress=[
                cilium.dns_egress(),
                EgressRule.to_entities(Entity.KUBE_APISERVER),
                database.egress(env.namespace),
                runner_gateway.egress(),
            ],
        )

    def _add_rbac(self, service_account: ServiceAccount, namespace: str) -> None:
        token_reviewer_cluster_rbac(
            self,
            "token-reviewer",
            name=f"{namespace}-session-dispatcher-token-reviewer",
            service_account_name=NAME,
            namespace=namespace,
        )
        Role(
            self,
            "role",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            rules=[
                # Requesters wake existing Sandboxes through the shared guarded spec patch.
                RolePolicyRule(
                    resources=[custom_resource(GROUP, "sandboxes")], verbs=["get", "list", "watch", "patch"]
                ),
                # Per-Sandbox delivery ownership. Grant before the Dispatcher starts using Leases.
                RolePolicyRule(
                    resources=[custom_resource("coordination.k8s.io", "leases")],
                    verbs=["get", "list", "watch", "create", "update"],
                ),
            ],
        )
        RoleBinding(
            self,
            "rolebinding",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            role=Role.from_role_name(self, "role-ref", NAME),
        ).add_subjects(service_account)

"""The Runner Gateway: Sessions, commands and the runner control stream. Its command admission schema migrates
before Pod startup."""

from typing import cast

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ApiResource,
    ContainerResources,
    Cpu,
    CpuResources,
    EnvFieldPaths,
    EnvValue,
    IApiResource,
    MemoryResources,
    Role,
    RoleBinding,
    RolePolicyRule,
    ServiceAccount,
)
from constructs import Construct

from agentplane.crd_group import GROUP
from agentplane.runner_gateway.settings import RUNNER_CONTROL_AUDIENCE, Settings
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane import database, egress
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

NAME = "agentplane-runner-gateway"
TOKEN_AUDIENCE = "agentplane-runner-gateway"
_LABELS = {"app.kubernetes.io/name": NAME}
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-runner-gateway"

# The policy a runner behind the egress proxy dials the control stream under (the KubeVirt
# experiment's; a Sandbox's runner uses its sidecar's platform route), and the credential it substitutes.
RUNNER_CONTROL_POLICY = "runner-control"
RUNNER_CONTROL_CREDENTIAL = "agentplane-runner-control"
_RUNNER_CONTROL_CONNECT = "/agentplane.proto.runner_control.RunnerControl/Connect"


def service(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME, port=Port(name="grpc", number=8080), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


def target(namespace: str) -> str:
    return f"{service(namespace).fqdn}:{service(namespace).pod_port}"


def control_target(namespace: str) -> str:
    """Each Sandbox's control Service, `<sandbox>-control`, which names the replica holding its runner
    connection, as the `{sandbox}`/`{namespace}` template the clients' `control_target` takes."""
    return f"{{sandbox}}-control.{{namespace}}.svc:{service(namespace).pod_port}"


def runner_control(namespace: str) -> ServiceRef:
    """The listener runners dial out to (agentplane/docs/runner_control.md), through the egress proxy."""
    return ServiceRef(
        name=NAME,
        port=Port(name="runner-control", number=8082),
        pods=Pods(namespace=namespace, labels=tuple(_LABELS.items())),
    )


class RunnerGateway(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        callers: dict[ServiceAccountRef, Pods],
        sandbox_controller: tuple[ServiceAccountRef, Pods],
    ) -> None:
        """`callers` are the Session API's clients and their Pods; `sandbox_controller` is the one caller
        of `Runner.Quiesce`."""
        super().__init__(scope, id)
        self.env = env
        endpoint = service(env.namespace)
        control = runner_control(env.namespace)
        config = SettingsFile(
            self,
            "config",
            metadata=ApiObjectMetadata(name=f"{NAME}-config", namespace=env.namespace),
            model=Settings,
            content={
                "sandbox_namespace": env.namespace,
                "caller_accounts": [
                    caller.model_dump()
                    for caller in sorted(callers, key=lambda caller: (caller.namespace, caller.name))
                ],
                "token_audience": TOKEN_AUDIENCE,
                "sandbox_controller_account": sandbox_controller[0].model_dump(),
                "runner_control_audience": RUNNER_CONTROL_AUDIENCE,
                "port": endpoint.port.number,
                "runner_control_port": control.pod_port,
            },
            path="/etc/agentplane-runner-gateway/config.yaml",
            supplied=[("commands_database_url",), ("pod_ip",)],
        )
        commands_database = (
            SecretRef(namespace=env.namespace, name="postgres-sandbox-commands")
            .key("uri")
            .env_value(self, "commands-database")
        )
        workload = add_service_workload(
            self,
            env,
            serves=[endpoint, control],
            container="runner-gateway",
            image=f"{_IMAGE}:unset",
            env_variables={
                env_name(Settings, "commands_database_url"): commands_database,
                # Where the Sandbox control Services this replica writes point (runner_control.md).
                env_name(Settings, "pod_ip"): EnvValue.from_field_ref(EnvFieldPaths.POD_IP),
            },
            ports=[endpoint.port.container_port(), control.port.container_port()],
            readiness=grpc_probe(port=endpoint.port.number, initial_delay_seconds=3, period_seconds=10),
            liveness=grpc_probe(port=endpoint.port.number, initial_delay_seconds=20, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(512)),
            ),
            termination_grace=Duration.seconds(60),
            migrate=migrate_init_container(
                "git.allegedly.works/ducktape-ci/agentplane-runner-gateway-migrate:unset",
                name="migrate-commands",
                env_variables={"AGENTPLANE_RUNNER_GATEWAY_COMMANDS_DATABASE_URL": commands_database},
            ),
        )
        self._add_rbac(workload.account)
        config.mount_into(workload.container, env=Settings.config_file_env)
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace),
            endpoint_selector=endpoint.pods.selector,
            ingress=[
                *(pods.admit(endpoint.pod_port) for pods in callers.values()),
                sandbox_controller[1].admit(endpoint.pod_port),
                egress.proxy(env.namespace).pods.admit(control.pod_port),
            ],
            egress=[cilium.dns_egress(), EgressRule.to_entities(Entity.KUBE_APISERVER), database.egress(env.namespace)],
        )
        egress.add_runner_egress(
            self,
            policy=RUNNER_CONTROL_POLICY,
            credential=RUNNER_CONTROL_CREDENTIAL,
            endpoint=control,
            path=_RUNNER_CONTROL_CONNECT,
            audience=RUNNER_CONTROL_AUDIENCE,
            purpose="control stream to the Runner Gateway",
        )

    def _add_rbac(self, service_account: ServiceAccount) -> None:
        namespace = self.env.namespace
        # TokenReview proves the caller's projected workload token. Creating a review grants none of the
        # reviewed identity's authority.
        token_reviewer_cluster_rbac(
            self,
            "token-reviewer",
            name=f"{namespace}-runner-gateway-token-reviewer",
            service_account_name=NAME,
            namespace=namespace,
        )
        Role(
            self,
            "role",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            rules=[
                # Resolving a Session's Sandbox and its runner Pod, and marking a deleting Sandbox's
                # Sessions sealed.
                RolePolicyRule(resources=[cast(IApiResource, ApiResource.PODS)], verbs=["get", "list", "watch"]),
                RolePolicyRule(resources=[custom_resource(GROUP, "sandboxes")], verbs=["get", "list", "watch"]),
                RolePolicyRule(resources=[custom_resource(GROUP, "sandboxes/status")], verbs=["patch"]),
                # Runner connection records, each owned by its Sandbox: the Lease every replica
                # watches, and the EndpointSlice of the control Service its holder points at itself.
                RolePolicyRule(
                    resources=[custom_resource("coordination.k8s.io", "leases")],
                    verbs=["get", "list", "watch", "create", "update"],
                ),
                RolePolicyRule(
                    resources=[custom_resource("discovery.k8s.io", "endpointslices")], verbs=["get", "create", "update"]
                ),
            ],
        )
        RoleBinding(
            self,
            "rolebinding",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            role=Role.from_role_name(self, "role-ref", NAME),
        ).add_subjects(service_account)

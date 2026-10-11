"""The Runner Gateway: Sessions, commands and the runner control stream. Its command admission schema migrates
before Pod startup."""

from pathlib import Path
from typing import cast

from agentplane_egresscredential_crds.works.allegedly.agentplane import (
    EgressCredentialSpecTargets,
    EgressCredentialSpecTargetsMethod,
)
from agentplane_egresspolicy_crds.works.allegedly.agentplane import (
    EgressPolicySpecRules,
    EgressPolicySpecRulesCredentialRef,
    EgressPolicySpecRulesMethods,
)
from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ApiResource,
    ContainerResources,
    ContainerSecurityContextProps,
    Cpu,
    CpuResources,
    Deployment,
    EnvFieldPaths,
    EnvValue,
    IApiResource,
    ImagePullPolicy,
    MemoryResources,
    PodSecurityContextProps,
    Role,
    RoleBinding,
    RolePolicyRule,
    Service,
    ServiceAccount,
)
from constructs import Construct

from agentplane.egress.resources import placeholder_of
from agentplane.runner_gateway.instructions import combine_instructions, render_platform_instructions
from agentplane.runner_gateway.settings import CONFIG_FILE_ENV, RUNNER_CONTROL_AUDIENCE, Settings
from agentplane.sandbox_service.runner_endpoints import RunnerEndpoint
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s import cilium, node_scheduling, pod_policy
from cluster.cdk8s.agentplane import actions, database, egress, notifications
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.migrate_container import migrate_init_container
from cluster.cdk8s.agentplane.pod_disruption_budget import add_pod_disruption_budget
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.forgejo_registry.chart import forgejo_images_creds_secret_ref
from cluster.cdk8s.probes import grpc_probe
from cluster.cdk8s.providers.agentplane.egress_credential import EgressCredential, Source
from cluster.cdk8s.providers.agentplane.egress_policy import EgressPolicy
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
KUBERNETES_ADMIN_INSTRUCTIONS = (
    Path(__file__).with_name("kubernetes_admin_instructions.md").read_text(encoding="utf-8").strip()
)

# Granted by the Sandbox Service with its `runner_control` endpoint, so no launch selection can cut a
# Sandbox off from its own control plane.
RUNNER_CONTROL_POLICY = "runner-control"
RUNNER_CONTROL_CREDENTIAL = "agentplane-runner-control"
# The bearer a runner sends on the control stream; the egress proxy swaps it for the Pod's control token.
RUNNER_CONTROL_BEARER = placeholder_of(RUNNER_CONTROL_CREDENTIAL)
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


def runner_control_endpoint(namespace: str) -> RunnerEndpoint:
    """The endpoint every new Sandbox's runner dials for its control stream: through the egress proxy,
    which swaps the placeholder bearer for the Pod's token."""
    control = runner_control(namespace)
    return RunnerEndpoint(
        target=f"{control.fqdn}:{control.port.number}",
        bearer=RUNNER_CONTROL_BEARER,
        egress_policy=RUNNER_CONTROL_POLICY,
    )


class RunnerGateway(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        callers: dict[ServiceAccountRef, ServiceRef],
        sealer: ServiceAccountRef,
        sealer_pods: Pods,
    ) -> None:
        """`callers` are the Session API's clients and their Pods; `sealer` alone may seal a Sandbox."""
        super().__init__(scope, id)
        self.env = env
        endpoint = service(env.namespace)
        control = runner_control(env.namespace)
        account = ServiceAccount(
            self, "account", metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace), automount_token=True
        )
        self._add_rbac(account)
        actions_service = actions.service(env.namespace)
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
                "sealer_accounts": [sealer.model_dump()],
                "token_audience": TOKEN_AUDIENCE,
                "platform_instructions": combine_instructions(
                    render_platform_instructions(
                        egress_api_url=f"http://{egress.agent_api(env.namespace).fqdn}",
                        actions_service_url=f"http://{actions_service.fqdn}:{actions_service.port.number}",
                        notifications_service_url=f"http://{notifications.service(env.namespace).fqdn}:8080",
                    ),
                    KUBERNETES_ADMIN_INSTRUCTIONS,
                ),
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
        # Like the other database-backed services, a migration failure blocks Pod startup. The command
        # has no retry loop.
        deployment = Deployment(
            self,
            "deployment",
            metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace, labels=_LABELS),
            pod_metadata=ApiObjectMetadata(labels=_LABELS),
            replicas=env.replicas.count,
            strategy=env.replicas.strategy,
            min_ready=env.replicas.min_ready,
            termination_grace_period=Duration.seconds(60),
            service_account=account,
            automount_service_account_token=True,
            docker_registry_auth=forgejo_images_creds_secret_ref(self, "images-creds"),
            security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000, fs_group=1000),
            init_containers=[
                migrate_init_container(
                    "git.allegedly.works/ducktape-ci/agentplane-sandbox-commands-migrate:unset",
                    name="migrate-commands",
                    env_variables={"AGENTPLANE_SANDBOX_SERVICE_COMMANDS_DATABASE_URL": commands_database},
                )
            ],
        )
        container = deployment.add_container(
            name="runner-gateway",
            image=f"{_IMAGE}:unset",
            env_variables={
                env_name(Settings, "commands_database_url"): commands_database,
                # Where the Sandbox control Services this replica writes point (runner_control.md).
                env_name(Settings, "pod_ip"): EnvValue.from_field_ref(EnvFieldPaths.POD_IP),
            },
            image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
            ports=[endpoint.port.container_port(), control.port.container_port()],
            readiness=grpc_probe(port=endpoint.port.number, initial_delay_seconds=3, period_seconds=10),
            liveness=grpc_probe(port=endpoint.port.number, initial_delay_seconds=20, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(512)),
            ),
            security_context=ContainerSecurityContextProps(read_only_root_filesystem=False),
        )
        config.mount_into(container, env=CONFIG_FILE_ENV)
        pod_policy.place(deployment, node_scheduling.HIL_OVH)
        pod_policy.harden(deployment)
        Service(
            self,
            "service",
            metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace, labels=_LABELS),
            selector=deployment,
            ports=[endpoint.port.service_port(), control.port.service_port()],
        )
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace),
            endpoint_selector=endpoint.pods.selector,
            ingress=[
                *(caller.pods.admit(endpoint.pod_port) for caller in callers.values()),
                sealer_pods.admit(endpoint.pod_port),
                egress.proxy(env.namespace).pods.admit(control.pod_port),
            ],
            egress=[
                cilium.dns_egress(),
                EgressRule.to_entities(Entity.KUBE_APISERVER),
                EgressRule.to_endpoints(
                    {"k8s:io.kubernetes.pod.namespace": env.namespace, "k8s:cnpg.io/cluster": "postgres"},
                    database.POSTGRES_PORT,
                ),
            ],
        )
        self._add_runner_control_egress(control)
        if env.replicas.pdb_min_available is not None:
            add_pod_disruption_budget(
                self,
                "pdb",
                name=NAME,
                namespace=env.namespace,
                min_available=env.replicas.pdb_min_available,
                selector=_LABELS,
            )

    def _add_runner_control_egress(self, control: ServiceRef) -> None:
        """The egress path a runner dials the control listener on: the proxy swaps the placeholder for the
        Pod's own token for the control stream's audience, which no other recipient accepts."""
        namespace = self.env.namespace
        EgressCredential(
            self,
            "runner-control-credential",
            metadata=ApiObjectMetadata(name=RUNNER_CONTROL_CREDENTIAL, namespace=namespace),
            description=(
                "The calling Sandbox Pod's short-lived, Pod-bound identity minted only for its runner's "
                "control stream to the Runner Gateway. It conveys no other authority."
            ),
            source=Source.projected_workload_token(audience=RUNNER_CONTROL_AUDIENCE),
            targets=[
                EgressCredentialSpecTargets(
                    header="Authorization", method=EgressCredentialSpecTargetsMethod.SCHEME_TOKEN, scheme="Bearer"
                )
            ],
        )
        EgressPolicy(
            self,
            "runner-control-policy",
            metadata=ApiObjectMetadata(name=RUNNER_CONTROL_POLICY, namespace=namespace),
            rules=[
                EgressPolicySpecRules(
                    hosts=[control.fqdn],
                    cluster_internal=True,
                    methods=[EgressPolicySpecRulesMethods.POST],
                    paths=[_RUNNER_CONTROL_CONNECT],
                    credential_ref=EgressPolicySpecRulesCredentialRef(name=RUNNER_CONTROL_CREDENTIAL),
                )
            ],
        )
        # Additive to the proxy's own policy, which egress.py owns and which cannot name this
        # module: this one imports that one.
        NetworkPolicy(
            self,
            "runner-control-proxy-egress",
            metadata=ApiObjectMetadata(name=f"{NAME}-runner-control-proxy", namespace=namespace),
            endpoint_selector=egress.proxy(namespace).pods.selector,
            egress=[control.egress()],
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
                # Resolving a Session's Sandbox and its runner Pod.
                RolePolicyRule(
                    resources=[custom_resource("agents.x-k8s.io", "sandboxes")], verbs=["get", "list", "watch"]
                ),
                RolePolicyRule(resources=[cast(IApiResource, ApiResource.PODS)], verbs=["get", "list", "watch"]),
                # Runner connection records, each owned by its Sandbox: the Lease every replica
                # watches, and the control Service and EndpointSlice its holder points at itself.
                RolePolicyRule(
                    resources=[custom_resource("coordination.k8s.io", "leases")],
                    verbs=["get", "list", "watch", "create", "update"],
                ),
                RolePolicyRule(
                    resources=[custom_resource("", "services"), custom_resource("discovery.k8s.io", "endpointslices")],
                    verbs=["get", "create", "update"],
                ),
            ],
        )
        RoleBinding(
            self,
            "rolebinding",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            role=Role.from_role_name(self, "role-ref", NAME),
        ).add_subjects(service_account)

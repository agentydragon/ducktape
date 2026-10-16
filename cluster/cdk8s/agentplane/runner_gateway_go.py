"""Candidate unified Go Runner Gateway objects for the fenced cutover.

The environment chart still instantiates the Python Gateway and Dispatcher. Keep this
construct out of it until the Go images are published and the old Dispatcher is stopped.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import cast

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ApiResource,
    ContainerProps,
    ContainerResources,
    ContainerSecurityContextProps,
    Cpu,
    CpuResources,
    DeploymentStrategy,
    EnvFieldPaths,
    EnvValue,
    IApiResource,
    ImagePullPolicy,
    Job,
    MemoryResources,
    PodSecurityContextProps,
    RestartPolicy,
    Role,
    RoleBinding,
    RolePolicyRule,
    ServiceAccount,
    k8s,
)
from constructs import Construct

from agentplane.crd_group import GROUP
from agentplane.runner_gateway.settings import RUNNER_CONTROL_AUDIENCE
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s import cilium, node_scheduling, pod_policy
from cluster.cdk8s.agentplane import database, egress, runner_gateway, session_dispatcher
from cluster.cdk8s.agentplane.environment import Environment, ReplicaProfile
from cluster.cdk8s.agentplane.service_workload import add_service_workload
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.forgejo_registry.pull_credentials import forgejo_images_creds_secret_ref
from cluster.cdk8s.probes import grpc_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac

_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-runner-gateway-go:unset"
_RUNNER_TOKEN_FILE = "/var/run/secrets/agentplane-runner-gateway/token"
_GRPC_PORT = 8080


_DEFAULT_MEMORY_LIMIT = Size.gibibytes(1)


def dispatcher_service(namespace: str) -> ServiceRef:
    """Keep the sender's DNS and Service port; target the shared Go listener."""
    return ServiceRef(
        name=session_dispatcher.NAME,
        port=Port(name="grpc", number=_GRPC_PORT),
        pods=runner_gateway.service(namespace).pods,
        target_port=_GRPC_PORT,
    )


def runner_control_service(namespace: str, *, port: Port | None = None) -> ServiceRef:
    """The Go listener behind a caller-selected control Service port."""
    control = runner_gateway.runner_control(namespace)
    return ServiceRef(name=control.name, port=port or control.port, pods=control.pods, target_port=_GRPC_PORT)


def _account(account: ServiceAccountRef) -> str:
    return f"{account.namespace}/{account.name}"


def add_stable_services(
    scope: Construct, env: Environment, *, cluster_ip: str | None = None, control: ServiceRef | None = None
) -> None:
    """Root-owned Services retain their names while the new Go workload is gated on migration.

    Call this in the existing environment chart only during the fenced cutover, after
    its Python Gateway and Dispatcher constructs have been removed. The Services stay
    in their current Flux owner, while the Go workload belongs to a dependent one.
    """
    proxy = runner_gateway.service(env.namespace)
    control = control or runner_control_service(env.namespace)
    dispatcher = dispatcher_service(env.namespace)
    k8s.KubeService(
        scope,
        "runner-gateway-service",
        metadata=k8s.ObjectMeta(name=proxy.name, namespace=env.namespace, labels=proxy.labels),
        spec=k8s.ServiceSpec(
            cluster_ip=cluster_ip,
            selector=proxy.pods.selector,
            ports=(
                [proxy.port.k8s_service_port()]
                if control.port.number == proxy.port.number
                else [
                    proxy.port.k8s_service_port(),
                    k8s.ServicePort(
                        name=control.port.name,
                        port=control.port.number,
                        target_port=k8s.IntOrString.from_number(_GRPC_PORT),
                        protocol="TCP",
                    ),
                ]
            ),
        ),
    )
    k8s.KubeService(
        scope,
        "session-dispatcher-service",
        metadata=k8s.ObjectMeta(
            name=dispatcher.name, namespace=env.namespace, labels={"app.kubernetes.io/name": dispatcher.name}
        ),
        spec=k8s.ServiceSpec(
            cluster_ip=cluster_ip,
            selector=dispatcher.pods.selector,
            ports=[
                k8s.ServicePort(
                    name=dispatcher.port.name,
                    port=dispatcher.port.number,
                    target_port=k8s.IntOrString.from_number(dispatcher.pod_port),
                    protocol="TCP",
                )
            ],
        ),
    )


class GoRunnerGateway(Construct):
    """One workload with three authenticated services on one listener; the root retains its Services."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        database_url: EnvValue,
        dispatcher_callers: Mapping[ServiceAccountRef, Pods],
        gateway_callers: Mapping[ServiceAccountRef, Pods],
        wake_callers: frozenset[ServiceAccountRef],
        sandbox_controller: tuple[ServiceAccountRef, Pods],
        control: ServiceRef | None = None,
        migration: ContainerProps | None = None,
        registry_secret_name: str | None = "forgejo-images-creds",
        placement: node_scheduling.Placement | None = node_scheduling.HIL_OVH,
        container_env: Mapping[str, EnvValue] | None = None,
        memory_limit: Size | None = _DEFAULT_MEMORY_LIMIT,
    ) -> None:
        super().__init__(scope, id)
        if not dispatcher_callers or not gateway_callers:
            raise ValueError("Go Runner Gateway needs explicit Dispatcher and proxy caller grants")
        if not wake_callers <= dispatcher_callers.keys():
            raise ValueError("wake callers must also be Dispatcher callers")

        namespace = env.namespace
        self.env = env
        self.dispatcher_callers = dispatcher_callers
        self.sandbox_controller_pods = sandbox_controller[1]
        proxy = runner_gateway.service(namespace)
        control = control or runner_control_service(namespace)
        self.control = control
        own_account = ServiceAccountRef(namespace=namespace, name=runner_gateway.NAME)
        # A delivery worker may run on a different replica from the holder. It calls
        # the per-Sandbox control Service with this projected Gateway-audience token.
        proxy_callers = {**gateway_callers, own_account: proxy.pods}
        self.proxy_callers = proxy_callers
        args = [
            f"--sandbox-namespace={namespace}",
            f"--listen-address=0.0.0.0:{_GRPC_PORT}",
            f"--runner-token-file={_RUNNER_TOKEN_FILE}",
            f"--dispatcher-audience={session_dispatcher.TOKEN_AUDIENCE}",
            f"--gateway-audience={runner_gateway.TOKEN_AUDIENCE}",
            f"--runner-audience={RUNNER_CONTROL_AUDIENCE}",
            f"--sandbox-controller-account={_account(sandbox_controller[0])}",
            *(f"--dispatcher-caller={_account(account)}" for account in sorted(dispatcher_callers, key=_account)),
            *(f"--gateway-caller={_account(account)}" for account in sorted(proxy_callers, key=_account)),
            *(f"--wake-caller={_account(account)}" for account in sorted(wake_callers, key=_account)),
        ]
        # The first Go cutover is a fenced Recreate; restoring RollingUpdate is a
        # separate cleanup after queue and connection recovery have been observed.
        initial_replicas = ReplicaProfile(
            count=env.replicas.count,
            strategy=DeploymentStrategy.recreate(),
            min_ready=env.replicas.min_ready,
            pdb_min_available=env.replicas.pdb_min_available,
        )
        workload = add_service_workload(
            self,
            env,
            serves=[proxy, control],
            container="runner-gateway",
            image=_IMAGE,
            args=args,
            env_variables={
                "AGENTPLANE_RUNNER_GATEWAY_DISPATCHER_DATABASE_URL": database_url,
                "AGENTPLANE_RUNNER_GATEWAY_POD_IP": EnvValue.from_field_ref(EnvFieldPaths.POD_IP),
                **(container_env or {}),
            },
            ports=[proxy.port.container_port()],
            readiness=grpc_probe(port=proxy.pod_port, initial_delay_seconds=3, period_seconds=10),
            liveness=grpc_probe(port=proxy.pod_port, initial_delay_seconds=20, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(100)),
                memory=MemoryResources(request=Size.mebibytes(256), limit=memory_limit),
            ),
            termination_grace=Duration.seconds(60),
            replica_profile=initial_replicas,
            create_service=False,
            migrate=migration,
            registry_secret_name=registry_secret_name,
            placement=placement,
        )
        workload.project_tokens(
            "runner-gateway-token",
            "/var/run/secrets/agentplane-runner-gateway",
            {"token": runner_gateway.TOKEN_AUDIENCE},
        )
        self._add_rbac(workload.account, namespace)
        egress.add_runner_egress(
            self,
            policy=runner_gateway.RUNNER_CONTROL_POLICY,
            credential=runner_gateway.RUNNER_CONTROL_CREDENTIAL,
            endpoint=control,
            path="/agentplane.proto.runner_control.RunnerControl/Connect",
            audience=RUNNER_CONTROL_AUDIENCE,
            purpose="control stream to the Runner Gateway",
        )

    def add_platform_resources(self) -> None:
        """Add the fleet Cilium fences around the shared Gateway listeners."""
        namespace = self.env.namespace
        proxy = runner_gateway.service(namespace)
        dispatcher = dispatcher_service(namespace)
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=runner_gateway.NAME.removeprefix("agentplane-"), namespace=namespace),
            endpoint_selector=proxy.pods.selector,
            ingress=[
                *(pods.admit(proxy.pod_port) for pods in self.proxy_callers.values()),
                self.sandbox_controller_pods.admit(proxy.pod_port),
                *(pods.admit(dispatcher.pod_port) for pods in self.dispatcher_callers.values()),
                egress.proxy(namespace).pods.admit(self.control.pod_port),
            ],
            egress=[
                cilium.dns_egress(),
                EgressRule.to_entities(Entity.KUBE_APISERVER),
                database.egress(namespace),
                proxy.egress(),
            ],
        )
        egress.add_runner_egress_network_policy(
            self, policy=runner_gateway.RUNNER_CONTROL_POLICY, endpoint=self.control
        )

    def _add_rbac(self, service_account: ServiceAccount, namespace: str) -> None:
        role_name = runner_gateway.NAME.removeprefix("agentplane-")
        token_reviewer_cluster_rbac(
            self,
            "token-reviewer",
            name=f"{namespace}-runner-gateway-token-reviewer",
            service_account_name=runner_gateway.NAME,
            namespace=namespace,
        )
        Role(
            self,
            "role",
            metadata=ApiObjectMetadata(name=role_name, namespace=namespace),
            rules=[
                RolePolicyRule(resources=[cast(IApiResource, ApiResource.PODS)], verbs=["get", "list", "watch"]),
                RolePolicyRule(
                    resources=[custom_resource(GROUP, "sandboxes")], verbs=["get", "list", "watch", "patch"]
                ),
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
            metadata=ApiObjectMetadata(name=role_name, namespace=namespace),
            role=Role.from_role_name(self, "role-ref", role_name),
        ).add_subjects(service_account)


_MIGRATOR_RE = re.compile(
    r"^git\.allegedly\.works/ducktape-ci/agentplane-runner-gateway-queue-migrate@sha256:([0-9a-f]{64})$"
)


class QueueMigration(Construct):
    """A digest-coupled Goose Job for the existing Agentplane root Kustomization.

    The root owns PostgreSQL and its Secret, so it also owns this Job and checks
    its completion. A separate Go serving Kustomization depends on that root.
    This construct is not instantiated until the fenced cutover is reviewed.
    """

    def __init__(self, scope: Construct, id: str, env: Environment, *, image: str, database_url: EnvValue) -> None:
        super().__init__(scope, id)
        match = _MIGRATOR_RE.fullmatch(image)
        if match is None:
            raise ValueError("queue migrator image must be the published OCI image pinned by its full sha256 digest")
        digest = match.group(1)
        self.name = f"runner-gateway-queue-migrate-{digest[:12]}"
        labels = {"app.kubernetes.io/name": "runner-gateway-queue-migrate"}
        account = ServiceAccount(
            self,
            "account",
            metadata=ApiObjectMetadata(name="runner-gateway-queue-migrate", namespace=env.namespace),
            automount_token=False,
        )
        job = Job(
            self,
            "job",
            metadata=ApiObjectMetadata(name=self.name, namespace=env.namespace),
            pod_metadata=ApiObjectMetadata(labels=labels),
            select=False,
            backoff_limit=3,
            active_deadline=Duration.minutes(10),
            restart_policy=RestartPolicy.NEVER,
            service_account=account,
            automount_service_account_token=False,
            enable_service_links=False,
            docker_registry_auth=forgejo_images_creds_secret_ref(self, "images-creds"),
            security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000),
        )
        job.add_container(
            name="migrate",
            image=image,
            image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
            env_variables={"DATABASE_URL": database_url},
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(512)),
            ),
            security_context=ContainerSecurityContextProps(read_only_root_filesystem=False),
        )
        pod_policy.place(job, node_scheduling.HIL_OVH)
        pod_policy.harden(job)
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name="runner-gateway-queue-migrate", namespace=env.namespace),
            endpoint_selector=labels,
            ingress=[],
            egress=[cilium.dns_egress(), database.egress(env.namespace)],
        )

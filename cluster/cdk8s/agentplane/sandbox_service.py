"""The Sandbox Service: Sandbox lifecycle and egress grants. The Sandbox Controller does the rest."""

from typing import cast

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ApiResource,
    ContainerResources,
    Cpu,
    CpuResources,
    IApiResource,
    MemoryResources,
    Role,
    RoleBinding,
    RolePolicyRule,
    ServiceAccount,
)
from constructs import Construct

from agentplane.crd_group import GROUP
from agentplane.sandbox_service.settings import Settings
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane import egress
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.service_workload import add_service_workload
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.probes import grpc_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac

NAME = "agentplane-sandbox-service"
TOKEN_AUDIENCE = "agentplane-sandbox-service"
_LABELS = {"app.kubernetes.io/name": NAME}
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-sandbox-service"


def service(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME, port=Port(name="grpc", number=8080), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


class SandboxService(Construct):
    def __init__(self, scope: Construct, id: str, env: Environment, *, callers: dict[ServiceAccountRef, Pods]) -> None:
        super().__init__(scope, id)
        self.env = env
        endpoint = service(env.namespace)
        config = SettingsFile(
            self,
            "config",
            metadata=ApiObjectMetadata(name=f"{NAME}-config", namespace=env.namespace),
            model=Settings,
            content={
                "sandbox_namespace": env.namespace,
                "caller_accounts": [
                    account.model_dump()
                    for account in sorted(callers, key=lambda account: (account.namespace, account.name))
                ],
                "token_audience": TOKEN_AUDIENCE,
                "default_policy_sets": sorted(egress.DEFAULT_POLICIES),
                "port": endpoint.port.number,
            },
            path="/etc/agentplane-sandbox-service/config.yaml",
        )
        workload = add_service_workload(
            self,
            env,
            serves=[endpoint],
            container="sandbox-service",
            image=f"{_IMAGE}:unset",
            ports=[endpoint.port.container_port()],
            readiness=grpc_probe(port=endpoint.port.number, initial_delay_seconds=3, period_seconds=10),
            liveness=grpc_probe(port=endpoint.port.number, initial_delay_seconds=20, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(512)),
            ),
            termination_grace=Duration.seconds(60),
        )
        self._add_rbac(workload.account)
        config.mount_into(workload.container, env=Settings.config_file_env)
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace),
            endpoint_selector=endpoint.pods.selector,
            ingress=[pods.admit(endpoint.pod_port) for pods in callers.values()],
            egress=[cilium.dns_egress(), EgressRule.to_entities(Entity.KUBE_APISERVER)],
        )

    def _add_rbac(self, service_account: ServiceAccount) -> None:
        namespace = self.env.namespace
        # TokenReview proves the caller's projected workload token. Creating a
        # review grants none of the reviewed identity's authority.
        token_reviewer_cluster_rbac(
            self,
            "token-reviewer",
            name=f"{namespace}-sandbox-service-token-reviewer",
            service_account_name=NAME,
            namespace=namespace,
        )
        Role(
            self,
            "role",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            rules=[
                # ListTemplates lists them; a get-only Role 403'd the route (#7023).
                RolePolicyRule(resources=[custom_resource(GROUP, "sandboxtemplates")], verbs=["list"]),
                RolePolicyRule(
                    resources=[custom_resource(GROUP, "sandboxes")], verbs=["create", "get", "list", "patch", "delete"]
                ),
                RolePolicyRule(resources=[custom_resource(GROUP, "policysets")], verbs=["get", "list"]),
                # StreamConsole attaches a VM's serial console.
                RolePolicyRule(
                    resources=[custom_resource("subresources.kubevirt.io", "virtualmachineinstances/console")],
                    verbs=["get"],
                ),
                RolePolicyRule(resources=[cast(IApiResource, ApiResource.PODS)], verbs=["get", "list"]),
                # GrantEgress and RevokeEgress.
                RolePolicyRule(
                    resources=[
                        custom_resource(GROUP, resource) for resource in ("egresscredentials", "policybindings")
                    ],
                    verbs=["get", "list"],
                ),
                RolePolicyRule(
                    resources=[custom_resource(GROUP, resource) for resource in ("policybindings",)],
                    verbs=["create", "delete"],
                ),
            ],
        )
        RoleBinding(
            self,
            "rolebinding",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            role=Role.from_role_name(self, "role-ref", NAME),
        ).add_subjects(service_account)

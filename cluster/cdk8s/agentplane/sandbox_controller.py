"""The Sandbox Controller (agentplane/sandbox_controller): it keeps each of the environment's own
Sandboxes' state PVC and, while running, its Pod."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import ContainerResources, Cpu, CpuResources, MemoryResources, Role, RoleBinding, RolePolicyRule
from constructs import Construct
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector

from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.service_workload import add_service_workload
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "agentplane-sandbox-controller"
_LABELS = {"app.kubernetes.io/name": NAME}
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-sandbox-controller"
_GROUP = "agentplane.allegedly.works"
_HEALTH_PORT = 8081


def metrics(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME, port=Port(name="metrics", number=8080), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


class SandboxController(Construct):
    """The controller's Deployment, RBAC, network policy and metrics scrape."""

    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)
        namespace = env.namespace
        endpoint = metrics(namespace)
        workload = add_service_workload(
            self,
            env,
            serves=[endpoint],
            container="sandbox-controller",
            image=f"{_IMAGE}:unset",
            args=[f"--namespace={namespace}"],
            ports=[endpoint.port.container_port(), Port(name="health", number=_HEALTH_PORT).container_port()],
            readiness=http_probe("/readyz", port=_HEALTH_PORT, initial_delay_seconds=3),
            liveness=http_probe("/healthz", port=_HEALTH_PORT, initial_delay_seconds=10, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(10)),
                memory=MemoryResources(request=Size.mebibytes(64), limit=Size.mebibytes(256)),
            ),
            termination_grace=Duration.seconds(30),
        )
        Role(
            self,
            "role",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            rules=[
                # Update adds and removes the state-volume finalizer.
                RolePolicyRule(
                    resources=[custom_resource(_GROUP, "sandboxes")], verbs=["get", "list", "watch", "update"]
                ),
                RolePolicyRule(resources=[custom_resource(_GROUP, "sandboxes/status")], verbs=["update"]),
                # A Pod's controller owner reference sets blockOwnerDeletion, which admission allows
                # only to who may update the owner's finalizers.
                RolePolicyRule(resources=[custom_resource(_GROUP, "sandboxes/finalizers")], verbs=["update"]),
                RolePolicyRule(resources=[custom_resource(_GROUP, "sandboxtemplates")], verbs=["get", "list", "watch"]),
                RolePolicyRule(
                    resources=[custom_resource("", "pods"), custom_resource("", "persistentvolumeclaims")],
                    verbs=["get", "list", "watch", "create", "delete"],
                ),
                # Leader election.
                RolePolicyRule(
                    resources=[custom_resource("coordination.k8s.io", "leases")], verbs=["get", "create", "update"]
                ),
                RolePolicyRule(resources=[custom_resource("", "events")], verbs=["create", "patch"]),
            ],
        )
        RoleBinding(
            self,
            "role-binding",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            role=Role.from_role_name(self, "role-ref", NAME),
        ).add_subjects(workload.account)
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            endpoint_selector=endpoint.pods.selector,
            ingress=[cilium.SCRAPERS.admit(endpoint.pod_port)],
            egress=[cilium.dns_egress(), EgressRule.to_entities(Entity.KUBE_APISERVER)],
        )
        ServiceMonitor(
            self,
            "service-monitor",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            selector=ServiceMonitorSpecSelector(match_labels=endpoint.pods.selector),
            endpoints=[Endpoint.plain(port=endpoint.port.name)],
        )

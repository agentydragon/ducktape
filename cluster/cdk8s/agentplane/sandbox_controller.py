"""The Sandbox Controller (agentplane/sandbox_controller): it keeps each of the environment's own
Sandboxes' state PVC, ServiceAccount and PolicyBinding and, while running, its Pod or VM."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ContainerResources,
    Cpu,
    CpuResources,
    EnvValue,
    MemoryResources,
    Role,
    RoleBinding,
    RolePolicyRule,
)
from constructs import Construct
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector

from agentplane.sandbox_controller.settings import Settings
from cluster.cdk8s import cilium, node_scheduling
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.service_workload import add_service_workload
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from util.settings_contract import checked_value, env_name

_GATEWAY_TOKEN_DIR = "/var/run/secrets/agentplane-runner-gateway"

NAME = "sandbox-controller"
_LABELS = {"app.kubernetes.io/name": NAME}
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-sandbox-controller"
_GROUP = "agentplane.allegedly.works"
_HEALTH_PORT = 8081


_DEFAULT_MEMORY_LIMIT = Size.mebibytes(256)


def metrics(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME, port=Port(name="metrics", number=8080), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


def pods(namespace: str) -> Pods:
    return metrics(namespace).pods


class SandboxController(Construct):
    """The controller's Deployment and RBAC; fleet network policy and metrics are added separately."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        gateway: ServiceRef,
        gateway_audience: str,
        gateway_control_target: str,
        runner_certificate_issuer: str,
        placement: node_scheduling.Placement | None = node_scheduling.HIL_OVH,
        registry_secret_name: str | None = "forgejo-images-creds",
        memory_limit: Size | None = _DEFAULT_MEMORY_LIMIT,
    ) -> None:
        """The controller quiesces runners through the Runner Gateway's proxy, at each Sandbox's own
        control Service (`gateway_control_target`) under a token of `gateway_audience`."""
        super().__init__(scope, id)
        self.env = env
        self.gateway = gateway
        namespace = env.namespace
        endpoint = metrics(namespace)
        scalars = {
            field: checked_value(Settings, field, value)
            for field, value in {
                "namespace": namespace,
                "metrics_bind_address": f":{endpoint.port.number}",
                "health_probe_bind_address": f":{_HEALTH_PORT}",
                "runner_gateway_target": gateway_control_target,
                "runner_gateway_token_file": f"{_GATEWAY_TOKEN_DIR}/token",
                "runner_certificate_issuer": runner_certificate_issuer,
            }.items()
        }
        workload = add_service_workload(
            self,
            env,
            serves=[endpoint],
            container="sandbox-controller",
            image=f"{_IMAGE}:unset",
            env_variables={
                env_name(Settings, field): EnvValue.from_value(str(value)) for field, value in scalars.items()
            },
            extra_ports=[Port(name="health", number=_HEALTH_PORT).container_port()],
            readiness=http_probe("/readyz", port=_HEALTH_PORT, initial_delay_seconds=3),
            liveness=http_probe("/healthz", port=_HEALTH_PORT, initial_delay_seconds=10, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(10)),
                memory=MemoryResources(request=Size.mebibytes(64), limit=memory_limit),
            ),
            termination_grace=Duration.seconds(30),
            placement=placement,
            telemetry_enabled=True,
            registry_secret_name=registry_secret_name,
        )
        workload.project_tokens("runner-gateway-token", _GATEWAY_TOKEN_DIR, {"token": gateway_audience})
        role_name = NAME.removeprefix("agentplane-")
        Role(
            self,
            "role",
            metadata=ApiObjectMetadata(name=role_name, namespace=namespace),
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
                    resources=[custom_resource("", "pods")], verbs=["get", "list", "watch", "create", "delete"]
                ),
                # Per-incarnation cert-manager requests. cert-manager owns every generated key and
                # certificate; the controller only creates/deletes Certificate declarations and
                # reads the resulting leaf Secret before attaching it to a workload.
                RolePolicyRule(
                    resources=[custom_resource("cert-manager.io", "certificates")],
                    verbs=["get", "list", "create", "delete"],
                ),
                RolePolicyRule(resources=[custom_resource("", "secrets")], verbs=["get", "list", "delete"]),
                # Update adds and removes the Sandbox owner reference; patch backfills the durable
                # state-volume layout record on an older controller-owned claim.
                RolePolicyRule(
                    resources=[custom_resource("", "persistentvolumeclaims")],
                    verbs=["get", "list", "watch", "create", "update", "patch"],
                ),
                # A VM Sandbox's VM, kept halted while it is stopped, and the VMI it reads status from.
                RolePolicyRule(
                    resources=[custom_resource("kubevirt.io", "virtualmachines")],
                    verbs=["get", "list", "watch", "create", "update", "delete"],
                ),
                RolePolicyRule(
                    resources=[custom_resource("kubevirt.io", "virtualmachineinstances")],
                    verbs=["get", "list", "watch"],
                ),
                # Each Sandbox's own; garbage collection deletes them with the Sandbox.
                RolePolicyRule(
                    resources=[custom_resource("", "serviceaccounts")],
                    verbs=["get", "list", "watch", "create", "patch"],
                ),
                # Each Sandbox's control Service, which the Runner Gateway points at the connected runner.
                RolePolicyRule(
                    resources=[custom_resource("", "services")], verbs=["get", "list", "watch", "create", "patch"]
                ),
                # Each Sandbox's launch ConfigMap (its identity and bootstrap), kept current while it lives.
                RolePolicyRule(
                    resources=[custom_resource("", "configmaps")], verbs=["get", "list", "watch", "create", "patch"]
                ),
                # Binding a Sandbox's account to the PolicySets its spec names, in this namespace.
                RolePolicyRule(
                    resources=[custom_resource(_GROUP, "policybindings")],
                    verbs=["get", "list", "watch", "create", "patch", "delete"],
                ),
                # Leader election, and the Runner Gateway's connection Leases, which the controller watches.
                RolePolicyRule(
                    resources=[custom_resource("coordination.k8s.io", "leases")],
                    verbs=["get", "list", "watch", "create", "update"],
                ),
                # Records its own Events, and watches those about Sandbox Pods for what a starting one waits on.
                RolePolicyRule(
                    resources=[custom_resource("", "events")], verbs=["get", "list", "watch", "create", "patch"]
                ),
            ],
        )
        RoleBinding(
            self,
            "role-binding",
            metadata=ApiObjectMetadata(name=role_name, namespace=namespace),
            role=Role.from_role_name(self, "role-ref", role_name),
        ).add_subjects(workload.account)

    def add_platform_resources(self) -> None:
        """Add fleet network policy and metrics scrape."""
        namespace = self.env.namespace
        endpoint = metrics(namespace)
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=NAME.removeprefix("agentplane-"), namespace=namespace),
            endpoint_selector=endpoint.pods.selector,
            ingress=[cilium.SCRAPERS.admit(endpoint.pod_port)],
            egress=[cilium.dns_egress(), EgressRule.to_entities(Entity.KUBE_APISERVER), self.gateway.egress()],
        )
        ServiceMonitor(
            self,
            "service-monitor",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            selector=ServiceMonitorSpecSelector(match_labels=endpoint.pods.selector),
            endpoints=[Endpoint.plain(port=endpoint.port.name)],
        )

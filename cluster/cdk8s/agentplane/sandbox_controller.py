"""The Sandbox Controller (agentplane/sandbox_controller) reconciles state and optional workspace
PVCs, WorkloadCredentials and Pod or VM incarnations for Sandboxes using their referenced Identities."""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import ContainerResources, Cpu, CpuResources, MemoryResources, Role, RoleBinding, RolePolicyRule
from constructs import Construct
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector

from agentplane.net.peertls.config import MTLS, SANDBOX_CONTROLLER_URI, SESSION_DISPATCHER_URI
from agentplane.sandbox_controller.settings import Settings
from cluster.cdk8s import cilium, node_scheduling
from cluster.cdk8s.agentplane import environment_sandbox, workload_tls
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.service_workload import add_service_workload
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile

_GATEWAY_TOKEN_DIR = "/var/run/secrets/agentplane-runner-gateway"
_SETTINGS_PATH = "/etc/agentplane-sandbox-controller/config.yaml"

NAME = "sandbox-controller"
_LABELS = {"app.kubernetes.io/name": NAME}
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-sandbox-controller"
_GROUP = "agentplane.allegedly.works"
_HEALTH_PORT = 8081


_DEFAULT_MEMORY_REQUEST = Size.mebibytes(64)
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
        placement: node_scheduling.Placement | None = node_scheduling.HIL_OVH,
        registry_secret_name: str | None = "forgejo-images-creds",
        memory_request: Size = _DEFAULT_MEMORY_REQUEST,
        memory_limit: Size | None = _DEFAULT_MEMORY_LIMIT,
    ) -> None:
        """The controller quiesces runners through the stable Runner Gateway Service under a token
        of `gateway_audience`."""
        super().__init__(scope, id)
        self.env = env
        self.gateway = gateway
        namespace = env.namespace
        endpoint = metrics(namespace)
        workload = add_service_workload(
            self,
            env,
            serves=[endpoint],
            container="sandbox-controller",
            image=f"{_IMAGE}:unset",
            extra_ports=[Port(name="health", number=_HEALTH_PORT).container_port()],
            readiness=http_probe("/readyz", port=_HEALTH_PORT, initial_delay_seconds=3),
            liveness=http_probe("/healthz", port=_HEALTH_PORT, initial_delay_seconds=10, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(10)),
                memory=MemoryResources(request=memory_request, limit=memory_limit),
            ),
            termination_grace=Duration.seconds(30),
            placement=placement,
            telemetry_enabled=True,
            registry_secret_name=registry_secret_name,
        )
        runner_tls = workload_tls.client_certificate(
            self, env, container=workload.container, name=NAME, uri=SANDBOX_CONTROLLER_URI
        )
        settings = Settings(
            runner_gateway_tls=workload_tls.client_tls(SESSION_DISPATCHER_URI),
            runner_security=MTLS(tls=runner_tls),
            namespace=namespace,
            metrics_bind_address=f":{endpoint.port.number}",
            health_probe_bind_address=f":{_HEALTH_PORT}",
            runner_gateway_target=gateway_control_target,
            runner_gateway_token_file=Path(_GATEWAY_TOKEN_DIR) / "token",
        )
        settings_file = SettingsFile(
            self,
            "settings",
            metadata=ApiObjectMetadata(name=f"{NAME}-settings", namespace=namespace),
            content=yaml_config(settings),
            path=_SETTINGS_PATH,
        )
        settings_file.mount_into(workload.container, env=Settings.config_file_env)
        workload_tls.mount_trust_bundle(self, workload.container)
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
                # The controller is the only writer of Sandbox status, which holds the runner
                # credential binding; Sandbox writers holding patch must not get this rule.
                RolePolicyRule(resources=[custom_resource(_GROUP, "sandboxes/status")], verbs=["update"]),
                # A Pod's controller owner reference sets blockOwnerDeletion, which admission allows
                # only to who may update the owner's finalizers.
                RolePolicyRule(resources=[custom_resource(_GROUP, "sandboxes/finalizers")], verbs=["update"]),
                RolePolicyRule(resources=[custom_resource(_GROUP, "sandboxtemplates")], verbs=["get", "list", "watch"]),
                RolePolicyRule(
                    resources=[custom_resource("", "pods")],
                    verbs=["get", "list", "watch", "create", "update", "delete"],
                ),
                RolePolicyRule(resources=[custom_resource("", "pods/proxy")], verbs=["get"]),
                # Each Sandbox's request for its runner credential; the Policy Controller issues it
                # and alone writes its status.
                RolePolicyRule(
                    resources=[custom_resource(_GROUP, "workloadcredentials")],
                    verbs=["get", "list", "watch", "create", "update", "delete"],
                ),
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
                # CLEANUP(added 2026-11-14): Remove after the Controller image that stops
                # creating per-Sandbox control Services is pinned and all old Pods have left.
                RolePolicyRule(
                    resources=[custom_resource("", "services")], verbs=["get", "list", "watch", "create", "patch"]
                ),
                # Each Sandbox's launch ConfigMap (its identity and bootstrap), kept current while it lives.
                RolePolicyRule(
                    resources=[custom_resource("", "configmaps")], verbs=["get", "list", "watch", "create", "patch"]
                ),
                # The Identity each Sandbox acts as, read live before a start; a watch retries the
                # Sandboxes naming one that appears or goes.
                RolePolicyRule(resources=[custom_resource(_GROUP, "identities")], verbs=["get", "list", "watch"]),
                # Leader election; connection Lease permissions await the Controller image pin.
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
            egress=[
                cilium.dns_egress(),
                EgressRule.to_entities(Entity.KUBE_APISERVER),
                self.gateway.egress(),
                EgressRule.to_endpoints(
                    environment_sandbox.pods(namespace).cilium, environment_sandbox.ENVIRONMENT_RUNTIME_SERVICE_PORT
                ),
            ],
        )
        ServiceMonitor(
            self,
            "service-monitor",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            selector=ServiceMonitorSpecSelector(match_labels=endpoint.pods.selector),
            endpoints=[Endpoint.plain(port=endpoint.port.name)],
        )

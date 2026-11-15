"""The Sandbox Controller (agentplane/environments) reconciles state and optional workspace
PVCs, WorkloadCredentials and Pod or VM incarnations for Sandboxes using their referenced Identities."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import ContainerResources, Cpu, CpuResources, MemoryResources, Role, RoleBinding, RolePolicyRule
from constructs import Construct
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector

from agentplane.environments.settings import EnvironmentService, ServiceDelegation, ServiceDelegator, Settings
from agentplane.identity.auth import listener as workload_listener
from agentplane.net.peertls.config import (
    ACTIONS_URI,
    APP_URI,
    MTLS,
    NOTIFICATIONS_URI,
    SANDBOX_CONTROLLER_URI,
    SESSION_DISPATCHER_URI,
    ClientTLS,
    ServiceSPIFFEID,
)
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s import cilium, node_scheduling
from cluster.cdk8s.agentplane import actions, app as app_component, environment_sandbox, workload_tls
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
_ENVIRONMENT_SERVICE_PORT = 8082
_ENVIRONMENT_SERVICE = "/agentplane.environments.v1.EnvironmentService/"
_ENVIRONMENT_SERVICE_READ_METHODS = (
    f"{_ENVIRONMENT_SERVICE}ListEnvironments",
    f"{_ENVIRONMENT_SERVICE}GetEnvironment",
    f"{_ENVIRONMENT_SERVICE}WatchEnvironment",
)
OPERATOR_SERVICE_CALLERS = frozenset({APP_URI})
WAKE_SERVICE_CALLERS = frozenset({APP_URI, NOTIFICATIONS_URI})
_RESUME_METHOD = f"{_ENVIRONMENT_SERVICE}ResumeEnvironment"


def service(namespace: str) -> ServiceRef:
    """The Controller's stable every-replica EnvironmentService listener."""
    return ServiceRef(name=NAME, port=Port(name="grpc", number=_ENVIRONMENT_SERVICE_PORT), pods=pods(namespace))


def _account(account: ServiceAccountRef) -> str:
    return f"{account.namespace}/{account.name}"


@dataclass(frozen=True)
class EnvironmentServiceAuthority:
    """The shared authority projection for the legacy Gateway and the Controller receiver."""

    readers: frozenset[str]
    managers: frozenset[str]
    launchers: frozenset[str]
    operators: frozenset[str]
    service_readers: frozenset[ServiceSPIFFEID]
    service_managers: frozenset[ServiceSPIFFEID]
    service_launchers: frozenset[ServiceSPIFFEID]
    service_operators: frozenset[ServiceSPIFFEID]
    wake_callers: frozenset[str]
    wake_service_callers: frozenset[ServiceSPIFFEID]
    service_delegation_actor: ServiceSPIFFEID | None
    service_delegation_methods: tuple[str, ...]


def environment_service_authority(
    env: Environment,
    *,
    operator_callers: Iterable[ServiceAccountRef],
    launchers: Iterable[ServiceAccountRef],
    operator_service_callers: frozenset[ServiceSPIFFEID],
    wake_callers: Iterable[ServiceAccountRef],
    wake_service_callers: frozenset[ServiceSPIFFEID],
    delegation_actor: ServiceAccountRef | None,
    extra_delegated_methods: tuple[str, ...] = (),
) -> EnvironmentServiceAuthority:
    """Compose only the EnvironmentService slice of the Gateway's existing grants."""
    launcher_accounts = frozenset(map(_account, launchers))
    delegated_methods = (
        tuple(
            method
            for method in (*_ENVIRONMENT_SERVICE_READ_METHODS, *extra_delegated_methods)
            if method.startswith(_ENVIRONMENT_SERVICE)
        )
        if delegation_actor is not None
        else ()
    )
    return EnvironmentServiceAuthority(
        readers=frozenset({_account(ServiceAccountRef(namespace=env.namespace, name=app_component.NAME))}),
        managers=frozenset({_account(ServiceAccountRef(namespace=env.namespace, name=app_component.NAME))}),
        launchers=launcher_accounts,
        operators=frozenset(map(_account, operator_callers)),
        service_readers=frozenset({APP_URI}),
        service_managers=frozenset({APP_URI}),
        service_launchers=(
            frozenset({ACTIONS_URI})
            if _account(ServiceAccountRef(namespace=env.namespace, name=actions.NAME)) in launcher_accounts
            else frozenset()
        ),
        service_operators=operator_service_callers,
        wake_callers=frozenset(map(_account, wake_callers)),
        wake_service_callers=wake_service_callers,
        service_delegation_actor=ACTIONS_URI if delegated_methods else None,
        service_delegation_methods=delegated_methods,
    )


def action_delegation_actor(env: Environment) -> ServiceAccountRef | None:
    """The Action account that owns optional environment management and Session groups."""
    if env.actions.orchestration_v1 is None and env.actions.session_delegation is None:
        return None
    return ServiceAccountRef(namespace=env.namespace, name=actions.NAME)


def environment_service_delegated_methods(env: Environment) -> tuple[str, ...]:
    """Explicit resume is delegated only where an Environment Action group is configured."""
    return (_RESUME_METHOD,) if action_delegation_actor(env) is not None else ()


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
        authority: EnvironmentServiceAuthority,
        placement: node_scheduling.Placement | None = node_scheduling.HIL_OVH,
        registry_secret_name: str | None = "forgejo-images-creds",
        memory_request: Size = _DEFAULT_MEMORY_REQUEST,
        memory_limit: Size | None = _DEFAULT_MEMORY_LIMIT,
    ) -> None:
        """The controller quiesces runners through the stable Runner Gateway Service with its SVID."""
        super().__init__(scope, id)
        self.env = env
        self.gateway = gateway
        self.authority = authority
        namespace = env.namespace
        endpoint = metrics(namespace)
        environment_endpoint = service(namespace)
        workload = add_service_workload(
            self,
            env,
            serves=[endpoint, environment_endpoint],
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
        assert runner_tls.cert_file is not None
        assert runner_tls.key_file is not None
        assert runner_tls.ca_file is not None
        environment_service = EnvironmentService(
            rpc=workload_listener.Listener(
                listen_address=f"0.0.0.0:{environment_endpoint.port.number}",
                tls=workload_listener.TLS(cert_file=Path(runner_tls.cert_file), key_file=Path(runner_tls.key_file)),
                auth=[workload_listener.SPIFFEAuth(ca_file=Path(runner_tls.ca_file))],
            ),
            policy_namespace=namespace,
            audience=gateway_audience,
            readers=tuple(sorted(authority.readers)),
            managers=tuple(sorted(authority.managers)),
            launchers=tuple(sorted(authority.launchers)),
            operators=tuple(sorted(authority.operators)),
            service_readers=tuple(sorted(authority.service_readers)),
            service_managers=tuple(sorted(authority.service_managers)),
            service_launchers=tuple(sorted(authority.service_launchers)),
            service_operators=tuple(sorted(authority.service_operators)),
            wake_callers=tuple(sorted(authority.wake_callers)),
            wake_service_callers=tuple(sorted(authority.wake_service_callers)),
            service_delegation=(
                ServiceDelegation(
                    actors=(ServiceDelegator(spiffe_id=authority.service_delegation_actor),),
                    methods=authority.service_delegation_methods,
                )
                if authority.service_delegation_actor is not None
                else None
            ),
            runtime_catalog=env.runtime_catalog,
        )
        settings = Settings(
            environment_service=environment_service,
            runner_gateway_tls=ClientTLS(
                ca_file=Path(runner_tls.ca_file),
                server_uri=SESSION_DISPATCHER_URI,
                cert_file=Path(runner_tls.cert_file),
                key_file=Path(runner_tls.key_file),
                client_uri=SANDBOX_CONTROLLER_URI,
            ),
            runner_security=MTLS(tls=runner_tls),
            namespace=namespace,
            metrics_bind_address=f":{endpoint.port.number}",
            health_probe_bind_address=f":{_HEALTH_PORT}",
            runner_gateway_target=gateway_control_target,
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
                # Controller reconciliation and every-replica EnvironmentService management share
                # Sandbox writes; the latter only changes the live object through guarded patches.
                RolePolicyRule(
                    resources=[custom_resource(_GROUP, "sandboxes")],
                    verbs=["get", "list", "watch", "create", "update", "patch", "delete"],
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
                # Each Sandbox's launch ConfigMap (its identity and bootstrap), kept current while it lives.
                RolePolicyRule(
                    resources=[custom_resource("", "configmaps")], verbs=["get", "list", "watch", "create", "patch"]
                ),
                # Resolve the caller's ServiceAccount for SVID revocation and keep the independent
                # EnvironmentService policy watches current on every replica.
                RolePolicyRule(resources=[custom_resource("", "serviceaccounts")], verbs=["get", "list", "watch"]),
                RolePolicyRule(
                    resources=[
                        custom_resource(_GROUP, resource) for resource in ("identities", "policysets", "policybindings")
                    ],
                    verbs=["get", "list", "watch"],
                ),
                RolePolicyRule(resources=[custom_resource(_GROUP, "policybindings/status")], verbs=["patch"]),
                # Controller-runtime leader election reads, creates, and updates its coordination Lease.
                RolePolicyRule(
                    resources=[custom_resource("coordination.k8s.io", "leases")], verbs=["get", "create", "update"]
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
        """Add the scoped receiver network policy and metrics scrape."""
        namespace = self.env.namespace
        endpoint = metrics(namespace)
        environment_endpoint = service(namespace)
        environment_callers = []
        if self.env.app is not None:
            environment_callers.append(app_component.service(namespace).pods)
        if self.authority.service_delegation_actor is not None:
            environment_callers.append(actions.service(namespace).pods)
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=NAME.removeprefix("agentplane-"), namespace=namespace),
            endpoint_selector=endpoint.pods.selector,
            ingress=[
                cilium.SCRAPERS.admit(endpoint.pod_port),
                *(caller.admit(environment_endpoint.pod_port) for caller in environment_callers),
            ],
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

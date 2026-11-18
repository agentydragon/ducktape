"""The Sandbox Controller (agentplane/environments) reconciles state and optional workspace
PVCs, WorkloadCredentials and Pod or VM incarnations for Sandboxes using their referenced Identities."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

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
    ServiceAccount,
    k8s,
)
from constructs import Construct
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitor

from agentplane.environments.settings import (
    EnvironmentService,
    RuntimeProxy,
    ServiceDelegation,
    ServiceDelegator,
    Settings,
)
from agentplane.identity.auth import listener as workload_listener
from agentplane.net.peertls.config import (
    ACTIONS_URI,
    APP_URI,
    CENTRAL_SAMPLER_URI,
    GATEWAY_RUNTIME_PROXY_URI,
    GATEWAY_SESSIONS_URI,
    MTLS,
    NOTIFICATIONS_URI,
    SANDBOX_CONTROLLER_URI,
    ClientTLS,
    ServiceSPIFFEID,
)
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s import cilium, node_scheduling
from cluster.cdk8s.agentplane import (
    actions,
    app as app_component,
    central_sampler,
    egress,
    environment_sandbox,
    environment_service,
    notifications,
    runner_gateway,
    workload_tls,
)
from cluster.cdk8s.agentplane.environment import Environment, ReplicaProfile
from cluster.cdk8s.agentplane.service_workload import ServiceWorkload, add_service_workload
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile

_SETTINGS_PATH = "/etc/agentplane-sandbox-controller/config.yaml"

NAME = environment_service.NAME
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-sandbox-controller"
_GROUP = "agentplane.allegedly.works"
_HEALTH_PORT = 8081
_ENVIRONMENT_SERVICE = "/agentplane.environments.v1.EnvironmentService/"
_ENVIRONMENT_SERVICE_READ_METHODS = (
    f"{_ENVIRONMENT_SERVICE}ListEnvironments",
    f"{_ENVIRONMENT_SERVICE}GetEnvironment",
    f"{_ENVIRONMENT_SERVICE}WatchEnvironment",
)
OPERATOR_SERVICE_CALLERS = frozenset({APP_URI})
WAKE_SERVICE_CALLERS = frozenset({APP_URI, NOTIFICATIONS_URI})
_RESUME_METHOD = f"{_ENVIRONMENT_SERVICE}ResumeEnvironment"


def _account(account: ServiceAccountRef) -> str:
    return f"{account.namespace}/{account.name}"


@dataclass(frozen=True)
class EnvironmentServiceAuthority:
    """The service identity grants for the EnvironmentService receiver."""

    service_readers: frozenset[ServiceSPIFFEID]
    service_managers: frozenset[ServiceSPIFFEID]
    service_launchers: frozenset[ServiceSPIFFEID]
    service_operators: frozenset[ServiceSPIFFEID]
    wake_service_callers: frozenset[ServiceSPIFFEID]
    service_delegation_actor: ServiceSPIFFEID | None
    service_delegation_methods: tuple[str, ...]


def environment_service_authority(
    env: Environment,
    *,
    launchers: Iterable[ServiceAccountRef],
    operator_service_callers: frozenset[ServiceSPIFFEID],
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
        service_readers=frozenset({APP_URI}),
        service_managers=frozenset({APP_URI}),
        service_launchers=(
            frozenset({ACTIONS_URI})
            if _account(ServiceAccountRef(namespace=env.namespace, name=actions.NAME)) in launcher_accounts
            else frozenset()
        ),
        service_operators=operator_service_callers,
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
        name=NAME, port=Port(name="metrics", number=8080), pods=environment_service.service(namespace).pods
    )


def pods(namespace: str) -> Pods:
    return metrics(namespace).pods


def _add_controller_workload(
    scope: Construct,
    env: Environment,
    *,
    endpoint: ServiceRef,
    environment_endpoint: ServiceRef,
    image: str,
    placement: node_scheduling.Placement | None,
    registry_secret_name: str | None,
    memory_request: Size,
    memory_limit: Size | None,
    service_account: ServiceAccount | None = None,
    replica_profile: ReplicaProfile | None = None,
) -> ServiceWorkload:
    workload = add_service_workload(
        scope,
        env,
        serves=[endpoint, environment_endpoint],
        container=endpoint.name,
        image=image,
        extra_ports=[Port(name="health", number=_HEALTH_PORT).container_port()],
        readiness=http_probe("/readyz", port=_HEALTH_PORT, initial_delay_seconds=3),
        liveness=http_probe("/healthz", port=_HEALTH_PORT, initial_delay_seconds=10, period_seconds=30),
        resources=ContainerResources(
            cpu=CpuResources(request=Cpu.millis(10)), memory=MemoryResources(request=memory_request, limit=memory_limit)
        ),
        termination_grace=Duration.seconds(30),
        placement=placement,
        telemetry_enabled=True,
        registry_secret_name=registry_secret_name,
        service_account=service_account,
        replica_profile=replica_profile,
        create_service=False,
    )
    # Both Services route to old and new Pods without changing Deployment selectors.
    for key, value in environment_service.ENVIRONMENTS_ROUTING_LABELS.items():
        workload.deployment.pod_metadata.add_label(key, value)

    # Preserve telemetry continuity while the new Deployment is observed on staging.
    workload.container.env.add_variable("OTEL_SERVICE_NAME", EnvValue.from_value(NAME))
    return workload


class SandboxController(Construct):
    """The controller's Deployment and RBAC; fleet network policy and metrics are added separately."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        environments_image: str,
        authority: EnvironmentServiceAuthority,
        runtime_proxy_readers: Iterable[ServiceSPIFFEID],
        placement: node_scheduling.Placement | None = node_scheduling.HIL_OVH,
        registry_secret_name: str | None = "forgejo-images-creds",
        memory_request: Size = _DEFAULT_MEMORY_REQUEST,
        memory_limit: Size | None = _DEFAULT_MEMORY_LIMIT,
        legacy_replica_profile: ReplicaProfile | None = None,
    ) -> None:
        """The controller uses its SVID; callers choose the fixture or production Runtime-proxy readers."""
        super().__init__(scope, id)
        self.env = env
        self.authority = authority
        namespace = env.namespace
        endpoint = metrics(namespace)
        environment_endpoint = environment_service.service(namespace)
        workload = _add_controller_workload(
            self,
            env,
            endpoint=endpoint,
            environment_endpoint=environment_endpoint,
            image=f"{_IMAGE}:unset",
            placement=placement,
            registry_secret_name=registry_secret_name,
            memory_request=memory_request,
            memory_limit=memory_limit,
            replica_profile=legacy_replica_profile,
        )
        environments = environment_service.environments_service(namespace)
        # Keep the old metrics Service and one scrape over both sets of Pods.
        for service_id, service, ports in (
            ("service", endpoint, [endpoint.port, environment_endpoint.port]),
            ("environments-service", environments, [environments.port]),
        ):
            k8s.KubeService(
                self,
                service_id,
                metadata=k8s.ObjectMeta(name=service.name, namespace=namespace, labels=service.labels),
                spec=k8s.ServiceSpec(
                    type="ClusterIP",
                    external_i_ps=[],
                    selector=environment_service.ENVIRONMENTS_ROUTING_LABELS,
                    ports=[port.k8s_service_port() for port in ports],
                ),
            )
        runner_tls = workload_tls.client_certificate(
            self, env, container=workload.container, name=NAME, uri=SANDBOX_CONTROLLER_URI
        )
        assert runner_tls.cert_file is not None
        assert runner_tls.key_file is not None
        assert runner_tls.ca_file is not None
        proxy_tls = workload_tls.client_certificate(
            self, env, container=workload.container, name=f"{NAME}-runtime-proxy", uri=GATEWAY_RUNTIME_PROXY_URI
        )
        serving = EnvironmentService(
            rpc=workload_listener.Listener(
                listen_address=f"0.0.0.0:{environment_endpoint.port.number}",
                tls=workload_listener.TLS(cert_file=Path(runner_tls.cert_file), key_file=Path(runner_tls.key_file)),
                auth=[workload_listener.SPIFFEAuth(ca_file=Path(runner_tls.ca_file))],
            ),
            policy_namespace=namespace,
            service_readers=tuple(sorted(authority.service_readers)),
            service_managers=tuple(sorted(authority.service_managers)),
            service_launchers=tuple(sorted(authority.service_launchers)),
            service_operators=tuple(sorted(authority.service_operators)),
            wake_service_callers=tuple(sorted(authority.wake_service_callers)),
            service_delegation=(
                ServiceDelegation(
                    actors=(ServiceDelegator(spiffe_id=authority.service_delegation_actor),),
                    methods=authority.service_delegation_methods,
                )
                if authority.service_delegation_actor is not None
                else None
            ),
            runtime_proxy=RuntimeProxy(
                runner_security=MTLS(tls=proxy_tls),
                readers=tuple(sorted(runtime_proxy_readers)),
                session_callers=(GATEWAY_SESSIONS_URI,),
                controller_callers=(SANDBOX_CONTROLLER_URI,),
                executor_callers=(CENTRAL_SAMPLER_URI,) if env.central_sampler is not None else (),
            ),
            runtime_catalog=env.runtime_catalog,
        )
        settings = Settings(
            environment_service=serving,
            runner_gateway_tls=ClientTLS(
                ca_file=Path(runner_tls.ca_file),
                server_uri=SANDBOX_CONTROLLER_URI,
                cert_file=Path(runner_tls.cert_file),
                key_file=Path(runner_tls.key_file),
                client_uri=SANDBOX_CONTROLLER_URI,
            ),
            runner_security=MTLS(tls=runner_tls),
            namespace=namespace,
            metrics_bind_address=f":{endpoint.port.number}",
            health_probe_bind_address=f":{_HEALTH_PORT}",
            runner_gateway_target=environment_service.control_target(namespace),
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
        add_rbac(self, workload.account, namespace, role_name=NAME)
        prerequisites = Construct(self, "environments-prerequisites")
        account = ServiceAccount(
            prerequisites,
            "account",
            metadata=ApiObjectMetadata(
                name=environment_service.environments_service(namespace).name, namespace=namespace
            ),
            automount_token=True,
        )
        add_rbac(prerequisites, account, namespace, role_name=environment_service.environments_service(namespace).name)
        workload_tls.certificate(
            prerequisites,
            "serving",
            env,
            name="environments-client-workload",
            secret_name="environments-client-workload-tls",
            uri=SANDBOX_CONTROLLER_URI,
        )
        workload_tls.certificate(
            prerequisites,
            "proxy",
            env,
            name="environments-runtime-proxy-client-workload",
            secret_name="environments-runtime-proxy-client-workload-tls",
            uri=GATEWAY_RUNTIME_PROXY_URI,
        )

        environments_scope = Construct(self, "environments")
        environments_workload = _add_controller_workload(
            environments_scope,
            env,
            endpoint=ServiceRef(name=environments.name, port=endpoint.port, pods=environments.pods),
            environment_endpoint=environments,
            image=environments_image,
            placement=placement,
            registry_secret_name=registry_secret_name,
            memory_request=memory_request,
            memory_limit=memory_limit,
            service_account=account,
        )
        client = workload_tls.mount_client_certificate(
            environments_scope, container=environments_workload.container, name="environments"
        )
        proxy_client = workload_tls.mount_client_certificate(
            environments_scope, container=environments_workload.container, name="environments-runtime-proxy"
        )
        workload_tls.mount_trust_bundle(environments_scope, environments_workload.container)
        assert client.cert_file is not None
        assert client.key_file is not None
        credentials = {"cert_file": Path(client.cert_file), "key_file": Path(client.key_file)}
        assert settings.runner_gateway_tls is not None
        assert serving.runtime_proxy is not None
        environments_settings = settings.model_copy(
            update={
                "runner_gateway_target": environment_service.environments_control_target(namespace),
                "runner_security": MTLS(tls=client),
                "runner_gateway_tls": settings.runner_gateway_tls.model_copy(update=credentials),
                "environment_service": serving.model_copy(
                    update={
                        "rpc": serving.rpc.model_copy(update={"tls": workload_listener.TLS(**credentials)}),
                        "runtime_proxy": serving.runtime_proxy.model_copy(
                            update={"runner_security": MTLS(tls=proxy_client)}
                        ),
                    }
                ),
            }
        )
        SettingsFile(
            environments_scope,
            "settings",
            metadata=ApiObjectMetadata(name="environments-settings", namespace=namespace),
            content=yaml_config(environments_settings),
            path="/etc/agentplane-environments/config.yaml",
        ).mount_into(environments_workload.container, env="AGENTPLANE_ENVIRONMENTS_CONFIG_FILE")

    def add_platform_resources(self) -> None:
        """Add the scoped receiver network policy and metrics scrape."""
        namespace = self.env.namespace
        endpoint = metrics(namespace)
        environment_endpoint = environment_service.service(namespace)
        environment_callers = [
            egress.proxy(namespace).pods,
            endpoint.pods,
            runner_gateway.service(namespace).pods,
            notifications.service(namespace).pods,
        ]
        if self.env.central_sampler is not None:
            environment_callers.append(central_sampler.pods(namespace))
        if self.env.app is not None:
            environment_callers.append(app_component.service(namespace).pods)
        if self.authority.service_delegation_actor is not None:
            environment_callers.append(actions.service(namespace).pods)
        environments = environment_service.environments_service(namespace)
        environment_callers.extend([environments.pods, runner_gateway.sessions_service(namespace).pods])
        inbound = [
            cilium.SCRAPERS.admit(endpoint.pod_port),
            *(caller.admit(environment_endpoint.pod_port) for caller in environment_callers),
        ]
        outbound = [
            cilium.dns_egress(),
            EgressRule.to_entities(Entity.KUBE_APISERVER),
            environment_endpoint.egress(),
            environments.egress(),
            EgressRule.to_endpoints(
                environment_sandbox.pods(namespace).cilium, environment_sandbox.ENVIRONMENT_RUNTIME_SERVICE_PORT
            ),
        ]
        for id, service in (("network-policy", environment_endpoint), ("environments-network-policy", environments)):
            NetworkPolicy(
                self,
                id,
                metadata=ApiObjectMetadata(name=service.name, namespace=namespace),
                endpoint_selector=service.pods.selector,
                ingress=(
                    inbound
                    if service == environment_endpoint
                    else [*inbound, environment_sandbox.pods(namespace).admit(service.pod_port)]
                ),
                egress=outbound,
            )
        for caller in [*environment_callers, environment_sandbox.pods(namespace)]:
            NetworkPolicy(
                self,
                f"environments-client-{caller.selector['app.kubernetes.io/name']}",
                metadata=ApiObjectMetadata(
                    name=f"environments-client-{caller.selector['app.kubernetes.io/name']}", namespace=namespace
                ),
                endpoint_selector=caller.selector,
                egress=[environments.egress()],
            )
        ServiceMonitor(
            self,
            "service-monitor",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            spec=endpoint.service_monitor_spec(),
        )


def add_rbac(scope: Construct, service_account: ServiceAccount, namespace: str, *, role_name: str) -> None:
    Role(
        scope,
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
                resources=[custom_resource("", "pods")], verbs=["get", "list", "watch", "create", "update", "delete"]
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
                resources=[custom_resource("kubevirt.io", "virtualmachineinstances")], verbs=["get", "list", "watch"]
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
        scope,
        "role-binding",
        metadata=ApiObjectMetadata(name=role_name, namespace=namespace),
        role=Role.from_role_name(scope, "role-ref", role_name),
    ).add_subjects(service_account)

"""The Gateway workload with embedded startup migrations.

The public API and stable Service address are defined in `runner_gateway.py`; this module wires
the Dispatcher listener and its Environments and sampler client connections."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast

from cdk8s import ApiObjectMetadata, App, Chart, Duration, Size
from cdk8s_plus_34 import (
    ApiResource,
    ContainerResources,
    Cpu,
    CpuResources,
    DeploymentStrategy,
    EnvValue,
    IApiResource,
    MemoryResources,
    PercentOrAbsolute,
    Role,
    RoleBinding,
    RolePolicyRule,
    ServiceAccount,
    k8s,
)
from constructs import Construct

from agentplane.crd_group import GROUP
from agentplane.identity.auth import listener as workload_listener
from agentplane.net.peertls.config import (
    ACTIONS_URI,
    APP_URI,
    CENTRAL_SAMPLER_URI,
    GATEWAY_SESSIONS_URI,
    NOTIFICATIONS_URI,
    SANDBOX_CONTROLLER_URI,
    SESSION_DISPATCHER_URI,
    ClientTLS,
    ServiceSPIFFEID,
)
from agentplane.runner_gateway.settings import CentralSamplerTarget, ServiceDelegation, ServiceDelegator, Settings
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s import cilium, node_scheduling
from cluster.cdk8s.agentplane import (
    actions,
    app as app_component,
    central_sampler,
    database,
    egress,
    environment_service,
    notifications,
    runner_gateway,
    sandbox_controller,
    session_dispatcher,
    workload_tls,
)
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.service_workload import add_service_workload
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.probes import tls_grpc_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile

_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-runner-gateway:unset"
_GRPC_PORT = 8080

_DEFAULT_MEMORY_REQUEST = Size.mebibytes(256)
_DEFAULT_MEMORY_LIMIT = Size.gibibytes(1)


def dispatcher_service(namespace: str) -> ServiceRef:
    """Keep the sender's DNS and Service port; target the shared listener."""
    return ServiceRef(
        name=session_dispatcher.NAME,
        port=Port(name="grpc", number=_GRPC_PORT),
        pods=runner_gateway.service(namespace).pods,
        target_port=_GRPC_PORT,
    )


def add_stable_services(scope: Construct, env: Environment) -> None:
    """Keep stable Services in their root Flux owner, targeting the shared listener."""
    proxy = runner_gateway.service(env.namespace)
    dispatcher = dispatcher_service(env.namespace)
    k8s.KubeService(
        scope,
        "runner-gateway-service",
        metadata=k8s.ObjectMeta(name=proxy.name, namespace=env.namespace, labels=proxy.labels),
        spec=k8s.ServiceSpec(selector=proxy.pods.selector, ports=[proxy.port.k8s_service_port()]),
    )
    k8s.KubeService(
        scope,
        "session-dispatcher-service",
        metadata=k8s.ObjectMeta(
            name=dispatcher.name, namespace=env.namespace, labels={"app.kubernetes.io/name": dispatcher.name}
        ),
        spec=k8s.ServiceSpec(
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


class RunnerGateway(Construct):
    """The Dispatcher listener and its narrow Sessions client SVID."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        database_url: EnvValue,
        dispatcher_callers: Mapping[ServiceAccountRef, Pods],
        orchestration_callers: Mapping[ServiceAccountRef, Pods],
        delegation_actor: ServiceAccountRef | None,
        wake_service_callers: frozenset[ServiceSPIFFEID] = frozenset(),
        wake_callers: frozenset[ServiceAccountRef],
        registry_secret_name: str | None = "forgejo-images-creds",
        placement: node_scheduling.Placement | None = node_scheduling.HIL_OVH,
        container_env: Mapping[str, EnvValue] | None = None,
        memory_request: Size = _DEFAULT_MEMORY_REQUEST,
        memory_limit: Size | None = _DEFAULT_MEMORY_LIMIT,
        image: str = _IMAGE,
    ) -> None:
        super().__init__(scope, id)
        if not dispatcher_callers:
            raise ValueError("Runner Gateway needs explicit Dispatcher caller grants")
        if not wake_callers <= dispatcher_callers.keys():
            raise ValueError("wake callers must also be Dispatcher callers")

        namespace = env.namespace
        self.env = env
        self.dispatcher_callers = dispatcher_callers
        proxy = runner_gateway.service(namespace)
        # Only orchestration_v1 grants direct Actions Dispatcher authority.
        # The direct Actions SVID roles follow that exact feature gate; delegated session tools
        # do not make the Actions service a direct component caller.
        actions_orchestration_svids = (
            frozenset({ACTIONS_URI})
            if ServiceAccountRef(namespace=namespace, name=actions.NAME) in orchestration_callers
            else frozenset()
        )
        workload = add_service_workload(
            self,
            env,
            serves=[proxy],
            container="runner-gateway",
            image=image,
            env_variables={Settings.dispatcher_database_url_env: database_url, **(container_env or {})},
            readiness=tls_grpc_probe(
                port=proxy.pod_port,
                ca_file=workload_tls.TRUST_BUNDLE_FILE,
                server_uri=SESSION_DISPATCHER_URI,
                initial_delay_seconds=3,
                period_seconds=10,
            ),
            liveness=tls_grpc_probe(
                port=proxy.pod_port,
                ca_file=workload_tls.TRUST_BUNDLE_FILE,
                server_uri=SESSION_DISPATCHER_URI,
                initial_delay_seconds=20,
                period_seconds=30,
            ),
            startup=tls_grpc_probe(
                port=proxy.pod_port,
                ca_file=workload_tls.TRUST_BUNDLE_FILE,
                server_uri=SESSION_DISPATCHER_URI,
                initial_delay_seconds=0,
                period_seconds=5,
                failure_threshold=60,
            ),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(100)),
                memory=MemoryResources(request=memory_request, limit=memory_limit),
            ),
            termination_grace=Duration.seconds(60),
            # A Recreate drops Dispatcher admission and delivery, so the Gateway rolls
            # (a surged replica is Ready before the old one goes) even where the environment recreates.
            replica_profile=replace(
                env.replicas,
                strategy=DeploymentStrategy.rolling_update(
                    max_surge=PercentOrAbsolute.absolute(1), max_unavailable=PercentOrAbsolute.absolute(0)
                ),
            ),
            create_service=False,
            telemetry_enabled=True,
            registry_secret_name=registry_secret_name,
            placement=placement,
        )
        serving_tls = workload_tls.service_certificate(
            self, env, container=workload.container, name=runner_gateway.NAME, uri=SESSION_DISPATCHER_URI
        )
        sessions_tls = workload_tls.client_certificate(
            self, env, container=workload.container, name=f"{runner_gateway.NAME}-sessions", uri=GATEWAY_SESSIONS_URI
        )
        assert sessions_tls.cert_file is not None
        assert sessions_tls.key_file is not None
        assert sessions_tls.ca_file is not None
        central_sampler_target = (
            CentralSamplerTarget(
                target=central_sampler.target(namespace),
                tls=ClientTLS(
                    ca_file=Path(sessions_tls.ca_file),
                    server_uri=CENTRAL_SAMPLER_URI,
                    cert_file=Path(sessions_tls.cert_file),
                    key_file=Path(sessions_tls.key_file),
                    client_uri=GATEWAY_SESSIONS_URI,
                ),
            )
            if env.central_sampler is not None
            else None
        )
        assert serving_tls.cert_file is not None
        assert serving_tls.key_file is not None
        assert serving_tls.ca_file is not None
        dispatcher_service_delegation = None
        if delegation_actor is not None:
            dispatcher_service_delegation = ServiceDelegation(
                actors=(ServiceDelegator(spiffe_id=ACTIONS_URI, connection_provenance=False),),
                methods=(
                    "/agentplane.runner_gateway.dispatcher.SessionDispatcher/SubmitInput",
                    "/agentplane.runner_gateway.dispatcher.SessionDispatcher/ChangeModel",
                    "/agentplane.runner_gateway.dispatcher.SessionDispatcher/ChangeReasoningEffort",
                    "/agentplane.runner_gateway.dispatcher.SessionDispatcher/InterruptTurn",
                    "/agentplane.runner_gateway.dispatcher.SessionDispatcher/CreateSession",
                    "/agentplane.runner_gateway.dispatcher.SessionDispatcher/GetSession",
                    "/agentplane.runner_gateway.dispatcher.SessionDispatcher/GetDispatch",
                    "/agentplane.runner_gateway.dispatcher.SessionDispatcher/WatchDispatches",
                    "/agentplane.runner_gateway.dispatcher.SessionDispatcher/ListDispatches",
                    "/agentplane.runner_gateway.dispatcher.SessionDispatcher/CancelDispatch",
                ),
            )
        static_settings = Settings(
            sandbox_namespace=namespace,
            policy_namespace=namespace,
            kubernetes_client_qps=-1,
            rpc=workload_listener.Listener(
                listen_address=f"0.0.0.0:{proxy.pod_port}",
                tls=workload_listener.TLS(cert_file=Path(serving_tls.cert_file), key_file=Path(serving_tls.key_file)),
                auth=[workload_listener.SPIFFEAuth(ca_file=Path(serving_tls.ca_file))],
            ),
            gateway_tls=ClientTLS(
                ca_file=Path(sessions_tls.ca_file),
                server_uri=SANDBOX_CONTROLLER_URI,
                cert_file=Path(sessions_tls.cert_file),
                key_file=Path(sessions_tls.key_file),
                client_uri=GATEWAY_SESSIONS_URI,
            ),
            runner_control_target_template=environment_service.control_target(namespace),
            dispatcher_service_callers=frozenset({APP_URI, NOTIFICATIONS_URI}) | actions_orchestration_svids,
            wake_service_callers=wake_service_callers,
            dispatcher_service_delegation=dispatcher_service_delegation,
            central_sampler=central_sampler_target,
        )
        settings_file = SettingsFile(
            self,
            "settings",
            metadata=ApiObjectMetadata(name=f"{runner_gateway.NAME}-settings", namespace=namespace),
            content=yaml_config(static_settings),
            path="/etc/agentplane-runner-gateway/settings/settings.yaml",
        )
        settings_file.mount_into(workload.container, env=Settings.config_file_env)
        self._add_rbac(workload.account, namespace)

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
                # Managed Sandboxes reach the Dispatcher through the central proxy; the Gateway
                # authenticates them by their Sandbox SVID.
                egress.proxy(namespace).pods.admit(dispatcher.pod_port),
                *(pods.admit(dispatcher.pod_port) for pods in self.dispatcher_callers.values()),
            ],
            egress=[
                cilium.dns_egress(),
                EgressRule.to_entities(Entity.KUBE_APISERVER),
                database.egress(namespace),
                environment_service.service(namespace).egress(),
                *([central_sampler.service(namespace).egress()] if self.env.central_sampler is not None else []),
            ],
        )

    def _add_rbac(self, service_account: ServiceAccount, namespace: str) -> None:
        role_name = runner_gateway.NAME.removeprefix("agentplane-")
        Role(
            self,
            "role",
            metadata=ApiObjectMetadata(name=role_name, namespace=namespace),
            rules=[
                # Session admission rechecks the live Sandbox execution account.
                RolePolicyRule(
                    resources=[cast(IApiResource, ApiResource.SERVICE_ACCOUNTS)], verbs=["get", "list", "watch"]
                ),
                RolePolicyRule(
                    resources=[custom_resource(GROUP, "sandboxes")], verbs=["get", "list", "watch", "patch"]
                ),
                # A dispatcher caller's SVID names its WorkloadCredential, read live on every request.
                RolePolicyRule(resources=[custom_resource(GROUP, "workloadcredentials")], verbs=["get"]),
                # One Role covers both reads because the Sandbox namespace is the policy namespace: a
                # dispatcher caller's SVID names its Identity (policy namespace), and a Sandbox's
                # `spec.identity` names one beside it (Sandbox namespace).
                RolePolicyRule(
                    resources=[
                        custom_resource(GROUP, resource) for resource in ("identities", "policysets", "policybindings")
                    ],
                    verbs=["get", "list", "watch"],
                ),
                RolePolicyRule(resources=[custom_resource(GROUP, "policybindings/status")], verbs=["patch"]),
                # Dispatcher delivery ownership uses these Leases for atomic get/create/update operations.
                RolePolicyRule(
                    resources=[custom_resource("coordination.k8s.io", "leases")], verbs=["get", "create", "update"]
                ),
            ],
        )
        RoleBinding(
            self,
            "rolebinding",
            metadata=ApiObjectMetadata(name=role_name, namespace=namespace),
            role=Role.from_role_name(self, "role-ref", role_name),
        ).add_subjects(service_account)


def serving_chart(
    app: App, *, env: Environment, wake_service_callers: frozenset[ServiceSPIFFEID] = frozenset()
) -> Chart:
    chart = Chart(app, "agentplane", disable_resource_name_hashes=True)
    callers = {
        ServiceAccountRef(namespace=env.namespace, name=app_component.NAME): app_component.service(env.namespace).pods,
        ServiceAccountRef(namespace=env.namespace, name=notifications.NAME): notifications.service(env.namespace).pods,
    }
    orchestration = actions.orchestration_callers(env)
    session_delegation = actions.session_delegation_callers(env)
    dispatcher_database_url = (
        SecretRef(namespace=env.namespace, name="postgres-session-dispatcher")
        .key("uri")
        .env_value(chart, "dispatcher-database-url")
    )
    RunnerGateway(
        chart,
        "runner-gateway",
        env,
        database_url=dispatcher_database_url,
        dispatcher_callers=callers | orchestration | session_delegation,
        orchestration_callers=orchestration,
        delegation_actor=(sandbox_controller.action_delegation_actor(env)),
        wake_service_callers=wake_service_callers,
        # The orchestration_v1 group sends input that wakes a Sandbox launched with wakeOnSend.
        wake_callers=env.wake_caller_accounts | orchestration.keys(),
    ).add_platform_resources()
    return chart


def serving(chart: Chart, directory: RenderedDirectory, root: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart, directory.artifact.name, directory, timeout="10m", depends_on=flux_kustomization_depends_on_many(root)
    )

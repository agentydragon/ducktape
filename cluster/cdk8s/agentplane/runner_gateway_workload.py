"""The Gateway workload with embedded startup migrations.

The public API and stable Service address are defined in `runner_gateway.py`; this module wires
the Dispatcher listener and its Environments and sampler client connections."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
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
from agentplane.net.peertls.config import (
    ACTIONS_URI,
    APP_URI,
    CENTRAL_SAMPLER_URI,
    GATEWAY_SESSIONS_URI,
    NOTIFICATIONS_URI,
    SANDBOX_CONTROLLER_URI,
    SESSION_DISPATCHER_URI,
    ServiceSPIFFEID,
)
from agentplane.session_dispatcher.settings import CentralSamplerTarget, ServiceDelegation, ServiceDelegator, Settings
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
    workload_tls,
)
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.service_database import Database
from cluster.cdk8s.agentplane.service_workload import ServiceWorkload, add_service_workload
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.probes import TlsGrpcHealth
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy
from cluster.cdk8s.service_ref import Pods, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile

_SESSIONS_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-sessions:unset"

_DEFAULT_MEMORY_REQUEST = Size.mebibytes(256)
_DEFAULT_MEMORY_LIMIT = Size.gibibytes(1)


def add_service(scope: Construct, env: Environment) -> None:
    """Keep Sessions in the root Flux owner."""
    sessions = runner_gateway.sessions_service(env.namespace)
    k8s.KubeService(
        scope,
        "sessions-service",
        metadata=k8s.ObjectMeta(name=sessions.name, namespace=env.namespace, labels=sessions.labels),
        spec=k8s.ServiceSpec(selector=runner_gateway.SESSIONS_ROUTING_LABELS, ports=[sessions.k8s_service_port()]),
    )


def _add_dispatcher_workload(
    scope: Construct,
    env: Environment,
    *,
    endpoint: ServiceRef,
    image: str,
    database_env: str,
    database_url: EnvValue,
    container_env: Mapping[str, EnvValue] | None,
    registry_secret_name: str | None,
    placement: node_scheduling.Placement | None,
    memory_request: Size,
    memory_limit: Size | None,
    service_account: ServiceAccount,
) -> ServiceWorkload:
    health = TlsGrpcHealth(
        port=endpoint.pod_port, ca_file=workload_tls.TRUST_BUNDLE_FILE, server_uri=SESSION_DISPATCHER_URI
    )
    workload = add_service_workload(
        scope,
        env,
        serves=[endpoint],
        container=endpoint.name,
        image=image,
        env_variables={database_env: database_url, **(container_env or {})},
        readiness=health.readiness(),
        liveness=health.liveness(),
        startup=health.startup(),
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
        service_account=service_account,
        registry_secret_name=registry_secret_name,
        placement=placement,
    )
    for key, value in runner_gateway.SESSIONS_ROUTING_LABELS.items():
        workload.deployment.pod_metadata.add_label(key, value)
    # Preserve the OpenTelemetry service identity across the deployment rename.
    workload.container.env.add_variable("OTEL_SERVICE_NAME", EnvValue.from_value("runner-gateway"))
    return workload


class RunnerGateway(Construct):
    """The Dispatcher listener and its narrow Sessions client SVID."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        database_url: EnvValue,
        sessions_image: str,
        dispatcher_callers: Mapping[ServiceAccountRef, Pods],
        orchestration_callers: Mapping[ServiceAccountRef, Pods],
        delegation_actor: ServiceAccountRef | None,
        wake_service_callers: frozenset[ServiceSPIFFEID] = frozenset(),
        registry_secret_name: str | None = "forgejo-images-creds",
        placement: node_scheduling.Placement | None = node_scheduling.HIL_OVH,
        container_env: Mapping[str, EnvValue] | None = None,
        memory_request: Size = _DEFAULT_MEMORY_REQUEST,
        memory_limit: Size | None = _DEFAULT_MEMORY_LIMIT,
    ) -> None:
        super().__init__(scope, id)
        if not dispatcher_callers:
            raise ValueError("Runner Gateway needs explicit Dispatcher caller grants")

        namespace = env.namespace
        self.env = env
        self.dispatcher_callers = dispatcher_callers
        endpoint = runner_gateway.sessions_service(namespace)
        # Only orchestration_v1 grants direct Actions Dispatcher authority.
        # The direct Actions SVID roles follow that exact feature gate; delegated session tools
        # do not make the Actions service a direct component caller.
        actions_orchestration_svids = (
            frozenset({ACTIONS_URI})
            if ServiceAccountRef(namespace=namespace, name=actions.NAME) in orchestration_callers
            else frozenset()
        )
        prerequisites = Construct(self, "sessions-prerequisites")
        account = ServiceAccount(
            prerequisites,
            "account",
            metadata=ApiObjectMetadata(name=endpoint.name, namespace=namespace),
            automount_token=True,
        )
        add_rbac(prerequisites, account, namespace, role_name=endpoint.name)
        workload_tls.certificate(
            prerequisites,
            "serving",
            env,
            name="sessions-workload",
            secret_name="sessions-workload-tls",
            uri=SESSION_DISPATCHER_URI,
        )
        workload_tls.certificate(
            prerequisites,
            "client",
            env,
            name="sessions-client-workload",
            secret_name="sessions-client-workload-tls",
            uri=GATEWAY_SESSIONS_URI,
        )

        sessions = Construct(self, "sessions")
        sessions_workload = _add_dispatcher_workload(
            sessions,
            env,
            endpoint=endpoint,
            image=sessions_image,
            database_env=Settings.dispatcher_database_url_env,
            database_url=database_url,
            container_env=container_env,
            registry_secret_name=registry_secret_name,
            placement=placement,
            memory_request=memory_request,
            memory_limit=memory_limit,
            service_account=account,
        )
        serving_tls = workload_tls.mount_service_certificate(
            sessions, container=sessions_workload.container, name="sessions"
        )
        client = workload_tls.mount_client_certificate(sessions, container=sessions_workload.container, name="sessions")
        central_sampler_target = (
            CentralSamplerTarget(
                target=central_sampler.service(namespace).authority,
                tls=client.client_tls(CENTRAL_SAMPLER_URI, client_uri=GATEWAY_SESSIONS_URI),
            )
            if env.central_sampler is not None
            else None
        )
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
        sessions_settings = Settings(
            sandbox_namespace=namespace,
            policy_namespace=namespace,
            kubernetes_client_qps=-1,
            rpc=serving_tls.spiffe_listener(endpoint.pod_port),
            gateway_tls=client.client_tls(SANDBOX_CONTROLLER_URI, client_uri=GATEWAY_SESSIONS_URI),
            runner_control_target_template=environment_service.environments_control_target(namespace),
            dispatcher_service_callers=frozenset({APP_URI, NOTIFICATIONS_URI}) | actions_orchestration_svids,
            wake_service_callers=wake_service_callers,
            dispatcher_service_delegation=dispatcher_service_delegation,
            central_sampler=central_sampler_target,
        )
        SettingsFile(
            sessions,
            "settings",
            metadata=ApiObjectMetadata(name="sessions-settings", namespace=namespace),
            content=yaml_config(sessions_settings),
            path="/etc/agentplane-sessions/settings/settings.yaml",
        ).mount_into(sessions_workload.container, env=Settings.config_file_env)

    def add_platform_resources(self) -> None:
        """Add the fleet Cilium fences around the shared Gateway listeners."""
        namespace = self.env.namespace
        sessions = runner_gateway.sessions_service(namespace)
        ingress = [
            egress.proxy(namespace).pods.admit(sessions.pod_port),
            *(pods.admit(sessions.pod_port) for pods in self.dispatcher_callers.values()),
        ]
        outbound = [
            cilium.dns_egress(),
            EgressRule.to_entities(Entity.KUBE_APISERVER),
            database.egress(namespace),
            environment_service.environments_service(namespace).egress(),
            *([central_sampler.service(namespace).egress()] if self.env.central_sampler is not None else []),
        ]
        NetworkPolicy(
            self,
            "sessions-network-policy",
            metadata=ApiObjectMetadata(name=sessions.name, namespace=namespace),
            endpoint_selector=sessions.pods.selector,
            ingress=ingress,
            egress=outbound,
        )
        callers = dict.fromkeys([egress.proxy(namespace).pods, *self.dispatcher_callers.values()])
        for caller in callers:
            NetworkPolicy(
                self,
                f"sessions-client-{caller.selector['app.kubernetes.io/name']}",
                metadata=ApiObjectMetadata(
                    name=f"sessions-client-{caller.selector['app.kubernetes.io/name']}", namespace=namespace
                ),
                endpoint_selector=caller.selector,
                egress=[sessions.egress()],
            )
        if self.env.central_sampler is not None:
            sampler = central_sampler.service(namespace)
            NetworkPolicy(
                self,
                "sessions-sampler",
                metadata=ApiObjectMetadata(name="sessions-sampler", namespace=namespace),
                endpoint_selector=sampler.pods.selector,
                ingress=[sessions.pods.admit(sampler.pod_port)],
            )


def add_rbac(scope: Construct, service_account: ServiceAccount, namespace: str, *, role_name: str) -> None:
    role = Role(
        scope,
        "role",
        metadata=ApiObjectMetadata(name=role_name, namespace=namespace),
        rules=[
            # Session admission rechecks the live Sandbox execution account.
            RolePolicyRule(
                resources=[cast(IApiResource, ApiResource.SERVICE_ACCOUNTS)], verbs=["get", "list", "watch"]
            ),
            RolePolicyRule(resources=[custom_resource(GROUP, "sandboxes")], verbs=["get", "list", "watch", "patch"]),
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
        ],
    )
    RoleBinding(
        scope, "rolebinding", metadata=ApiObjectMetadata(name=role_name, namespace=namespace), role=role
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
    RunnerGateway(
        chart,
        "runner-gateway",
        env,
        database_url=Database.SESSION_DISPATCHER.url(chart, namespace=env.namespace),
        sessions_image=_SESSIONS_IMAGE,
        dispatcher_callers=callers | orchestration | session_delegation,
        orchestration_callers=orchestration,
        delegation_actor=sandbox_controller.action_delegation_actor(env),
        wake_service_callers=wake_service_callers,
    ).add_platform_resources()
    return chart

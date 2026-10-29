"""The Gateway workload and its Goose migration init container.

The public API and stable Service address are defined in `runner_gateway.py`; this module wires
the Gateway's public listeners and direct mTLS connections to runner Pods."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import cast

from cdk8s import ApiObject, ApiObjectMetadata, App, Chart, Duration, JsonPatch, Size
from cdk8s_plus_34 import (
    ApiResource,
    ContainerProps,
    ContainerResources,
    Cpu,
    CpuResources,
    DeploymentStrategy,
    EnvFieldPaths,
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
from agentplane.runner_gateway.settings import Settings
from agentplane.runner_transport.configuration import MTLS, TLS
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s import cilium, node_scheduling
from cluster.cdk8s.agentplane import (
    actions,
    app as app_component,
    database,
    notifications,
    runner_gateway,
    runner_sandbox,
    sandbox_controller,
    session_dispatcher,
)
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.migrate_container import migrate_init_container
from cluster.cdk8s.agentplane.service_workload import add_service_workload
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.cert_manager.cluster_ca import BUNDLE as TRUST_BUNDLE_NAME, BUNDLE_KEY as TRUST_BUNDLE_KEY
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.probes import grpc_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac
from util.settings_contract import checked_value, env_name, validate_settings_fragment

_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-runner-gateway:unset"
_MIGRATION_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-runner-gateway-queue-migrate:unset"
_RUNNER_TOKEN_FILE = "/var/run/secrets/agentplane-runner-gateway/token"
_RUNNER_TLS_CERT = f"{runner_gateway.GATEWAY_TLS_MOUNT_PATH}/tls.crt"
_RUNNER_TLS_KEY = f"{runner_gateway.GATEWAY_TLS_MOUNT_PATH}/tls.key"
_RUNNER_TLS_CA = f"{runner_gateway.GATEWAY_TLS_MOUNT_PATH}/ca.crt"
_GRPC_PORT = 8080

_DEFAULT_MEMORY_LIMIT = Size.gibibytes(1)


def dispatcher_service(namespace: str) -> ServiceRef:
    """Keep the sender's DNS and Service port; target the shared listener."""
    return ServiceRef(
        name=session_dispatcher.NAME,
        port=Port(name="grpc", number=_GRPC_PORT),
        pods=runner_gateway.service(namespace).pods,
        target_port=_GRPC_PORT,
    )


def _account(account: ServiceAccountRef) -> str:
    return f"{account.namespace}/{account.name}"


def add_stable_services(scope: Construct, env: Environment) -> None:
    """Keep stable Services in their root Flux owner, targeting the shared listener."""
    proxy = runner_gateway.service(env.namespace)
    dispatcher = dispatcher_service(env.namespace)
    dispatcher_http = session_dispatcher.http_service(env.namespace)
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
                ),
                dispatcher_http.port.k8s_service_port(),
            ],
        ),
    )


class RunnerGateway(Construct):
    """One workload with public gRPC, dispatcher HTTP and an mTLS client for runner Pods."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        database_url: EnvValue,
        migration: ContainerProps,
        dispatcher_callers: Mapping[ServiceAccountRef, Pods],
        gateway_callers: Mapping[ServiceAccountRef, Pods],
        wake_callers: frozenset[ServiceAccountRef],
        sandbox_controller: tuple[ServiceAccountRef, Pods],
        registry_secret_name: str | None = "forgejo-images-creds",
        placement: node_scheduling.Placement | None = node_scheduling.HIL_OVH,
        container_env: Mapping[str, EnvValue] | None = None,
        memory_limit: Size | None = _DEFAULT_MEMORY_LIMIT,
        image: str = _IMAGE,
    ) -> None:
        super().__init__(scope, id)
        if not dispatcher_callers or not gateway_callers:
            raise ValueError("Runner Gateway needs explicit Dispatcher and proxy caller grants")
        if not wake_callers <= dispatcher_callers.keys():
            raise ValueError("wake callers must also be Dispatcher callers")

        namespace = env.namespace
        self.env = env
        self.dispatcher_callers = dispatcher_callers
        self.sandbox_controller_pods = sandbox_controller[1]
        proxy = runner_gateway.service(namespace)
        dispatcher_http = session_dispatcher.http_service(namespace)
        own_account = ServiceAccountRef(namespace=namespace, name=runner_gateway.NAME)
        # A delivery worker may run on a different replica from the holder. It calls
        # the per-Sandbox control Service with this projected Gateway-audience token.
        proxy_callers = {**gateway_callers, own_account: proxy.pods}
        self.proxy_callers = proxy_callers
        scalars = {
            field: checked_value(Settings, field, value)
            for field, value in {
                "sandbox_namespace": namespace,
                "listen_address": f"0.0.0.0:{_GRPC_PORT}",
                "http_listen_address": f"0.0.0.0:{dispatcher_http.pod_port}",
                "runner_token_file": _RUNNER_TOKEN_FILE,
                "dispatcher_audience": session_dispatcher.TOKEN_AUDIENCE,
                "gateway_audience": runner_gateway.TOKEN_AUDIENCE,
                "runner_control_target_template": runner_gateway.control_target(namespace),
                "sandbox_controller_account": _account(sandbox_controller[0]),
            }.items()
        }
        dispatcher_accounts = sorted(map(_account, dispatcher_callers))
        gateway_accounts = sorted(map(_account, proxy_callers))
        wake_accounts = sorted(map(_account, wake_callers))
        # Lists are file-only settings.
        content = {
            "dispatcher_callers": dispatcher_accounts,
            "gateway_callers": gateway_accounts,
            "runner_security": MTLS(
                mode="mtls", tls=TLS(cert_file=_RUNNER_TLS_CERT, key_file=_RUNNER_TLS_KEY, ca_file=_RUNNER_TLS_CA)
            ).model_dump(mode="json"),
            **({"wake_callers": wake_accounts} if wake_accounts else {}),
        }
        settings = SettingsFile(
            self,
            "settings",
            metadata=ApiObjectMetadata(name=f"{runner_gateway.NAME}-settings", namespace=namespace),
            content=yaml_config(validate_settings_fragment(Settings, content)),
            path="/etc/agentplane-runner-gateway/settings/settings.yaml",
        )
        workload = add_service_workload(
            self,
            env,
            serves=[proxy],
            extra_ports=[dispatcher_http.container_port()],
            container="runner-gateway",
            image=image,
            env_variables={
                **{env_name(Settings, field): EnvValue.from_value(str(value)) for field, value in scalars.items()},
                env_name(Settings, "dispatcher_database_url"): database_url,
                env_name(Settings, "pod_ip"): EnvValue.from_field_ref(EnvFieldPaths.POD_IP),
                **(container_env or {}),
            },
            readiness=grpc_probe(port=proxy.pod_port, initial_delay_seconds=3, period_seconds=10),
            liveness=grpc_probe(port=proxy.pod_port, initial_delay_seconds=20, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(100)),
                memory=MemoryResources(request=Size.mebibytes(256), limit=memory_limit),
            ),
            termination_grace=Duration.seconds(60),
            # A Recreate drops delivery and every runner connection, so the Gateway rolls
            # (a surged replica is Ready before the old one goes) even where the environment recreates.
            replica_profile=replace(
                env.replicas,
                strategy=DeploymentStrategy.rolling_update(
                    max_surge=PercentOrAbsolute.absolute(1), max_unavailable=PercentOrAbsolute.absolute(0)
                ),
            ),
            create_service=False,
            migrate=migration,
            telemetry_enabled=True,
            registry_secret_name=registry_secret_name,
            placement=placement,
        )
        settings.mount_into(workload.container, env=Settings.config_file_env)
        trust_projection = k8s.ProjectedVolumeSource(
            default_mode=0o440,
            sources=[
                k8s.VolumeProjection(
                    secret=k8s.SecretProjection(
                        name=runner_gateway.GATEWAY_TLS_SECRET,
                        items=[
                            k8s.KeyToPath(key="tls.crt", path="tls.crt"),
                            k8s.KeyToPath(key="tls.key", path="tls.key"),
                        ],
                    )
                ),
                k8s.VolumeProjection(
                    config_map=k8s.ConfigMapProjection(
                        name=TRUST_BUNDLE_NAME, items=[k8s.KeyToPath(key=TRUST_BUNDLE_KEY, path="ca.crt")]
                    )
                ),
            ],
        )
        # cdk8s-plus Container.mount consumes its IStorage interface; a projected volume has no
        # cdk8s-plus wrapper, so append the generated typed Kubernetes structs directly.
        deployment = ApiObject.of(workload.deployment)
        deployment.add_json_patch(
            JsonPatch.add("/spec/template/spec/volumes/-", k8s.Volume(name="runner-tls", projected=trust_projection))
        )
        deployment.add_json_patch(
            JsonPatch.add(
                "/spec/template/spec/containers/0/volumeMounts/-",
                k8s.VolumeMount(name="runner-tls", mount_path=runner_gateway.GATEWAY_TLS_MOUNT_PATH, read_only=True),
            )
        )
        workload.project_tokens(
            "runner-gateway-token",
            "/var/run/secrets/agentplane-runner-gateway",
            {"token": runner_gateway.TOKEN_AUDIENCE},
        )
        self._add_rbac(workload.account, namespace)

    def add_platform_resources(self) -> None:
        """Add the fleet Cilium fences around the shared Gateway listeners."""
        namespace = self.env.namespace
        proxy = runner_gateway.service(namespace)
        dispatcher = dispatcher_service(namespace)
        dispatcher_http = session_dispatcher.http_service(namespace)
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=runner_gateway.NAME.removeprefix("agentplane-"), namespace=namespace),
            endpoint_selector=proxy.pods.selector,
            ingress=[
                *(pods.admit(proxy.pod_port) for pods in self.proxy_callers.values()),
                self.sandbox_controller_pods.admit(proxy.pod_port),
                *(
                    pods.admit(dispatcher.pod_port, dispatcher_http.pod_port)
                    for pods in self.dispatcher_callers.values()
                ),
            ],
            egress=[
                cilium.dns_egress(),
                EgressRule.to_entities(Entity.KUBE_APISERVER),
                database.egress(namespace),
                proxy.egress(),
                EgressRule.to_endpoints(runner_sandbox.pods(namespace).cilium, runner_sandbox.RUNNER_SERVICE_PORT),
            ],
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
                    resources=[
                        custom_resource("kubevirt.io", "virtualmachineinstances"),
                        custom_resource("kubevirt.io", "virtualmachines"),
                    ],
                    verbs=["get", "list", "watch"],
                ),
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


def serving_chart(app: App, *, env: Environment) -> Chart:
    chart = Chart(app, "agentplane", disable_resource_name_hashes=True)
    callers = {
        ServiceAccountRef(namespace=env.namespace, name=app_component.NAME): app_component.service(env.namespace).pods,
        ServiceAccountRef(namespace=env.namespace, name=notifications.NAME): notifications.service(env.namespace).pods,
    }
    orchestration = actions.orchestration_callers(env)
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
        migration=migrate_init_container(
            _MIGRATION_IMAGE, name="migrate", env_variables={"DATABASE_URL": dispatcher_database_url}
        ),
        dispatcher_callers=callers | orchestration,
        gateway_callers=callers,
        # The orchestration_v1 group sends input that wakes a Sandbox launched with wakeOnSend.
        wake_callers=env.wake_caller_accounts | orchestration.keys(),
        sandbox_controller=(
            ServiceAccountRef(namespace=env.namespace, name=sandbox_controller.NAME),
            sandbox_controller.pods(env.namespace),
        ),
    ).add_platform_resources()
    return chart


def serving(chart: Chart, directory: RenderedDirectory, root: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart, directory.artifact.name, directory, timeout="10m", depends_on=flux_kustomization_depends_on_many(root)
    )

"""The objects a deployed Agentplane environment has, as one chart. `staging.chart` calls this and
adds its environment-only objects to what it returns; that is what generate_manifests (writes it to
disk) and the tests (synthesize it in memory via `cdk8s.Testing`) build.

The caller names the Pod execution environment the chart offers and the image it runs
(`environment_template_name`, `environment_tools_image`), plus any Secret a runner workload reads as a file
(`environment_runtime_key_files`). Workload service identities and each Runtime incarnation
use the Agentplane workload CA (`cluster_ca.WORKLOAD_ISSUER`).
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import ConfigMap
from constructs import Construct

from agentplane.session_instructions.platform_instructions import SessionRuntime
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s import namespaces, node_scheduling
from cluster.cdk8s.agentplane import (
    actions,
    app as app_component,
    central_sampler_workload,
    database,
    egress,
    electric,
    environment_firecracker,
    environment_guest,
    environment_sandbox,
    environment_service,
    environment_vm,
    frontend,
    history_service,
    launcher_relay,
    llm_ingress,
    mcp_edge,
    notifications,
    runner_gateway,
    runner_gateway_workload,
    sandbox_controller,
    sandbox_pod,
    telemetry,
)
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.platform_instructions import agent_cli_environment, platform_instructions, service_urls
from cluster.cdk8s.cert_manager import cluster_ca
from cluster.cdk8s.forgejo_registry.pull_credentials import SECRET_NAME as REGISTRY_SECRET_NAME
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.providers.cilium.network_policy import NetworkPolicy
from cluster.cdk8s.secret_ref import SecretRef

RUNNER_TRUST_BUNDLE = cluster_ca.BUNDLE
RUNNER_TRUST_BUNDLE_KEY = cluster_ca.BUNDLE_KEY


class Namespace(Construct):
    """The namespace shared by an Agentplane environment."""

    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)
        namespaces.namespace(
            self,
            "namespace",
            name=env.namespace,
            # Runner Pods are Sandbox-owned, not Deployments. Static staging services opt in separately.
            vpa=Vpa.DISABLED,
            # Standing agent access to metadata and logs (Kyverno-generated bindings).
            # Environment-specific write access is layered on by its chart.
            labels={"name": env.namespace},
            annotations={"description": env.description},
        )


def environment_chart(
    app: App,
    env: Environment,
    *,
    db: database.DbProps,
    environment_template_name: str,
    environment_tools_image: str,
    environment_runtime_key_files: Sequence[environment_sandbox.KeyFile] = (),
) -> Chart:
    """The shared objects. The fleet rules attach as a synth-time validation, so a caller
    may keep adding objects to the returned chart and they are still checked."""
    chart = Chart(app, "agentplane", disable_resource_name_hashes=True)
    Namespace(chart, "namespace", env)
    NetworkPolicy(
        chart,
        "otel-egress",
        metadata=ApiObjectMetadata(name="otel-egress", namespace=env.namespace),
        endpoint_selector=telemetry.EMITTER_SERVICE_CLASS_LABELS,
        egress=[telemetry.ALLOY_OTLP_GRPC.egress()],
    )
    database.Db(chart, "db", env, db)
    app_credentials = database.postgres(env).app_secret
    app_database_url = app_credentials.key("uri").env_value(chart, "postgres-app-database-url")
    actions_database_url = (
        SecretRef(namespace=env.namespace, name="postgres-actions").key("uri").env_value(chart, "actions-database-url")
    )
    edge_database_url = (
        SecretRef(namespace=env.namespace, name="postgres-mcp-edge")
        .key("uri")
        .env_value(chart, "mcp-edge-database-url")
    )
    egress_database_url = (
        SecretRef(namespace=env.namespace, name="postgres-egress").key("uri").env_value(chart, "egress-database-url")
    )
    app_account = ServiceAccountRef(namespace=env.namespace, name=app_component.NAME)
    history_database_url = (
        SecretRef(namespace=env.namespace, name="postgres-sandbox-service")
        .key("uri")
        .env_value(chart, "history-database-url")
    )
    notifications_database_url = (
        SecretRef(namespace=env.namespace, name="postgres-notifications")
        .key("uri")
        .env_value(chart, "notifications-database-url")
    )
    electric.Electric(chart, "electric", env, placement=node_scheduling.HIL_OVH).add_platform_resources()
    llm_ingress.LlmIngress(
        chart, "llm-ingress", env, placement=node_scheduling.HIL_OVH, registry_secret_name=REGISTRY_SECRET_NAME
    ).add_platform_resources()
    egress.Egress(
        chart,
        "egress",
        env,
        database_url=egress_database_url,
        runners=environment_sandbox.pods(env.namespace),
        placement=node_scheduling.HIL_OVH,
        registry_secret_name=REGISTRY_SECRET_NAME,
        tls_passthrough_services=[
            history_service.service(env.namespace),
            llm_ingress.service(env.namespace),
            notifications.service(env.namespace),
            runner_gateway.sessions_service(env.namespace),
            environment_service.environments_service(env.namespace),
            actions.service(env.namespace),
        ],
    ).add_platform_resources()
    sandbox_pod.add_tool_config(chart, env)
    app_caller = {app_account: app_component.service(env.namespace).pods}
    environment_launchers = actions.orchestration_callers(env)
    notifications_caller = {
        ServiceAccountRef(namespace=env.namespace, name=notifications.NAME): notifications.service(env.namespace).pods
    }
    runner_gateway_workload.add_service(chart, env)
    history_service.HistoryService(
        chart,
        "history-service",
        env,
        database_url=history_database_url,
        # The app reads Threads; the Notification Service reads command receipts; orchestration_v1
        # and session_delegation use the explicitly configured Action delegation actor.
        callers=app_caller
        | notifications_caller
        | actions.orchestration_callers(env)
        | actions.session_delegation_callers(env),
        delegation_actor=sandbox_controller.action_delegation_actor(env),
        placement=node_scheduling.HIL_OVH,
        registry_secret_name=REGISTRY_SECRET_NAME,
    ).add_platform_resources()
    if env.central_sampler is not None:
        sampler_database_url = (
            SecretRef(namespace=env.namespace, name="postgres-central-sampler")
            .key("uri")
            .env_value(chart, "central-sampler-database-url")
        )
        central_sampler_workload.CentralSampler(
            chart,
            "central-sampler",
            env,
            database_url=sampler_database_url,
            placement=node_scheduling.HIL_OVH,
            registry_secret_name=REGISTRY_SECRET_NAME,
        ).add_platform_resources()
    app_component.App(
        chart,
        "app",
        env,
        electric=electric.service(env.namespace),
        database_url=app_database_url,
        environment_service_endpoint=environment_service.environments_service(env.namespace),
        placement=node_scheduling.HIL_OVH,
        registry_secret_name=REGISTRY_SECRET_NAME,
    ).add_platform_resources()
    frontend.Frontend(chart, "frontend", env).add_platform_resources()
    runner_scope = Construct(chart, "runner-sandbox")
    runner_template = environment_sandbox.EnvironmentSandboxTemplate(
        runner_scope,
        "runner-template",
        env,
        name=environment_template_name,
        image=environment_tools_image,
        key_files=environment_runtime_key_files,
        image_pull_secret_name=sandbox_pod.SECRET_NAME,
    )
    if env.vm_sandboxes:
        relay_settings = runner_template.add_launcher_relay_settings(name="launcher-relay-egress-sidecar")
        launcher_relay.LauncherRelayPolicy(
            runner_scope,
            "launcher-relay",
            namespace=env.namespace,
            template_name=environment_template_name,
            settings_config_map=relay_settings,
        )
        state_volume = runner_template.spec["stateVolume"]
        state_claim = {**state_volume["claim"], "volumeMode": "Filesystem"}
        if env.vm_storage_class is not None:
            state_claim["storageClassName"] = env.vm_storage_class
        vm_state_volume = {**state_volume, "claim": state_claim}
        vm_config = ConfigMap(
            runner_scope,
            "environment-vm-config",
            metadata=ApiObjectMetadata(name=f"{environment_vm.NAME}-config", namespace=env.namespace),
            data=environment_guest.guest_config_data(
                env,
                gateway=launcher_relay.GUEST_GATEWAY,
                block_devices=("/dev/disk/by-id/virtio-workspace",),
                platform_instructions=platform_instructions(
                    env, session_runtime=SessionRuntime.NATIVE_HARNESS, egress_host=launcher_relay.GUEST_GATEWAY
                ),
                harness_env=agent_cli_environment(service_urls(env, egress_host=launcher_relay.GUEST_GATEWAY)),
            ),
        )
        environment_vm.EnvironmentVmTemplate(
            runner_scope,
            "environment-vm-template",
            env,
            template_name=environment_vm.NAME,
            root_image=environment_vm.ROOT_IMAGE,
            config_map=vm_config,
            labels=environment_sandbox.ENVIRONMENT_RUNTIME_LABELS,
            state_volume=vm_state_volume,
            workspace_volume=environment_sandbox.workspace_volume_spec(
                vm_state_volume, layout_type="BlockFilesystem", storage_class=env.vm_storage_class
            ),
        )
    if env.firecracker_sandboxes:
        state_volume = runner_template.spec["stateVolume"]
        claim = {**state_volume["claim"], "volumeMode": "Filesystem", "resources": {"requests": {"storage": "100Gi"}}}
        if env.vm_storage_class is not None:
            claim["storageClassName"] = env.vm_storage_class
        firecracker_state = {**state_volume, "claim": claim, "layout": {"type": "RawDiskImage", "filesystem": "ext4"}}
        firecracker_workspace = environment_sandbox.workspace_volume_spec(firecracker_state, layout_type="RawDiskImage")
        firecracker_workspace["claim"]["resources"] = state_volume["claim"]["resources"]
        guest_gateway = environment_firecracker.GATEWAY
        environment_firecracker.EnvironmentFirecrackerTemplate(
            runner_scope,
            "firecracker-template",
            env,
            labels=environment_sandbox.ENVIRONMENT_RUNTIME_LABELS,
            state_volume=firecracker_state,
            workspace_volume=firecracker_workspace,
            runner_image=environment_vm.RUNNER_DISK_IMAGE,
            platform_instructions=platform_instructions(
                env, session_runtime=SessionRuntime.NATIVE_HARNESS, egress_host=guest_gateway
            ),
            harness_env=agent_cli_environment(service_urls(env, egress_host=guest_gateway)),
            image_pull_secret_name=sandbox_pod.SECRET_NAME,
        )
    runner_template.add_platform_resources(runner_scope)
    sandbox_controller.SandboxController(
        chart,
        "sandbox-controller",
        env,
        environments_image="git.allegedly.works/ducktape-ci/agentplane-environments:unset",
        authority=sandbox_controller.environment_service_authority(
            env,
            launchers=environment_launchers,
            operator_service_callers=sandbox_controller.OPERATOR_SERVICE_CALLERS,
            wake_service_callers=sandbox_controller.WAKE_SERVICE_CALLERS,
            delegation_actor=sandbox_controller.action_delegation_actor(env),
            extra_delegated_methods=sandbox_controller.environment_service_delegated_methods(env),
        ),
        runtime_proxy_readers=(),
        placement=node_scheduling.HIL_OVH,
        registry_secret_name=REGISTRY_SECRET_NAME,
    ).add_platform_resources()
    actions.Actions(
        chart,
        "actions",
        env,
        database_url=actions_database_url,
        history=history_service.service(env.namespace),
        dispatcher=runner_gateway.sessions_service(env.namespace),
        environment_service_endpoint=(
            environment_service.environments_service(env.namespace)
            if sandbox_controller.action_delegation_actor(env) is not None
            else None
        ),
        placement=node_scheduling.HIL_OVH,
        registry_secret_name=REGISTRY_SECRET_NAME,
    ).add_platform_resources()
    mcp_edge.McpEdge(chart, "mcp-edge", env, database_url=edge_database_url).add_platform_resources()
    notifications.Notifications(
        chart,
        "notifications",
        env,
        database_url=notifications_database_url,
        actions=actions.service(env.namespace),
        history=history_service.service(env.namespace),
        dispatcher=runner_gateway.sessions_service(env.namespace),
        callers=[app_component.service(env.namespace).pods],
        placement=node_scheduling.HIL_OVH,
        registry_secret_name=REGISTRY_SECRET_NAME,
    ).add_platform_resources()
    return chart

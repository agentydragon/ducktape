"""Compose the ephemeral Agentplane testing stack directly from cdk8s constructs.

The fixture keeps the service contracts and settings authored in cluster/cdk8s/agentplane.
It supplies bare-k3s dependencies (Secrets, local-path storage) as inputs
and never reads or edits the production generated manifest.

Browsers and services reach the app and Dex by their in-cluster Service names: Pods through the
cluster DNS, the test process on the node through `/etc/hosts` (`stack.py`). In MCP fixtures the app
and standalone edge use loopback HTTP origins for OAuth callbacks, served by port-forwards.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from agentplane_policyset_crds.works.allegedly.agentplane import PolicySetSpecRulesMethods
from cdk8s import ApiObjectMetadata, Chart, Size, Testing
from cdk8s_plus_34 import (
    ContainerProps,
    ContainerResources,
    ContainerSecurityContextProps,
    Cpu,
    CpuResources,
    EnvValue,
    ImagePullPolicy,
    MemoryResources,
    k8s,
)
from constructs import Construct
from more_itertools import one
from pydantic import AnyHttpUrl

from agentplane.subjects import ServiceAccountRef
from agentplane.system_tests import launch_preset, runtime_grant, send_grant
from agentplane.x.k3s_env import fixture_database
from agentplane.x.k3s_env.rendered_fixture import LocalModel, Models, PublicLiteLLM
from cluster.cdk8s import agent_access_profiles as access
from cluster.cdk8s.agentplane import (
    actions,
    app as app_component,
    app_settings,
    command_sandbox,
    dex,
    egress,
    history_service,
    llm_ingress,
    mcp_edge,
    notifications,
    policy_controller,
    policy_controller_rbac,
    runner_gateway,
    runner_gateway_workload,
    runner_sandbox,
    runner_vm,
    sandbox_controller,
    sandbox_pod,
    session_dispatcher,
    testing,
)
from cluster.cdk8s.agentplane.chart import Namespace
from cluster.cdk8s.agentplane.egress_credentials import EgressCredentials
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.model_selections import TESTING_APP_MODELS, HarnessRoutes, model_configs
from cluster.cdk8s.providers.agentplane.policy_set import PolicySet, Rule
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Port, ServiceRef
from model_catalog.catalog import GPT6_LUNA_RESPONSES, HAIKU_API

NAMESPACE = testing.ENV.namespace
RUNNER_GATEWAY = "runner-gateway"
SESSION_DISPATCHER = "session-dispatcher"
RUNNER_GATEWAY_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-runner-gateway:unset"
QUEUE_MIGRATOR_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-runner-gateway-queue-migrate:unset"
DISPATCHER_PORT = 8080
EGRESS_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-egress-proxy:unset"
EGRESS_MIGRATOR_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-egress-history-migrate:unset"
APP_SERVICE = app_component.service(NAMESPACE)
APP_ORIGIN = APP_SERVICE.url
ACTIONS_SERVICE = actions.service(NAMESPACE)
MCP_EDGE_SERVICE = mcp_edge.service(NAMESPACE)
DEX_HOST = dex.service().host
DEX_ISSUER = dex.issuer(f"https://{DEX_HOST}:{dex.HTTPS_PORT.number}")
# Test-only Dex clients and operator in place of testing's, which an operator's deployment uses.
_APP_LOGIN = dex.StaticClient(id="test-app-login", name="Test app login", redirect_uri=f"{APP_ORIGIN}/auth/callback")
_MCP_LINKAGE = dex.StaticClient(
    id="test-mcp-linkage", name="Test MCP linkage", redirect_uri=f"{APP_ORIGIN}/mcp-linkage/callback"
)
_VM_GUEST_BOOT_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-vm-guest-boot:unset"
_VM_GUEST_ROOT_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-vm-guest-root:unset"
# The serial guest/initramfs_init mounts as the root.
_VM_GUEST_ROOT_SERIAL = "guest-root"
# What the app offers when LLM ingress forwards to the public LiteLLM: models the BuildBuddy live-test key's
# lane admits (`cheap_experiments_models`), neither waiting on the shared GPU as testing's Claude model does.
_PUBLIC_LITELLM_MODELS = HarnessRoutes(claude=(HAIKU_API,), codex=(GPT6_LUNA_RESPONSES,))


def add_secret(
    chart: Chart, name: str, data: dict[str, str], *, namespace: str = NAMESPACE, kind: str = "Opaque"
) -> k8s.KubeSecret:
    return k8s.KubeSecret(
        chart,
        f"fixture-secret-{namespace}-{name}",
        metadata=k8s.ObjectMeta(name=name, namespace=namespace),
        type=kind,
        string_data=data,
    )


@dataclass(frozen=True)
class FixtureChart:
    chart: Chart
    app: Construct
    actions: Construct
    mcp_edge: Construct
    app_login: dex.StaticClient
    mcp_linkage: dex.StaticClient


def login_client(app_origin: str) -> dex.StaticClient:
    return replace(_APP_LOGIN, redirect_uri=f"{app_origin}/auth/callback")


def linkage_client(app_origin: str) -> dex.StaticClient:
    return replace(_MCP_LINKAGE, redirect_uri=f"{app_origin}/mcp-linkage/callback")


def environment(*, vm_sandboxes: bool, models: Models, app_origin: str = APP_ORIGIN) -> Environment:
    """Build the common fixture settings; MCP renderers add their servers and Dex resources separately."""
    app_login = login_client(app_origin)
    operator_oidc = testing.operator_oidc(issuer=DEX_ISSUER, app_client_id=app_login.id)
    routes = _PUBLIC_LITELLM_MODELS if isinstance(models, PublicLiteLLM) else TESTING_APP_MODELS
    return replace(
        testing.ENV,
        vm_sandboxes=vm_sandboxes,
        llm_ingress=replace(testing.ENV.llm_ingress, models=model_configs(routes), codex_routes=routes.codex),
        # The scenarios' own preset in place of the deployment's, which an operator uses.
        app_config=testing.ENV.app_config.model_copy(
            update={
                "models": app_settings.model_catalog(routes),
                "connection_service_url": AnyHttpUrl(MCP_EDGE_SERVICE.url),
                "thread_presets": {
                    launch_preset.THREAD_PRESET: launch_preset.thread_preset(model=GPT6_LUNA_RESPONSES.id)
                },
                "sandbox_presets": {
                    launch_preset.SANDBOX_PRESET: launch_preset.sandbox_preset(
                        template=runner_sandbox.NAME, policy_sets=[egress.PACKAGES_POLICY]
                    )
                },
                "action_federation": testing.action_federation(operator_oidc),
            }
        ),
        app=replace(testing.ENV.app, origin=app_origin, oidc_issuer=DEX_ISSUER),
        actions=replace(
            testing.ENV.actions,
            integrated_connection_service=False,
            operator_oidc=operator_oidc,
            mcp_servers={},
            action_groups={"sandbox": testing.ENV.actions.action_groups["sandbox"]},
            oauth_secret_items=("client-secret",),
        ),
        mcp_edge=replace(
            testing.ENV.mcp_edge, operator_oidc=operator_oidc, mintable_accounts=frozenset(), oauth_secret_name=None
        ),
    )


def objects(
    *, ca_cert: str, ca_key: str, system_roots: str, cluster_ca: str, models: Models, vm_sandboxes: bool
) -> list[dict[str, Any]]:
    """Synthesize the common one-node fixture, optionally replacing the production guest for VM tests."""
    env = environment(vm_sandboxes=vm_sandboxes, models=models)
    fixture = build_chart(
        ca_cert=ca_cert, ca_key=ca_key, system_roots=system_roots, cluster_ca=cluster_ca, models=models, env=env
    )
    synthesized = list(Testing.synth(fixture.chart))
    if vm_sandboxes:
        _boot_test_guest(synthesized)
    return synthesized


def build_chart(
    *, ca_cert: str, ca_key: str, system_roots: str, cluster_ca: str, models: Models, env: Environment
) -> FixtureChart:
    """Construct the shared stack and return its chart before synthesis for fixture-specific additions."""
    app_origin = str(env.app.origin)
    app_login = login_client(app_origin)
    mcp_linkage = linkage_client(app_origin)
    sandbox_sidecar_resources = k8s.ResourceRequirements(
        requests={
            "cpu": k8s.Quantity.from_string(Cpu.millis(10).amount),
            "memory": k8s.Quantity.from_string(Size.mebibytes(32).as_string()),
        }
    )
    app = Testing.app()
    chart = Chart(app, "agentplane", disable_resource_name_hashes=True)
    Namespace(chart, "namespace", env)
    fixture_database.add_database(chart, env)
    app_database_url = (
        SecretRef(namespace=NAMESPACE, name="postgres-app").key("async-uri").env_value(chart, "app-database-url")
    )
    actions_database_url = (
        SecretRef(namespace=NAMESPACE, name="postgres-actions").key("uri").env_value(chart, "actions-database-url")
    )
    egress_database_url = (
        SecretRef(namespace=NAMESPACE, name="postgres-egress").key("uri").env_value(chart, "egress-database-url")
    )
    history_database_url = (
        SecretRef(namespace=NAMESPACE, name="postgres-sandbox-service")
        .key("uri")
        .env_value(chart, "history-database-url")
    )
    notifications_database_url = (
        SecretRef(namespace=NAMESPACE, name="postgres-notifications")
        .key("uri")
        .env_value(chart, "notifications-database-url")
    )
    dispatcher_database_url = (
        SecretRef(namespace=NAMESPACE, name="postgres-session-dispatcher")
        .key("uri")
        .env_value(chart, "session-dispatcher-database-url")
    )
    mcp_edge_database_url = (
        SecretRef(namespace=NAMESPACE, name="postgres-mcp-edge").key("uri").env_value(chart, "mcp-edge-database-url")
    )
    llm_ingress.LlmIngress(
        chart,
        "llm-ingress",
        env,
        litellm_url=_litellm_url(models),
        placement=None,
        registry_secret_name=None,
        memory_limit=None,
    )
    agent_api = ServiceRef(name=egress.NAME, port=Port(name="http", number=8082), pods=egress.agent_api(NAMESPACE).pods)
    database_env = {"PGSSLMODE": EnvValue.from_value("disable")}
    egress.Egress(
        chart,
        "egress",
        env,
        database_url=egress_database_url,
        runners=runner_sandbox.pods(env.namespace),
        admin_grpc_callers=[ServiceAccountRef(namespace=NAMESPACE, name=app_component.NAME)],
        agent_api_service=agent_api,
        litellm_host="litellm.allegedly.works" if isinstance(models, PublicLiteLLM) else None,
        litellm_cluster_internal=not isinstance(models, PublicLiteLLM),
        placement=None,
        registry_secret_name=None,
        container_env=database_env,
        memory_limit=None,
        migrate_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
        migrate_memory_limit=None,
        migrate_env=database_env,
    )
    sandbox_pod.add_tool_config(chart, env)
    app_caller = {
        ServiceAccountRef(namespace=NAMESPACE, name=app_component.NAME): app_component.service(NAMESPACE).pods
    }
    notifications_caller = {
        ServiceAccountRef(namespace=NAMESPACE, name=notifications.NAME): notifications.service(NAMESPACE).pods
    }
    dispatcher_callers = app_caller | notifications_caller
    control = runner_gateway_workload.runner_control_service(NAMESPACE, port=Port(name="grpc", number=8080))
    runner_gateway_workload.add_stable_services(chart, env, control=control)
    runner_gateway_workload.RunnerGateway(
        chart,
        "runner-gateway",
        env,
        dispatcher_callers=dispatcher_callers,
        gateway_callers=dispatcher_callers,
        wake_callers=env.wake_caller_accounts,
        database_url=dispatcher_database_url,
        sandbox_controller=(
            ServiceAccountRef(namespace=NAMESPACE, name=sandbox_controller.NAME),
            sandbox_controller.pods(NAMESPACE),
        ),
        control=control,
        migration=ContainerProps(
            name="migrate",
            image=QUEUE_MIGRATOR_IMAGE,
            image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
            env_variables={"DATABASE_URL": dispatcher_database_url, **database_env},
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(25)), memory=MemoryResources(request=Size.mebibytes(64))
            ),
            security_context=ContainerSecurityContextProps(read_only_root_filesystem=False),
        ),
        registry_secret_name=None,
        placement=None,
        container_env=database_env,
        memory_limit=None,
    )
    control_target = f"{control.fqdn}:{control.port.number}"
    history_service.HistoryService(
        chart,
        "history-service",
        env,
        database_url=history_database_url,
        callers=app_caller | notifications_caller,
        placement=None,
        registry_secret_name=None,
        container_env=database_env,
        migrate_env=database_env,
        memory_limit=None,
        migrate_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
        migrate_memory_limit=None,
    )
    app_service = app_component.App(
        chart,
        "app",
        env,
        electric=None,
        database_url=app_database_url,
        placement=None,
        registry_secret_name=None,
        runner_control_target=f"{{sandbox}}-control.{{namespace}}.svc:{control.port.number}",
        memory_limit=None,
        migrate_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
        migrate_memory_limit=None,
    )
    mcp_edge_service = mcp_edge.McpEdge(
        chart,
        "mcp-edge",
        env,
        database_url=mcp_edge_database_url,
        placement=None,
        registry_secret_name=None,
        image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
        migrate_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
    )
    runner_sandbox.RunnerSandbox(
        chart,
        "runner-sandbox",
        env,
        egress_api_service=agent_api,
        storage_class="local-path",
        runner_resources=k8s.ResourceRequirements(
            requests={
                "cpu": k8s.Quantity.from_string(Cpu.millis(500).amount),
                "memory": k8s.Quantity.from_string(Size.gibibytes(1).as_string()),
            }
        ),
        sidecar_resources=sandbox_sidecar_resources,
        image_pull_secret_name=None,
        trust_mounts=sandbox_pod.egress_proxy_mounts(),
        control_service=control,
    )
    sandbox_controller.SandboxController(
        chart,
        "sandbox-controller",
        env,
        gateway=runner_gateway.service(NAMESPACE),
        gateway_audience=runner_gateway.TOKEN_AUDIENCE,
        gateway_control_target=control_target,
        placement=None,
        registry_secret_name=None,
        memory_limit=None,
    )
    action_service = actions.Actions(
        chart,
        "actions",
        env,
        database_url=actions_database_url,
        placement=None,
        registry_secret_name=None,
        image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
        memory_limit=None,
        migrate_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
        migrate_memory_limit=None,
    )
    notifications.Notifications(
        chart,
        "notifications",
        env,
        database_url=notifications_database_url,
        actions=actions.service(NAMESPACE),
        gateway=runner_gateway.service(NAMESPACE),
        gateway_audience=runner_gateway.TOKEN_AUDIENCE,
        gateway_control_target=control_target,
        history=history_service.service(NAMESPACE),
        history_audience=history_service.TOKEN_AUDIENCE,
        dispatcher=runner_gateway_workload.dispatcher_service(NAMESPACE),
        dispatcher_audience=session_dispatcher.TOKEN_AUDIENCE,
        callers=[egress.proxy(NAMESPACE).pods, app_component.service(NAMESPACE).pods],
        placement=None,
        registry_secret_name=None,
        container_env=database_env,
        memory_limit=None,
        migrate_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
        migrate_memory_limit=None,
    )
    command_sandbox.CommandSandbox(
        chart,
        "command-sandbox",
        env,
        sidecar_resources=sandbox_sidecar_resources,
        image_pull_secret_name=None,
        trust_mounts=sandbox_pod.egress_proxy_mounts(),
    )
    # The app and the Actions MCP client read their normal Secret names.
    add_secret(
        chart,
        app_component.oidc_secret(NAMESPACE).name,
        {
            "client-id": _APP_LOGIN.id,
            "client-secret": "test-k3s-app-client-secret",
            "session-secret": "test-k3s-app-session-secret",
        },
    )
    add_secret(
        chart,
        actions.mcp_oauth_secret(NAMESPACE).name,
        {"client-id": _MCP_LINKAGE.id, "client-secret": "test-k3s-actions-client-secret"},
    )
    add_secret(
        chart,
        "agentplane-testing-egress-ca",
        {"tls.crt": ca_cert, "tls.key": ca_key, "ca.crt": ca_cert},
        kind="kubernetes.io/tls",
    )
    k8s.KubeConfigMap(
        chart,
        "fixture-egress-ca",
        metadata=k8s.ObjectMeta(name="agentplane-testing-egress-ca", namespace=NAMESPACE),
        data={"ca-certificates.crt": system_roots + ca_cert},
    )
    k8s.KubeConfigMap(
        chart,
        "fixture-egress-upstream-ca",
        metadata=k8s.ObjectMeta(name="agentplane-testing-egress-upstream-ca", namespace=NAMESPACE),
        data={"ca-certificates.crt": system_roots + cluster_ca},
    )
    key = models.key if isinstance(models, PublicLiteLLM) else "unused-without-litellm"
    add_secret(chart, "litellm-key-cheap-experiments", {"api-key": key})
    EgressCredentials(
        chart, "egress-credentials", namespace=env.egress.credentials_namespace, proxy_namespace=NAMESPACE
    )
    if isinstance(models, PublicLiteLLM):
        add_secret(chart, "litellm-key-cheap-experiments", {"api-key": key}, namespace=env.egress.credentials_namespace)
    tool_config_grant = sandbox_pod.tool_config_grant(NAMESPACE)
    PolicySet(
        chart,
        f"policy-set-{sandbox_pod.TOOL_CONFIG_GRANT}",
        metadata=ApiObjectMetadata(name=sandbox_pod.TOOL_CONFIG_GRANT, namespace=NAMESPACE),
        rules=access.rules_for_grants([tool_config_grant]),
    )
    # A request `basic` refuses: GET on a path it does not name, on a host it does.
    PolicySet(
        chart,
        f"policy-set-{runtime_grant.POLICY_SET}",
        metadata=ApiObjectMetadata(name=runtime_grant.POLICY_SET, namespace=NAMESPACE),
        rules=[
            Rule.egress(
                hosts=[egress.agent_api(NAMESPACE).fqdn],
                cluster_internal=True,
                methods=[PolicySetSpecRulesMethods.GET],
                paths=["/test-runtime-grant"],
            )
        ],
    )
    PolicySet(
        chart,
        f"policy-set-{send_grant.POLICY_SET}",
        metadata=ApiObjectMetadata(name=send_grant.POLICY_SET, namespace=NAMESPACE),
        rules=[Rule.send_message(inbox_tags=[send_grant.INBOX_TAG])],
    )
    controller = policy_controller.PolicyController(
        chart, "policy-controller", env, placement=None, registry_secret_name=None, memory_limit=None
    )
    policy_controller_rbac.PolicyControllerRbac(
        chart, "policy-controller-rbac", env, account=controller.account, assignable_grants=[tool_config_grant]
    )
    return FixtureChart(
        chart=chart,
        app=app_service,
        actions=action_service,
        mcp_edge=mcp_edge_service,
        app_login=app_login,
        mcp_linkage=mcp_linkage,
    )


def _boot_test_guest(objects: list[dict[str, Any]]) -> None:
    """Put the test guest (guest/) in the VM template in place of production's NixOS image, which boots
    too slowly without KVM. KubeVirt boots the guest's kernel directly; its initramfs finds the root disk
    by serial."""
    template = one(
        doc for doc in objects if doc["kind"] == "SandboxTemplate" and doc["metadata"]["name"] == runner_vm.NAME
    )
    spec = template["spec"]["vm"]["template"]["spec"]
    spec["domain"]["firmware"] = {
        "kernelBoot": {
            "kernelArgs": "console=ttyS0",
            "container": {
                "image": _VM_GUEST_BOOT_IMAGE,
                "kernelPath": "/boot/vmlinuz",
                "initrdPath": "/boot/initramfs.cpio",
                "imagePullPolicy": "IfNotPresent",
            },
        }
    }
    root = one(disk for disk in spec["domain"]["devices"]["disks"] if "bootOrder" in disk)
    root["serial"] = _VM_GUEST_ROOT_SERIAL
    volume = one(volume for volume in spec["volumes"] if volume["name"] == root["name"])
    volume["containerDisk"]["image"] = _VM_GUEST_ROOT_IMAGE


def _litellm_url(models: Models) -> str:
    match models:
        case PublicLiteLLM():
            return "https://litellm.allegedly.works"
        case LocalModel(url=url):
            return url
        case None:
            return "http://litellm.litellm.svc.cluster.local:4000"

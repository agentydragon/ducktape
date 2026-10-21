"""Compose the ephemeral Agentplane testing stack directly from cdk8s constructs.

The fixture keeps the service contracts and settings authored in cluster/cdk8s/agentplane.
It supplies bare-k3s dependencies (Secrets, headless routing, local-path storage) as inputs
and never reads or edits the production generated manifest.
"""

from __future__ import annotations

import secrets
import string
from dataclasses import dataclass, replace
from typing import Any

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

from agentplane.acceptance import launch_preset
from agentplane.subjects import ServiceAccountRef
from agentplane.x.k3s_env import fixture_database
from cluster.cdk8s import agent_access_profiles as access
from cluster.cdk8s.agentplane import (
    actions,
    app as app_component,
    command_sandbox,
    egress,
    history_service,
    llm_ingress,
    notifications,
    policy_controller,
    policy_controller_rbac,
    runner_gateway,
    runner_gateway_workload,
    runner_sandbox,
    sandbox_controller,
    sandbox_pod,
    session_dispatcher,
    testing,
)
from cluster.cdk8s.agentplane.chart import Namespace
from cluster.cdk8s.agentplane.egress_credentials import EgressCredentials
from cluster.cdk8s.providers.agentplane.policy_set import PolicySet
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Port, ServiceRef
from model_catalog.catalog import GPT6_LUNA_RESPONSES

NAMESPACE = testing.ENV.namespace
RUNNER_GATEWAY = "runner-gateway"
SESSION_DISPATCHER = "session-dispatcher"
RUNNER_GATEWAY_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-runner-gateway:unset"
QUEUE_MIGRATOR_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-runner-gateway-queue-migrate:unset"
DISPATCHER_PORT = 8080
EGRESS_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-egress-proxy:unset"
EGRESS_MIGRATOR_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-egress-history-migrate:unset"


@dataclass(frozen=True)
class PublicLiteLLM:
    """LLM ingress forwards to the public LiteLLM with this virtual key."""

    key: str


@dataclass(frozen=True)
class LocalModel:
    """LLM ingress forwards, keyless, to a process on the node: the test's scripted model."""

    url: str


# None leaves LLM ingress at the cluster-internal LiteLLM Service, which does not exist here, so no turn can run.
Models = PublicLiteLLM | LocalModel | None


def _password(length: int = 40) -> str:
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(length))


def _secret(
    chart: Chart, name: str, data: dict[str, str], *, namespace: str = NAMESPACE, kind: str = "Opaque"
) -> k8s.KubeSecret:
    return k8s.KubeSecret(
        chart,
        f"fixture-secret-{namespace}-{name}",
        metadata=k8s.ObjectMeta(name=name, namespace=namespace),
        type=kind,
        string_data=data,
    )


def objects(
    *, node_ip: str, ca_cert: str, ca_key: str, system_roots: str, cluster_ca: str, models: Models
) -> list[dict[str, Any]]:
    """Synthesize only the objects the one-node, operator-light k3s fixture deploys."""
    env = replace(
        testing.ENV,
        vm_sandboxes=False,
        # The scenarios' own preset in place of the deployment's, which an operator uses.
        app_config=testing.ENV.app_config.model_copy(
            update={
                "thread_presets": {
                    launch_preset.THREAD_PRESET: launch_preset.thread_preset(model=GPT6_LUNA_RESPONSES.id)
                },
                "sandbox_presets": {
                    launch_preset.SANDBOX_PRESET: launch_preset.sandbox_preset(
                        template=runner_sandbox.NAME, policy_sets=[egress.PACKAGES_POLICY]
                    )
                },
            }
        ),
    )
    if isinstance(models, LocalModel):
        # The scripted model has no catalog entry, as in the runner tests: Codex then uses the metadata it
        # falls back to for an unknown slug and calls the function tools the scripted model answers. A
        # bundled model's entry would switch it to code mode.
        env = replace(env, llm_ingress=replace(env.llm_ingress, codex_routes=()))
    sandbox_api_env = (
        k8s.EnvVar(name="KUBERNETES_SERVICE_HOST", value=node_ip),
        k8s.EnvVar(name="KUBERNETES_SERVICE_PORT", value="6443"),
    )
    sandbox_sidecar_resources = k8s.ResourceRequirements(
        requests={
            "cpu": k8s.Quantity.from_string(Cpu.millis(10).amount),
            "memory": k8s.Quantity.from_string(Size.mebibytes(32).as_string()),
        }
    )
    kube_api_env = {
        "KUBERNETES_SERVICE_HOST": EnvValue.from_value(node_ip),
        "KUBERNETES_SERVICE_PORT": EnvValue.from_value("6443"),
    }
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
    llm_ingress.LlmIngress(
        chart,
        "llm-ingress",
        env,
        litellm_url=_litellm_url(models),
        placement=None,
        registry_secret_name=None,
        service_cluster_ip="None",
        container_env=kube_api_env,
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
        service_cluster_ip="None",
        container_env={**kube_api_env, **database_env},
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
    runner_gateway_workload.add_stable_services(chart, env, cluster_ip="None", control=control)
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
            name="goose-migrate",
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
        container_env={**kube_api_env, **database_env},
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
        service_cluster_ip="None",
        container_env={**kube_api_env, **database_env},
        migrate_env=database_env,
        memory_limit=None,
        migrate_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
        migrate_memory_limit=None,
    )
    app_component.App(
        chart,
        "app",
        env,
        electric=None,
        database_url=app_database_url,
        placement=None,
        registry_secret_name=None,
        service_cluster_ip="None",
        runner_control_target=f"{{sandbox}}-control.{{namespace}}.svc:{control.port.number}",
        container_env=kube_api_env,
        memory_limit=None,
        migrate_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
        migrate_memory_limit=None,
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
        workload_env=sandbox_api_env,
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
        service_cluster_ip="None",
        container_env=kube_api_env,
        memory_limit=None,
    )
    actions.Actions(
        chart,
        "actions",
        env,
        database_url=actions_database_url,
        placement=None,
        registry_secret_name=None,
        service_cluster_ip="None",
        container_env=kube_api_env,
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
        service_cluster_ip="None",
        container_env={**kube_api_env, **database_env},
        memory_limit=None,
        migrate_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
        migrate_memory_limit=None,
    )
    command_sandbox.CommandSandbox(
        chart,
        "command-sandbox",
        env,
        workload_env=sandbox_api_env,
        sidecar_env=sandbox_api_env,
        sidecar_resources=sandbox_sidecar_resources,
        image_pull_secret_name=None,
        trust_mounts=sandbox_pod.egress_proxy_mounts(),
    )
    # The Action Service and app still read their normal Secret names. Dex is deliberately
    # absent; the lifecycle and model smoke authenticate with a service-account token.
    _secret(
        chart,
        "agentplane-oidc",
        {"client-id": "agentplane-testing", "client-secret": _password(), "session-secret": _password(64)},
    )
    _secret(chart, "agentplane-mcp-oauth", {"client-id": "agentplane-testing-mcp", "client-secret": _password()})
    _secret(
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
    _secret(chart, "litellm-key-cheap-experiments", {"api-key": key})
    EgressCredentials(
        chart, "egress-credentials", namespace=env.egress.credentials_namespace, proxy_namespace=NAMESPACE
    )
    if isinstance(models, PublicLiteLLM):
        _secret(chart, "litellm-key-cheap-experiments", {"api-key": key}, namespace=env.egress.credentials_namespace)
    tool_config_grant = sandbox_pod.tool_config_grant(NAMESPACE)
    PolicySet(
        chart,
        f"policy-set-{sandbox_pod.TOOL_CONFIG_GRANT}",
        metadata=ApiObjectMetadata(name=sandbox_pod.TOOL_CONFIG_GRANT, namespace=NAMESPACE),
        rules=access.rules_for_grants([tool_config_grant]),
    )
    controller = policy_controller.PolicyController(
        chart,
        "policy-controller",
        env,
        placement=None,
        registry_secret_name=None,
        service_cluster_ip="None",
        container_env=kube_api_env,
        memory_limit=None,
    )
    policy_controller_rbac.PolicyControllerRbac(
        chart, "policy-controller-rbac", env, account=controller.account, assignable_grants=[tool_config_grant]
    )
    return list(Testing.synth(chart))


def _litellm_url(models: Models) -> str:
    match models:
        case PublicLiteLLM():
            return "https://litellm.allegedly.works"
        case LocalModel(url=url):
            return url
        case None:
            return "http://litellm.litellm.svc.cluster.local:4000"

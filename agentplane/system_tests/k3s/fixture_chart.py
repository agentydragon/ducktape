"""Compose the ephemeral k3s Agentplane stack directly from cdk8s constructs.

The fixture keeps the service contracts and settings authored in cluster/cdk8s/agentplane.
It supplies bare-k3s dependencies (Secrets, local-path storage) as inputs
and never reads or edits the production generated manifest.

Browsers and services reach the app and Dex by their in-cluster Service names: Pods through the
cluster DNS, the test process on the node through `/etc/hosts` (`stack.py`). In MCP fixtures the app
and standalone edge use loopback HTTP origins for OAuth callbacks, served by port-forwards.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from agentplane_egresscredential_crds.works.allegedly.agentplane import (
    EgressCredentialSpecTargets,
    EgressCredentialSpecTargetsMethod,
)
from agentplane_policyset_crds.works.allegedly.agentplane import (
    PolicySetSpecRulesCredentialRef,
    PolicySetSpecRulesMethods,
)
from cdk8s import ApiObjectMetadata, App, Chart, Size, Testing
from cdk8s_plus_34 import ConfigMap, Cpu, DeploymentStrategy, EnvValue, ImagePullPolicy, k8s
from cert_manager_clusterissuer_crds.io.cert_manager import ClusterIssuerSpecCa, ClusterIssuerSpecSelfSigned
from cert_manager_crds.io.cert_manager import CertificateSpecIssuerRef, CertificateSpecUsages
from constructs import Construct
from more_itertools import one
from pydantic import AnyHttpUrl
from trust_manager_crds.io.cert_manager.trust import (
    BundleSpecSources,
    BundleSpecSourcesSecret,
    BundleSpecTarget,
    BundleSpecTargetConfigMap,
    BundleSpecTargetNamespaceSelector,
)

from agentplane.action_service.catalog import ActionGroup
from agentplane.action_service.sandbox.binding import PodExecutorBinding
from agentplane.app.settings import AppSettingsConfig, FrontendConfig, LangfuseSearchSettings, TraceSearchSettings
from agentplane.notification_service.settings import NoticeDebounceSettings
from agentplane.runner.configuration import CodexReadiness
from agentplane.runner.harness import Harness
from agentplane.subjects import ServiceAccountRef
from agentplane.system_tests import launch_preset, probe_credential, runtime_grant, send_grant, vm_configuration
from agentplane.system_tests.k3s import block_storage, dex, fixture_database
from agentplane.system_tests.k3s.namespaces import CREDENTIALS_NAMESPACE, NAMESPACE
from agentplane.system_tests.k3s.rendered_fixture import LocalModel, Models, PublicLiteLLM
from agentplane.x.central_sampler.settings import AuthConfig, ConnectionConfig, ModelConfig, ResponsesConfig
from cluster.cdk8s import agent_access_profiles as access
from cluster.cdk8s.agentplane import (
    actions,
    app as app_component,
    app_settings,
    central_sampler_workload,
    command_sandbox,
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
)
from cluster.cdk8s.agentplane.chart import (
    RUNNER_CERTIFICATE_ISSUER,
    RUNNER_TRUST_BUNDLE,
    RUNNER_TRUST_BUNDLE_KEY,
    Namespace,
    add_runner_certificates,
)
from cluster.cdk8s.agentplane.egress_credentials import EgressCredentials
from cluster.cdk8s.agentplane.environment import (
    ActionsProps,
    AppProps,
    CentralSamplerProps,
    EgressProps,
    Environment,
    LlmIngressProps,
    McpEdgeProps,
    OrchestrationProps,
    ReplicaProfile,
)
from cluster.cdk8s.agentplane.model_selections import HarnessRoutes, model_configs
from cluster.cdk8s.providers.agentplane.egress_credential import EgressCredential, Source
from cluster.cdk8s.providers.agentplane.policy_set import PolicySet, Rule
from cluster.cdk8s.providers.cert_manager.bundle import Bundle
from cluster.cdk8s.providers.cert_manager.certificate import Certificate, CertificatePrivateKey
from cluster.cdk8s.providers.cert_manager.cluster_issuer import ClusterIssuer
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Port, ServiceRef
from model_catalog.catalog import GPT6_LUNA_RESPONSES, HAIKU_API, OLLAMA_QWEN_IQ4XS_128K

RUNNER_GATEWAY = "runner-gateway"
SESSION_DISPATCHER = "session-dispatcher"


# The fixture's one Pod execution environment. Nothing outside the fixture names it -- no live Sandbox
# or preset does -- so unlike staging's it is the fixture's own to rename.
POD_TEMPLATE = "runner"
# The image it runs: //agentplane/system_tests/k3s:runner_image, or its harnesses variant, which
# stack.py imports into containerd under this reference. The runner binary itself still comes from the
# runner disk image, as in staging.
RUNNER_IMAGE = "git.allegedly.works/ducktape-ci/runner"
RUNNER_GATEWAY_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-runner-gateway:unset"
DISPATCHER_PORT = 8080
EGRESS_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-egress-proxy:unset"
APP_SERVICE = app_component.service(NAMESPACE)
APP_ORIGIN = APP_SERVICE.url
ACTIONS_SERVICE = actions.service(NAMESPACE)
MCP_EDGE_SERVICE = mcp_edge.service(NAMESPACE)
MCP_EDGE_HTTP_SERVICE = mcp_edge.http_service(NAMESPACE)
DEX_HOST = dex.service().host
DEX_ISSUER = dex.issuer(f"https://{DEX_HOST}:{dex.HTTPS_PORT.number}")
# The fixture's own Dex clients.
_APP_LOGIN = dex.StaticClient(id="test-app-login", name="Test app login", redirect_uri=f"{APP_ORIGIN}/auth/callback")
_MCP_LINKAGE = dex.StaticClient(
    id="test-mcp-linkage", name="Test MCP linkage", redirect_uri=f"{APP_ORIGIN}/mcp-linkage/callback"
)
_VM_GUEST_BOOT_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-vm-guest-boot:unset"
_VM_GUEST_ROOT_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-vm-guest-root:unset"
# The serial guest/initramfs_init mounts as the root.
_VM_GUEST_ROOT_SERIAL = "guest-root"
# What the app offers when LLM ingress forwards to the scenario's scripted model. Scenarios take each harness's
# first model; a ChatGPT route puts Codex in code mode.
_LOCAL_MODEL_MODELS = HarnessRoutes(claude=(OLLAMA_QWEN_IQ4XS_128K.native,), codex=(GPT6_LUNA_RESPONSES,))
# What the app offers when LLM ingress forwards to the public LiteLLM: models the BuildBuddy live-test key's
# lane admits (`cheap_experiments_models`), neither waiting on the shared GPU.
_PUBLIC_LITELLM_MODELS = HarnessRoutes(claude=(HAIKU_API,), codex=(GPT6_LUNA_RESPONSES,))
_ONE_REPLICA = ReplicaProfile(count=1, strategy=DeploymentStrategy.recreate(), min_ready=None, pdb_min_available=None)
_EGRESS_CA = "systest-egress-ca"
_LITELLM_KEY_SECRET = "litellm-key-cheap-experiments"


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


def environment(
    *, vm_sandboxes: bool, models: Models, app_origin: str = APP_ORIGIN, central_sampler: bool = False
) -> Environment:
    """Build the common fixture settings; MCP renderers add their servers and Dex resources separately."""
    app_login = login_client(app_origin)
    operator_oidc = dex.operator_oidc(issuer=DEX_ISSUER, app_client_id=app_login.id)
    routes = _PUBLIC_LITELLM_MODELS if isinstance(models, PublicLiteLLM) else _LOCAL_MODEL_MODELS
    central_sampler_props = None
    if central_sampler:
        central_sampler_props = CentralSamplerProps(
            models={
                GPT6_LUNA_RESPONSES.id: ModelConfig(
                    connection="llm-ingress",
                    provider_model=GPT6_LUNA_RESPONSES.id,
                    reasoning_efforts={"", *GPT6_LUNA_RESPONSES.reasoning_efforts},
                    api="responses",
                    responses=ResponsesConfig(dialect="lite"),
                    exec_encoding="freeform",
                )
            },
            connections={
                "llm-ingress": ConnectionConfig(
                    base_url=llm_ingress.proxied_url(NAMESPACE),
                    auth=AuthConfig(type="bearer", credential_file=Path("/var/run/secrets/central-sampler/llm")),
                )
            },
            max_live_sessions=2,
            turn_timeout_seconds=60,
            rpc_timeout_seconds=10,
            executor_initialize_timeout_seconds=120 if vm_sandboxes else 30,
            lease_seconds=60,
        )
    if central_sampler:
        routes = replace(routes, central_sampler=(GPT6_LUNA_RESPONSES,))
    catalog = app_settings.model_catalog(routes, central_sampler_enabled=central_sampler)
    if central_sampler:
        # This fixture exercises the pinned Lite host with its official model.
        assert catalog.harnesses[Harness.CENTRAL_SAMPLER] == [GPT6_LUNA_RESPONSES.id]
    return Environment(
        namespace=NAMESPACE,
        description="Agentplane k3s fixture",
        replicas=_ONE_REPLICA,
        # These scenarios check delivery and acknowledgement; worker integration tests cover
        # debounce deadlines. Keep batching enabled without a minute-long wait per notice.
        notifications_debounce=NoticeDebounceSettings(quiet_seconds=1, max_wait_seconds=2),
        app_config=AppSettingsConfig(
            models=catalog,
            default_policy_sets=set(egress.DEFAULT_POLICIES),
            connection_service_url=AnyHttpUrl(MCP_EDGE_SERVICE.url),
            thread_presets={launch_preset.THREAD_PRESET: launch_preset.thread_preset(model=GPT6_LUNA_RESPONSES.id)},
            sandbox_presets={
                launch_preset.SANDBOX_PRESET: launch_preset.sandbox_preset(
                    template=POD_TEMPLATE, policy_sets=[egress.PACKAGES_POLICY]
                )
            },
            egress_admin_url=AnyHttpUrl(egress.admin(NAMESPACE).url),
            action_federation=dex.action_federation(operator_oidc),
            frontend_config=FrontendConfig(
                trace_search=TraceSearchSettings(
                    grafana_base_url=AnyHttpUrl("https://grafana.example.test/"),
                    tempo_datasource_uid="fixture-tempo",
                    deployment_environment="system-test",
                ),
                langfuse_search=LangfuseSearchSettings(
                    base_url=AnyHttpUrl("https://langfuse.example.test/"),
                    project_id="fixture-project",
                    sandbox_namespace_metadata_key="fixture_namespace",
                    sandbox_service_account_metadata_key="fixture_service_account",
                ),
            ),
        ),
        settle_deltas=True,
        central_sampler=central_sampler_props,
        llm_ingress=LlmIngressProps(
            litellm_key_secret_name=_LITELLM_KEY_SECRET, models=model_configs(routes), codex_routes=routes.codex
        ),
        egress=EgressProps(
            ca_secret_name=_EGRESS_CA,
            credentials_namespace=CREDENTIALS_NAMESPACE,
            replicas=_ONE_REPLICA,
            traffic_distribution=None,
        ),
        app=AppProps(origin=app_origin, oidc_issuer=DEX_ISSUER, reach_incluster_authentik=False, runner_zone=None),
        actions=ActionsProps(
            operator_oidc=operator_oidc,
            caller_service_account_namespaces=frozenset({NAMESPACE}),
            mcp_servers={},
            action_groups={
                "sandbox": ActionGroup(
                    title="Sandbox",
                    description="Boxes that run as the calling ServiceAccount, and bounded commands in them.",
                    executor=PodExecutorBinding(
                        kind="pod",
                        description="Created and exec'd by this service, as the caller, in its own namespace.",
                        namespace=NAMESPACE,
                        templates=command_sandbox.TEMPLATES,
                    ),
                )
            },
            oauth_secret_items=("client-secret",),
            orchestration_v1=OrchestrationProps(),
        ),
        mcp_edge=McpEdgeProps(
            replicas=_ONE_REPLICA,
            operator_oidc=operator_oidc,
            caller_service_account_namespaces=frozenset({NAMESPACE}),
            mintable_accounts=frozenset(),
            oauth_secret_name=None,
            hostname=None,
        ),
        vm_sandboxes=vm_sandboxes,
        vm_resources=vm_configuration.RESOURCES if vm_sandboxes else None,
        vm_console_autologin=True,
        wake_caller_accounts=frozenset(
            {
                ServiceAccountRef(namespace=NAMESPACE, name=app_component.NAME),
                ServiceAccountRef(namespace=NAMESPACE, name=notifications.NAME),
            }
        ),
    )


def _add_runner_root(chart: Chart) -> None:
    bootstrap = ClusterIssuer(
        chart,
        "runner-root-bootstrap",
        metadata=ApiObjectMetadata(name="test-runner-root-bootstrap"),
        self_signed=ClusterIssuerSpecSelfSigned(),
    )
    root_secret = "test-runner-root"
    Certificate(
        chart,
        "runner-root",
        metadata=ApiObjectMetadata(name="test-runner-root", namespace="cert-manager"),
        secret_name=root_secret,
        common_name="test-k3s-runner-root",
        is_ca=True,
        duration="87600h",
        renew_before="8760h",
        private_key=CertificatePrivateKey.ecdsa_p256(),
        usages=[CertificateSpecUsages.CERT_SIGN, CertificateSpecUsages.CRL_SIGN],
        issuer_ref=CertificateSpecIssuerRef(name=bootstrap.name, kind="ClusterIssuer"),
    )
    ClusterIssuer(
        chart,
        "runner-internal-issuer",
        # add_runner_certificates uses the production issuer reference.
        metadata=ApiObjectMetadata(name="cluster-internal-ca"),
        ca=ClusterIssuerSpecCa(secret_name=root_secret),
    )
    Bundle(
        chart,
        "runner-trust",
        metadata=ApiObjectMetadata(name=RUNNER_TRUST_BUNDLE),
        sources=[BundleSpecSources(secret=BundleSpecSourcesSecret(name=root_secret, key="ca.crt"))],
        target=BundleSpecTarget(
            config_map=BundleSpecTargetConfigMap(key=RUNNER_TRUST_BUNDLE_KEY),
            namespace_selector=BundleSpecTargetNamespaceSelector(
                match_labels={"kubernetes.io/metadata.name": NAMESPACE}
            ),
        ),
    )


def objects(
    *,
    ca_cert: str,
    ca_key: str,
    system_roots: str,
    cluster_ca: str,
    models: Models,
    vm_sandboxes: bool,
    central_sampler: bool = False,
) -> list[dict[str, Any]]:
    """Synthesize the common one-node fixture, optionally replacing the production guest for VM tests."""
    env = environment(vm_sandboxes=vm_sandboxes, models=models, central_sampler=central_sampler)
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
    app = App()
    chart = Chart(app, "agentplane", disable_resource_name_hashes=True)
    Namespace(chart, "namespace", env)
    _add_runner_root(chart)
    add_runner_certificates(chart, env)
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
    )
    sandbox_pod.add_tool_config(chart, env)
    app_caller = {
        ServiceAccountRef(namespace=NAMESPACE, name=app_component.NAME): app_component.service(NAMESPACE).pods
    }
    notifications_caller = {
        ServiceAccountRef(namespace=NAMESPACE, name=notifications.NAME): notifications.service(NAMESPACE).pods
    }
    orchestration_caller = actions.orchestration_callers(env)
    gateway_callers = app_caller | notifications_caller
    runner_gateway_workload.add_stable_services(chart, env)
    runner_gateway_workload.RunnerGateway(
        chart,
        "runner-gateway",
        env,
        dispatcher_callers=gateway_callers | orchestration_caller,
        gateway_callers=gateway_callers,
        wake_callers=env.wake_caller_accounts | orchestration_caller.keys(),
        database_url=dispatcher_database_url,
        sandbox_controller=(
            ServiceAccountRef(namespace=NAMESPACE, name=sandbox_controller.NAME),
            sandbox_controller.pods(NAMESPACE),
        ),
        registry_secret_name=None,
        placement=None,
        container_env=database_env,
        memory_limit=None,
    )
    control_target = runner_gateway.control_target(NAMESPACE)
    if env.central_sampler is not None:
        sampler_database_url = (
            SecretRef(namespace=NAMESPACE, name="postgres-central-sampler")
            .key("uri")
            .env_value(chart, "central-sampler-database-url")
        )
        central_sampler_workload.CentralSampler(
            chart, "central-sampler", env, database_url=sampler_database_url, placement=None, registry_secret_name=None
        )
    history_service.HistoryService(
        chart,
        "history-service",
        env,
        database_url=history_database_url,
        callers=app_caller | notifications_caller | orchestration_caller,
        placement=None,
        registry_secret_name=None,
        container_env=database_env,
        memory_limit=None,
    )
    app_service = app_component.App(
        chart,
        "app",
        env,
        electric=None,
        database_url=app_database_url,
        placement=None,
        registry_secret_name=None,
        runner_control_target=control_target,
        memory_limit=None,
    )
    mcp_edge_service = mcp_edge.McpEdge(
        chart,
        "mcp-edge",
        env,
        database_url=mcp_edge_database_url,
        placement=None,
        registry_secret_name=None,
        image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
    )
    runner_sandbox.RunnerSandbox(
        chart,
        "runner-sandbox",
        env,
        name=POD_TEMPLATE,
        image=RUNNER_IMAGE,
        egress_api_service=agent_api,
        storage_class="local-path",
        vm_workspace_storage_class=block_storage.STORAGE_CLASS if env.vm_sandboxes else None,
        runner_resources=k8s.ResourceRequirements(
            requests={
                "cpu": k8s.Quantity.from_string(Cpu.millis(500).amount),
                "memory": k8s.Quantity.from_string(Size.gibibytes(1).as_string()),
            }
        ),
        sidecar_resources=sandbox_sidecar_resources,
        image_pull_secret_name=None,
        trust_mounts=sandbox_pod.egress_proxy_mounts(),
        vm_codex_readiness=(
            CodexReadiness(
                exec_server_ready_timeout_ms=60_000,
                exec_server_probe_timeout_ms=5_000,
                exec_server_dependency_timeout_ms=120_000,
            )
            if env.vm_sandboxes
            else None
        ),
        # The hermetic k3s fixture has no Alloy collector behind the telemetry sidecar route.
        disable_otel_sdk=True,
    )
    sandbox_controller.SandboxController(
        chart,
        "sandbox-controller",
        env,
        gateway=runner_gateway.service(NAMESPACE),
        gateway_audience=runner_gateway.TOKEN_AUDIENCE,
        gateway_control_target=control_target,
        runner_certificate_issuer=RUNNER_CERTIFICATE_ISSUER,
        placement=None,
        registry_secret_name=None,
        memory_limit=None,
    )
    action_service = actions.Actions(
        chart,
        "actions",
        env,
        database_url=actions_database_url,
        history=history_service.service(NAMESPACE),
        history_audience=history_service.TOKEN_AUDIENCE,
        dispatcher=runner_gateway_workload.dispatcher_service(NAMESPACE),
        dispatcher_audience=session_dispatcher.TOKEN_AUDIENCE,
        placement=None,
        registry_secret_name=None,
        image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
        memory_limit=None,
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
        callers=[app_component.service(NAMESPACE).pods],
        http_callers=[egress.proxy(NAMESPACE).pods],
        placement=None,
        registry_secret_name=None,
        container_env=database_env,
        memory_limit=None,
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
    add_secret(chart, _EGRESS_CA, {"tls.crt": ca_cert, "tls.key": ca_key, "ca.crt": ca_cert}, kind="kubernetes.io/tls")
    ConfigMap(
        chart,
        "fixture-egress-ca",
        metadata=ApiObjectMetadata(name=_EGRESS_CA, namespace=NAMESPACE),
        data={"ca-certificates.crt": system_roots + ca_cert},
    )
    ConfigMap(
        chart,
        "fixture-egress-upstream-ca",
        metadata=ApiObjectMetadata(name="systest-egress-upstream-ca", namespace=NAMESPACE),
        data={"ca-certificates.crt": system_roots + cluster_ca},
    )
    key = models.key if isinstance(models, PublicLiteLLM) else "unused-without-litellm"
    add_secret(chart, _LITELLM_KEY_SECRET, {"api-key": key})
    EgressCredentials(
        chart, "egress-credentials", namespace=env.egress.credentials_namespace, proxy_namespace=NAMESPACE
    )
    if isinstance(models, PublicLiteLLM):
        add_secret(chart, _LITELLM_KEY_SECRET, {"api-key": key}, namespace=env.egress.credentials_namespace)
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
    _add_probe_credential(chart, credentials_namespace=env.egress.credentials_namespace)
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


def _add_probe_credential(chart: Chart, *, credentials_namespace: str) -> None:
    """The credential `test_egress`'s probe presents in place of a real account's: a token the API server
    authenticates and names back, for an account it authorizes for nothing beyond what every account may."""
    k8s.KubeServiceAccount(
        chart,
        "probe-account",
        metadata=k8s.ObjectMeta(name=probe_credential.SERVICE_ACCOUNT, namespace=credentials_namespace),
    )
    # The token controller fills in `token`.
    k8s.KubeSecret(
        chart,
        "probe-token",
        metadata=k8s.ObjectMeta(
            name=probe_credential.SERVICE_ACCOUNT,
            namespace=credentials_namespace,
            annotations={"kubernetes.io/service-account.name": probe_credential.SERVICE_ACCOUNT},
        ),
        type="kubernetes.io/service-account-token",
    )
    EgressCredential(
        chart,
        "probe-credential",
        metadata=ApiObjectMetadata(name=probe_credential.CREDENTIAL, namespace=NAMESPACE),
        description=(
            f"A token for the ServiceAccount {probe_credential.SERVICE_ACCOUNT}, which holds no RBAC: the "
            "Kubernetes API authenticates it and lets it ask who it is."
        ),
        source=Source.secret_ref(name=probe_credential.SERVICE_ACCOUNT, key="token"),
        targets=[
            EgressCredentialSpecTargets(
                header="Authorization", method=EgressCredentialSpecTargetsMethod.SCHEME_TOKEN, scheme="Bearer"
            )
        ],
    )
    PolicySet(
        chart,
        f"policy-set-{probe_credential.POLICY_SET}",
        metadata=ApiObjectMetadata(name=probe_credential.POLICY_SET, namespace=NAMESPACE),
        rules=[
            Rule.egress(
                hosts=[probe_credential.HOST],
                cluster_internal=True,
                methods=[PolicySetSpecRulesMethods.POST],
                paths=[probe_credential.WHOAMI_PATH],
                credential_ref=PolicySetSpecRulesCredentialRef(name=probe_credential.CREDENTIAL),
            )
        ],
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
            "kernelArgs": "console=ttyS0 net.ifnames=0",
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
    # Keep the smaller executor cgroup local to the OOM scenario; all other VM cases share runner-vm.
    oom_template = deepcopy(template)
    oom_template["metadata"]["name"] = vm_configuration.OOM_TEMPLATE
    oom_config_map_name = f"{vm_configuration.OOM_TEMPLATE}-config"
    oom_config_map = deepcopy(
        one(
            doc
            for doc in objects
            if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == f"{runner_vm.NAME}-config"
        )
    )
    oom_config_map["metadata"]["name"] = oom_config_map_name
    oom_config = json.loads(oom_config_map["data"]["runner.json"])
    oom_config["environment"]["resources"]["executor"]["memory_max_bytes"] = vm_configuration.OOM_EXECUTOR_MEMORY_BYTES
    oom_config_map["data"]["runner.json"] = json.dumps(oom_config, separators=(",", ":"))
    oom_spec = oom_template["spec"]["vm"]["template"]["spec"]
    config_volume = one(
        disk for disk in oom_spec["volumes"] if disk.get("configMap", {}).get("name") == f"{runner_vm.NAME}-config"
    )
    config_volume["configMap"]["name"] = oom_config_map_name
    objects.extend([oom_config_map, oom_template])
    # Run the private supervisor checks once, in an existing harness VM. All templates use
    # the same guest root; the boot argument selects the fixture executable before Runner starts.
    supervision = deepcopy(template)
    supervision["metadata"]["name"] = vm_configuration.SUPERVISION_TEMPLATE
    supervision["spec"]["vm"]["template"]["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"] += (
        " " + vm_configuration.SUPERVISION_KERNEL_ARG
    )
    objects.append(supervision)


def _litellm_url(models: Models) -> str:
    match models:
        case PublicLiteLLM():
            return "https://litellm.allegedly.works"
        case LocalModel(url=url):
            return url

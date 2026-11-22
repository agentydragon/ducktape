"""Compose the ephemeral k3s Agentplane stack directly from cdk8s constructs.

The fixture keeps the service contracts and settings authored in cluster/cdk8s/agentplane.
It supplies bare-k3s dependencies (Secrets, local-path storage) as inputs
and never reads or edits the production generated manifest.

Browsers and services reach the app and Dex by their in-cluster Service names: Pods through the
cluster DNS, the test process on the node through `/etc/hosts` (`stack.py`). In MCP fixtures the app
and standalone edge use loopback HTTP origins for OAuth callbacks, served by port-forwards.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from agentplane_policyset_crds.works.allegedly.agentplane import PolicySet, PolicySetSpec, PolicySetSpecRulesMethods
from cdk8s import ApiObjectMetadata, App, Chart, Size
from cdk8s_plus_34 import (
    ConfigMap,
    ContainerResources,
    Cpu,
    CpuResources,
    Deployment,
    DeploymentStrategy,
    EnvValue,
    ImagePullPolicy,
    MemoryResources,
    PodSecurityContextProps,
    ServiceAccount,
    k8s,
)
from cert_manager_clusterissuer_crds.io.cert_manager import (
    ClusterIssuer,
    ClusterIssuerSpec,
    ClusterIssuerSpecCa,
    ClusterIssuerSpecSelfSigned,
)
from cert_manager_crds.io.cert_manager import (
    Certificate,
    CertificateSpec,
    CertificateSpecIssuerRef,
    CertificateSpecPrivateKeyRotationPolicy,
    CertificateSpecUsages,
)
from constructs import Construct
from kubevirt_virtualmachine_crds.io.kubevirt import (
    VirtualMachineSpecTemplateSpecDomainFirmware,
    VirtualMachineSpecTemplateSpecDomainFirmwareKernelBoot,
    VirtualMachineSpecTemplateSpecDomainFirmwareKernelBootContainer,
)
from pydantic import AnyHttpUrl
from trust_manager_crds.io.cert_manager.trust import (
    Bundle,
    BundleSpec,
    BundleSpecSources,
    BundleSpecSourcesSecret,
    BundleSpecTarget,
    BundleSpecTargetConfigMap,
    BundleSpecTargetNamespaceSelector,
)

from agentplane.action_service.catalog import ActionGroup
from agentplane.action_service.sandbox.binding import PodExecutorBinding
from agentplane.app.oidc import OIDCSettings
from agentplane.app.settings import FrontendConfig, LangfuseSearchSettings, Settings, TraceSearchSettings
from agentplane.env.config import CodexReadiness, Executor, SystemdExecutorOwner
from agentplane.env.runtime.harness import Harness
from agentplane.identity.operator.settings import OperatorOidcSettings
from agentplane.net.peertls.config import (
    APP_URI,
    CENTRAL_SAMPLER_URI,
    EGRESS_PROXY_URI,
    GATEWAY_SESSIONS_URI,
    HISTORY_SERVICE_URI,
    LLM_INGRESS_URI,
    NOTIFICATIONS_URI,
    POLICY_CONTROLLER_URI,
    SANDBOX_CONTROLLER_URI,
    SESSION_DISPATCHER_URI,
    TLS,
)
from agentplane.notification_service.settings import NoticeDebounceSettings

# gazelle:include_dep @pypi//protobuf
from agentplane.proto import session_pb2
from agentplane.sampler.settings import ActionsConfig, AuthConfig, ConnectionConfig, ModelConfig, ResponsesConfig
from agentplane.sandboxes.runtimeconfig.model_catalog import Catalog
from agentplane.subjects import ServiceAccountRef
from agentplane.system_tests.deployment import dex, fixture_database, launch_preset, runtime_grant, send_grant, svid_box
from agentplane.system_tests.deployment.local_models import LOCAL_MODEL_MODELS
from agentplane.system_tests.deployment.rendered_fixture import (
    BrowserService,
    LocalModel,
    Models,
    OperatorTokenIssuer,
    PublicLiteLLM,
    SandboxRuntime,
)
from agentplane.system_tests.driver.native_auth import (
    NATIVE_CLIENT_NAME,
    RUNTIME_PROXY_TEST_READER_CERTIFICATE,
    RUNTIME_PROXY_TEST_READER_SECRET,
    RUNTIME_PROXY_TEST_READER_URI,
)
from agentplane.system_tests.k3s.namespaces import CREDENTIALS_NAMESPACE, NAMESPACE
from agentplane.system_tests.vm import block_storage, configuration as vm_configuration
from cluster.cdk8s import agent_access_profiles as access, public_web_pki
from cluster.cdk8s.agentplane import (
    actions,
    app as app_component,
    app_settings,
    central_sampler_workload,
    command_sandbox,
    egress,
    environment_firecracker,
    environment_guest,
    environment_sandbox,
    environment_service,
    environment_vm,
    history_service,
    launcher_relay,
    llm_ingress,
    mcp_edge,
    notifications,
    policy_controller,
    policy_controller_rbac,
    runner_gateway,
    runner_gateway_workload,
    sandbox_controller,
    sandbox_pod,
    workload_certificate_admission,
    workload_tls,
)
from cluster.cdk8s.agentplane.chart import RUNNER_TRUST_BUNDLE, RUNNER_TRUST_BUNDLE_KEY, Namespace
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
from cluster.cdk8s.agentplane.kubernetes_accounts import KubernetesAccounts
from cluster.cdk8s.agentplane.model_selections import HarnessRoutes, model_configs
from cluster.cdk8s.agentplane.namespaces import kubernetes_accounts_namespace
from cluster.cdk8s.agentplane.service_database import Database
from cluster.cdk8s.cert_manager.cluster_ca import add_workload_ca
from cluster.cdk8s.providers.agentplane.policy_set import Rule
from cluster.cdk8s.providers.cert_manager.certificate import CertificatePrivateKey
from cluster.cdk8s.service_ref import Pods, ServiceRef
from model_catalog.catalog import GPT6_LUNA_RESPONSES, HAIKU_5_5_API, Route


@dataclass(frozen=True)
class AppFixture:
    """The integration App and the fixture access facts it exposes to system-test setup."""

    construct: Construct
    service: ServiceRef
    origin: str
    operator_tokens: OperatorTokenIssuer | None

    @property
    def browser_service(self) -> BrowserService:
        return BrowserService(name=self.service.name, host=self.service.host, fqdn=self.service.fqdn)


# The fixture's one Pod execution environment. Nothing outside the fixture names it -- no live Sandbox
# or preset does -- so unlike staging's it is the fixture's own to rename.
POD_TEMPLATE = "environment-tools"
NATIVE_TEST_CLIENT = NATIVE_CLIENT_NAME
_NATIVE_TEST_CLIENT_LABELS = {"app.kubernetes.io/name": NATIVE_TEST_CLIENT}
# Runner environments use this image; the idle identity Pod reuses PostgreSQL instead.
ENVIRONMENT_TOOLS_IMAGE = "git.allegedly.works/ducktape-ci/environment-tools"
# The native identity Pod reuses the already imported fixture PostgreSQL image; OCI inspection of
# its pinned linux/amd64 manifest confirms /bin -> usr/bin and executable usr/bin/sleep.
NATIVE_TEST_CLIENT_IMAGE = fixture_database.IMAGE
APP_SERVICE = app_component.service(NAMESPACE)
APP_ORIGIN = APP_SERVICE.url
ACTIONS_SERVICE = actions.service(NAMESPACE)
MCP_EDGE_SERVICE = mcp_edge.tls_service(NAMESPACE)
MCP_EDGE_HTTP_SERVICE = mcp_edge.http_service(NAMESPACE)
DEX_HOST = dex.service().host
DEX_ISSUER = dex.issuer(f"https://{DEX_HOST}:{dex.HTTPS_PORT.number}")
# The fixture's own Dex clients.
APP_CLIENT_ID = "test-app-login"
APP_CLIENT_SECRET = "test-k3s-app-client-secret"
_APP_LOGIN = dex.StaticClient(id=APP_CLIENT_ID, name="Test app login", redirect_uri=f"{APP_ORIGIN}/auth/callback")
_MCP_LINKAGE = dex.StaticClient(
    id="test-mcp-linkage", name="Test MCP linkage", redirect_uri=f"{APP_ORIGIN}/mcp-linkage/callback"
)
# Fixtures without Dex: the app's OIDC issuer is this test-only one, whose key the stack pins.
OPERATOR_TOKENS = OperatorTokenIssuer(
    issuer="https://test-operator-issuer.test",
    audience=APP_CLIENT_ID,
    subject="test-operator-subject",
    username="test-operator",
)
_VM_GUEST_BOOT_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-vm-guest-boot:unset"
_VM_GUEST_ROOT_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-vm-guest-root:unset"
# The serial guest/initramfs_init mounts as the root.
_VM_GUEST_ROOT_SERIAL = "guest-root"
_VM_CODEX_READINESS = CodexReadiness(
    exec_server_ready_timeout_ms=60_000, exec_server_probe_timeout_ms=5_000, exec_server_dependency_timeout_ms=120_000
)
_VM_GUEST_KERNEL_ARGS = "console=ttyS0 net.ifnames=0"
# What the app offers when LLM ingress forwards to the scenario's scripted model. Scenarios take each harness's
# first model; a ChatGPT route puts Codex in code mode.
LOCAL_MODEL_ROUTES = HarnessRoutes(
    claude=(LOCAL_MODEL_MODELS[session_pb2.HARNESS_CLAUDE],), codex=(LOCAL_MODEL_MODELS[session_pb2.HARNESS_CODEX],)
)
LOCAL_MODEL_ROUTES_WITH_SAMPLER = replace(LOCAL_MODEL_ROUTES, central_sampler=(GPT6_LUNA_RESPONSES,))
# What the app offers when LLM ingress forwards to the public LiteLLM: models the BuildBuddy live-test key's
# lane admits (`cheap_experiments_models`), neither waiting on the shared GPU.
PUBLIC_LITELLM_ROUTES = HarnessRoutes(claude=(HAIKU_5_5_API,), codex=(GPT6_LUNA_RESPONSES,))
_ONE_REPLICA = ReplicaProfile(count=1, strategy=DeploymentStrategy.recreate(), min_ready=None, pdb_min_available=None)
_EGRESS_CA = "systest-egress-ca"
_LITELLM_KEY_SECRET = "litellm-key-cheap-experiments"
_THREAD_PRESETS = {launch_preset.THREAD_PRESET: launch_preset.thread_preset(model=GPT6_LUNA_RESPONSES.id)}
_SANDBOX_PRESETS = {
    launch_preset.SANDBOX_PRESET: launch_preset.sandbox_preset(
        template=POD_TEMPLATE, policy_sets=[egress.PACKAGES_POLICY]
    )
}


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


APP_CALLERS = {ServiceAccountRef(namespace=NAMESPACE, name=app_component.NAME): APP_SERVICE.pods}
NATIVE_CALLERS = {
    ServiceAccountRef(namespace=NAMESPACE, name=NATIVE_TEST_CLIENT): Pods(
        namespace=NAMESPACE, labels=tuple(_NATIVE_TEST_CLIENT_LABELS.items())
    )
}
BACKEND_DATABASES = (
    Database.ACTIONS,
    Database.EGRESS,
    Database.NOTIFICATIONS,
    Database.HISTORY,
    Database.SESSION_DISPATCHER,
)


def login_client(app_origin: str) -> dex.StaticClient:
    return replace(_APP_LOGIN, redirect_uri=f"{app_origin}/auth/callback")


def linkage_client(app_origin: str) -> dex.StaticClient:
    return replace(_MCP_LINKAGE, redirect_uri=f"{app_origin}/mcp-linkage/callback")


def operator_oidc() -> OperatorOidcSettings:
    return dex.operator_oidc(issuer=DEX_ISSUER, app_client_id=APP_CLIENT_ID)


def runtime_catalog(routes: HarnessRoutes) -> Catalog:
    """Publish the recipe's actual route selections and fixture Pod template."""
    catalog = app_settings.runtime_catalog(
        routes, templates=(POD_TEMPLATE,), central_sampler_enabled=bool(routes.central_sampler)
    )
    if routes.central_sampler:
        # This fixture exercises the pinned Lite host with its official model.
        assert catalog.models.harnesses[Harness.CENTRAL_SAMPLER] == [GPT6_LUNA_RESPONSES.id]
    return catalog


def app_props(
    *, routes: HarnessRoutes, operator_oidc: OperatorOidcSettings, operator_token_key: str | None, app_origin: str
) -> AppProps:
    catalog = app_settings.model_catalog(routes, central_sampler_enabled=bool(routes.central_sampler))
    return AppProps(
        origin=app_origin,
        oidc_issuer=DEX_ISSUER,
        reach_incluster_authentik=False,
        config=Settings(
            models=catalog,
            connection_service_url=AnyHttpUrl(f"https://{MCP_EDGE_SERVICE.authority}"),
            thread_presets=_THREAD_PRESETS,
            sandbox_presets=_SANDBOX_PRESETS,
            egress_admin_url=AnyHttpUrl(egress.admin(NAMESPACE).url),
            egress_admin_tls=workload_tls.service_client_tls(EGRESS_PROXY_URI, client_uri=APP_URI),
            sandbox_namespace=NAMESPACE,
            runner_gateway_target=environment_service.environments_service(NAMESPACE).authority,
            runner_gateway_tls=workload_tls.service_client_tls(SANDBOX_CONTROLLER_URI, client_uri=APP_URI),
            session_dispatcher_target=(runner_gateway.sessions_service(NAMESPACE).authority),
            session_dispatcher_tls=workload_tls.service_client_tls(SESSION_DISPATCHER_URI, client_uri=APP_URI),
            policy_service_target=policy_controller.tls_service(NAMESPACE).authority,
            policy_service_tls=workload_tls.service_client_tls(POLICY_CONTROLLER_URI, client_uri=APP_URI),
            history_service_target=(history_service.service(NAMESPACE).authority),
            history_service_tls=workload_tls.service_client_tls(HISTORY_SERVICE_URI, client_uri=APP_URI),
            notifications_url=AnyHttpUrl(notifications.service(NAMESPACE).url),
            notifications_tls=workload_tls.service_client_tls(NOTIFICATIONS_URI, client_uri=APP_URI),
            listen_address="0.0.0.0:8080",
            log_levels={},
            oidc=OIDCSettings(issuer=DEX_ISSUER, public_base_url=app_origin)
            if operator_token_key is None
            else OIDCSettings(
                issuer=OPERATOR_TOKENS.issuer,
                public_base_url=app_origin,
                static_ed25519_keys=frozenset({operator_token_key}),
            ),
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
    )


def central_sampler_props(models: Sequence[Route]) -> CentralSamplerProps:
    return CentralSamplerProps(
        admin_service_callers=(GATEWAY_SESSIONS_URI,),
        actions=ActionsConfig(
            target=ACTIONS_SERVICE.authority,
            tls=TLS(
                cert_file="/run/agentplane/workload-tls/tls.crt",
                key_file="/run/agentplane/workload-tls/tls.key",
                ca_file=workload_tls.TRUST_BUNDLE_FILE,
            ),
        ),
        replicas=_ONE_REPLICA,
        models={
            model.id: ModelConfig(
                connection="llm-ingress",
                provider_model=model.id,
                response_header_timeout_seconds=360,
                reasoning_efforts={"", *model.reasoning_efforts},
                api="responses",
                responses=ResponsesConfig(dialect="lite"),
                exec_encoding="freeform",
            )
            for model in models
        },
        connections={
            "llm-ingress": ConnectionConfig(
                base_url=f"https://{llm_ingress.service(NAMESPACE).authority}",
                auth=AuthConfig(type="agentplane_ingress"),
                tls=workload_tls.service_client_tls(LLM_INGRESS_URI, client_uri=CENTRAL_SAMPLER_URI),
            )
        },
        max_live_sessions=2,
        # Gateway reconnect has a bounded 120s window; a live turn must outlast it.
        rpc_timeout_seconds=10,
        executor_initialize_timeout_seconds=30,
        lease_seconds=60,
    )


def actions_props(
    *,
    operator_oidc: OperatorOidcSettings | None,
    oauth_secret_items: Sequence[str],
    central_sampler_session_delegation: bool,
) -> ActionsProps:
    return ActionsProps(
        central_sampler_session_delegation=central_sampler_session_delegation,
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
                    templates={
                        **command_sandbox.TEMPLATES,
                        svid_box.TEMPLATE: "The command box on fixture images, with the agentplane CLI.",
                    },
                ),
            )
        },
        oauth_secret_items=oauth_secret_items,
        orchestration_v1=OrchestrationProps(
            presets=app_settings.orchestration_presets(sandbox_presets=_SANDBOX_PRESETS, thread_presets=_THREAD_PRESETS)
        ),
    )


def mcp_edge_props(*, operator_oidc: OperatorOidcSettings, oauth_secret_name: str | None) -> McpEdgeProps:
    return McpEdgeProps(
        replicas=_ONE_REPLICA, operator_oidc=operator_oidc, oauth_secret_name=oauth_secret_name, hostname=None
    )


def environment(
    *,
    runtime: SandboxRuntime,
    routes: HarnessRoutes,
    app: AppProps | None,
    central_sampler: CentralSamplerProps | None,
    actions: ActionsProps,
    mcp_edge: McpEdgeProps,
) -> Environment:
    """Build one frozen fixture Environment from explicit component props and route selections."""
    return Environment(
        namespace=NAMESPACE,
        description="Agentplane k3s fixture",
        replicas=_ONE_REPLICA,
        # These scenarios check delivery and acknowledgement; worker integration tests cover
        # debounce deadlines. Keep batching enabled without a minute-long wait per notice.
        notifications_debounce=NoticeDebounceSettings(quiet_seconds=1, max_wait_seconds=2),
        runtime_catalog=runtime_catalog(routes),
        settle_deltas=True,
        compact_deltas=True,
        central_sampler=central_sampler,
        llm_ingress=LlmIngressProps(
            litellm_key_secret_name=_LITELLM_KEY_SECRET, models=model_configs(routes), codex_routes=routes.codex
        ),
        egress=EgressProps(
            ca_secret_name=_EGRESS_CA,
            credentials_namespace=CREDENTIALS_NAMESPACE,
            replicas=_ONE_REPLICA,
            traffic_distribution=None,
        ),
        app=app,
        actions=actions,
        mcp_edge=mcp_edge,
        vm_sandboxes=runtime is SandboxRuntime.KUBEVIRT,
        firecracker_sandboxes=runtime is SandboxRuntime.FIRECRACKER,
        vm_resources=vm_configuration.RESOURCES if runtime is not SandboxRuntime.POD else None,
        vm_executor=Executor(url="ws://127.0.0.1:4501", owner=SystemdExecutorOwner(), placement="systemd")
        if runtime is not SandboxRuntime.POD
        else None,
        vm_console_autologin=True,
    )


def _add_runner_root(chart: Chart) -> None:
    bootstrap = ClusterIssuer(
        chart,
        "runner-root-bootstrap",
        metadata=ApiObjectMetadata(name="test-runner-root-bootstrap"),
        spec=ClusterIssuerSpec(self_signed=ClusterIssuerSpecSelfSigned()),
    )
    root_secret = "test-runner-root"
    Certificate(
        chart,
        "runner-root",
        metadata=ApiObjectMetadata(name="test-runner-root", namespace="cert-manager"),
        spec=CertificateSpec(
            secret_name=root_secret,
            common_name="test-k3s-runner-root",
            is_ca=True,
            duration="87600h",
            renew_before="8760h",
            # Already-issued intermediates must survive a root reissuance during fixture startup.
            # Cleanup removes the root Secret between fixtures, so each fixture still gets a fresh key.
            private_key=CertificatePrivateKey.ecdsa_p256(CertificateSpecPrivateKeyRotationPolicy.NEVER),
            usages=[CertificateSpecUsages.CERT_SIGN, CertificateSpecUsages.CRL_SIGN],
            issuer_ref=CertificateSpecIssuerRef(name=bootstrap.name, kind="ClusterIssuer"),
        ),
    )
    ClusterIssuer(
        chart,
        "runner-internal-issuer",
        # add_workload_ca signs from the production issuer reference.
        metadata=ApiObjectMetadata(name="cluster-internal-ca"),
        spec=ClusterIssuerSpec(ca=ClusterIssuerSpecCa(secret_name=root_secret)),
    )
    Bundle(
        chart,
        "environment-runtime-trust",
        metadata=ApiObjectMetadata(name=RUNNER_TRUST_BUNDLE),
        spec=BundleSpec(
            sources=[BundleSpecSources(secret=BundleSpecSourcesSecret(name=root_secret, key="ca.crt"))],
            target=BundleSpecTarget(
                config_map=BundleSpecTargetConfigMap(key=RUNNER_TRUST_BUNDLE_KEY),
                namespace_selector=BundleSpecTargetNamespaceSelector(
                    match_labels={"kubernetes.io/metadata.name": NAMESPACE}
                ),
            ),
        ),
    )


def add_native_test_client(chart: Chart, env: Environment) -> None:
    """Give backend-only clients the App role SVID and a legacy History reader account."""
    account = ServiceAccount(
        chart,
        "native-test-client-account",
        metadata=ApiObjectMetadata(name=NATIVE_TEST_CLIENT, namespace=NAMESPACE),
        automount_token=False,
    )
    deployment = Deployment(
        chart,
        "native-test-client-deployment",
        metadata=ApiObjectMetadata(name=NATIVE_TEST_CLIENT, namespace=NAMESPACE, labels=_NATIVE_TEST_CLIENT_LABELS),
        pod_metadata=ApiObjectMetadata(labels=_NATIVE_TEST_CLIENT_LABELS),
        replicas=1,
        strategy=DeploymentStrategy.recreate(),
        service_account=account,
        automount_service_account_token=False,
        security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000),
    )
    container = deployment.add_container(
        name="native-test-client",
        image=NATIVE_TEST_CLIENT_IMAGE,
        image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
        # A bare sleep as PID 1 ignores SIGTERM and consumes the full Pod grace period.
        command=["/bin/sh", "-c", "trap 'exit 0' TERM; sleep infinity & wait"],
        resources=ContainerResources(
            cpu=CpuResources(request=Cpu.millis(10)), memory=MemoryResources(request=Size.mebibytes(16))
        ),
    )

    workload_tls.service_certificate(deployment, env, container=container, name=app_component.NAME, uri=APP_URI)


def configure_port_forward_tls(documents: list[dict[str, Any]]) -> None:
    """Give port-forwarded Python gRPC clients DNS SANs to verify in the k3s-only fixture."""
    names_by_certificate = {
        "agentplane-egress-proxy": f"egress-admin.{NAMESPACE}.svc.cluster.local",
        "sessions-workload": runner_gateway.sessions_service(NAMESPACE).fqdn,
        "history-service-workload": f"history-service.{NAMESPACE}.svc.cluster.local",
        "environments-client-workload": environment_service.environments_service(NAMESPACE).fqdn,
    }
    certificates = {
        document["metadata"]["name"]: document["spec"]
        for document in documents
        if document.get("kind") == "Certificate"
    }
    for certificate_name, dns_name in names_by_certificate.items():
        certificates[certificate_name]["dnsNames"] = [dns_name]
    certificates["sessions-workload"]["ipAddresses"] = ["127.0.0.1"]
    if sampler := certificates.get("central-sampler-workload"):
        sampler["dnsNames"] = [f"central-sampler.{NAMESPACE}.svc.cluster.local"]


def new_chart(env: Environment) -> Chart:
    chart = Chart(App(), "agentplane", disable_resource_name_hashes=True)
    Namespace(chart, "namespace", env)
    return chart


def add_fixture_pki(
    chart: Chart, env: Environment, *, ca_cert: str, ca_key: str, system_roots: str, cluster_ca: str
) -> None:
    _add_runner_root(chart)
    add_workload_ca(chart)
    workload_tls.certificate(
        chart,
        "runtime-proxy-test-reader-certificate",
        env,
        name=RUNTIME_PROXY_TEST_READER_CERTIFICATE,
        secret_name=RUNTIME_PROXY_TEST_READER_SECRET,
        uri=RUNTIME_PROXY_TEST_READER_URI,
    )
    workload_certificate_admission.add_workload_certificate_admission(
        chart,
        workload_credential_issuers={ServiceAccountRef(namespace=NAMESPACE, name=policy_controller.NAME)},
        # The user k3s writes its admin kubeconfig for, which applies these manifests.
        service_requester="system:admin",
        cert_manager=ServiceAccountRef(namespace="cert-manager", name="cert-manager"),
    )
    add_secret(chart, _EGRESS_CA, {"tls.crt": ca_cert, "tls.key": ca_key, "ca.crt": ca_cert}, kind="kubernetes.io/tls")
    Bundle(
        chart,
        "fixture-egress-trust",
        metadata=ApiObjectMetadata(name=_EGRESS_CA),
        # The fixture's SVID root is independent of its Kubernetes API CA.
        spec=BundleSpec(
            sources=[
                BundleSpecSources(in_line=system_roots + ca_cert + cluster_ca),
                BundleSpecSources(secret=BundleSpecSourcesSecret(name="test-runner-root", key="ca.crt")),
            ],
            target=BundleSpecTarget(
                config_map=BundleSpecTargetConfigMap(key="ca-certificates.crt"),
                namespace_selector=BundleSpecTargetNamespaceSelector(
                    match_labels={"kubernetes.io/metadata.name": NAMESPACE}
                ),
            ),
        ),
    )
    ConfigMap(
        chart,
        "fixture-public-web-pki",
        metadata=ApiObjectMetadata(name=public_web_pki.NAME, namespace=NAMESPACE),
        data={public_web_pki.KEY: system_roots},
    )
    ConfigMap(
        chart,
        "fixture-egress-upstream-ca",
        metadata=ApiObjectMetadata(name="systest-egress-upstream-ca", namespace=NAMESPACE),
        data={"ca-certificates.crt": system_roots + cluster_ca},
    )


def add_backend_services(
    chart: Chart,
    env: Environment,
    *,
    models: Models,
    platform_callers: dict[ServiceAccountRef, Pods],
    notification_callers: Sequence[Pods],
) -> Construct:
    # Control-plane `memory_request`s below are the measured peak working set (`memory/peaks.json`
    # in a suite's undeclared outputs) plus about half again, so the node keeps room for Sandboxes.
    llm_ingress.LlmIngress(
        chart,
        "llm-ingress",
        env,
        litellm_url=_litellm_url(models),
        placement=None,
        registry_secret_name=None,
        memory_request=Size.mebibytes(32),
        memory_limit=None,
    )
    database_env = {"PGSSLMODE": EnvValue.from_value("disable")}
    egress.Egress(
        chart,
        "egress",
        env,
        database_url=Database.EGRESS.url(chart, namespace=env.namespace),
        runners=environment_sandbox.pods(env.namespace),
        litellm_host="litellm.allegedly.works" if isinstance(models, PublicLiteLLM) else None,
        litellm_cluster_internal=not isinstance(models, PublicLiteLLM),
        placement=None,
        registry_secret_name=None,
        container_env=database_env,
        memory_request=Size.mebibytes(32),
        memory_limit=None,
        tls_passthrough_services=[
            history_service.service(env.namespace),
            llm_ingress.service(env.namespace),
            notifications.service(env.namespace),
            runner_gateway.sessions_service(env.namespace),
            environment_service.environments_service(env.namespace),
            actions.service(NAMESPACE),
        ],
    )
    notifications_caller = {
        ServiceAccountRef(namespace=NAMESPACE, name=notifications.NAME): notifications.service(NAMESPACE).pods
    }
    orchestration_caller = actions.orchestration_callers(env)
    environment_service_delegation_actor = (
        ServiceAccountRef(namespace=NAMESPACE, name=actions.NAME) if env.actions.orchestration_v1 is not None else None
    )
    environment_service_delegated_methods = (
        ("/agentplane.environments.v1.EnvironmentService/ResumeEnvironment",)
        if environment_service_delegation_actor is not None
        else ()
    )
    gateway_callers = platform_callers | notifications_caller
    runner_gateway_workload.add_service(chart, env)
    runner_gateway_workload.RunnerGateway(
        chart,
        "sessions",
        env,
        dispatcher_callers=gateway_callers | orchestration_caller,
        orchestration_callers=orchestration_caller,
        wake_service_callers=sandbox_controller.WAKE_SERVICE_CALLERS,
        delegation_actor=environment_service_delegation_actor,
        database_url=Database.SESSION_DISPATCHER.url(chart, namespace=env.namespace),
        sessions_image="git.allegedly.works/ducktape-ci/agentplane-sessions:unset",
        registry_secret_name=None,
        placement=None,
        container_env=database_env,
        memory_request=Size.mebibytes(64),
        memory_limit=None,
    )
    history_service.HistoryService(
        chart,
        "history-service",
        env,
        database_url=Database.HISTORY.url(chart, namespace=env.namespace),
        callers=platform_callers | notifications_caller | orchestration_caller,
        delegation_actor=environment_service_delegation_actor,
        placement=None,
        registry_secret_name=None,
        container_env=database_env,
        memory_request=Size.mebibytes(48),
        memory_limit=None,
    )
    sandbox_controller.SandboxController(
        chart,
        "environments",
        env,
        environments_image="git.allegedly.works/ducktape-ci/agentplane-environments:unset",
        authority=sandbox_controller.environment_service_authority(
            env,
            launchers=orchestration_caller,
            operator_service_callers=sandbox_controller.OPERATOR_SERVICE_CALLERS,
            wake_service_callers=sandbox_controller.WAKE_SERVICE_CALLERS,
            delegation_actor=environment_service_delegation_actor,
            extra_delegated_methods=environment_service_delegated_methods,
        ),
        runtime_proxy_readers=(RUNTIME_PROXY_TEST_READER_URI,),
        placement=None,
        registry_secret_name=None,
        memory_request=Size.mebibytes(48),
        memory_limit=None,
    )
    action_service = actions.Actions(
        chart,
        "actions",
        env,
        database_url=Database.ACTIONS.url(chart, namespace=env.namespace),
        history=history_service.service(NAMESPACE),
        dispatcher=runner_gateway.sessions_service(NAMESPACE),
        environment_service_endpoint=(
            environment_service.environments_service(NAMESPACE)
            if environment_service_delegation_actor is not None
            else None
        ),
        placement=None,
        registry_secret_name=None,
        image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
        memory_request=Size.mebibytes(32),
        memory_limit=None,
    )
    notifications.Notifications(
        chart,
        "notifications",
        env,
        database_url=Database.NOTIFICATIONS.url(chart, namespace=env.namespace),
        actions=actions.service(NAMESPACE),
        history=history_service.service(NAMESPACE),
        dispatcher=runner_gateway.sessions_service(NAMESPACE),
        callers=list(notification_callers),
        placement=None,
        registry_secret_name=None,
        container_env=database_env,
        memory_request=Size.mebibytes(32),
        memory_limit=None,
    )
    return action_service


def add_central_sampler(chart: Chart, env: Environment) -> Construct:
    return central_sampler_workload.CentralSampler(
        chart,
        "central-sampler",
        env,
        database_url=Database.CENTRAL_SAMPLER.url(chart, namespace=env.namespace),
        placement=None,
        registry_secret_name=None,
    )


def add_app(chart: Chart, env: Environment) -> AppFixture:
    if env.app is None:
        raise ValueError("the fixture App requires app configuration")
    service = app_component.App(
        chart,
        "app",
        env,
        electric=None,
        database_url=Database.APP.url(chart, namespace=env.namespace, key="async-uri"),
        environment_service_endpoint=environment_service.environments_service(NAMESPACE),
        placement=None,
        registry_secret_name=None,
        memory_request=Size.mebibytes(48),
        memory_limit=None,
    )
    # The app and the Actions MCP client read their normal Secret names.
    add_secret(
        chart,
        app_component.oidc_secret(NAMESPACE).name,
        {
            "client-id": APP_CLIENT_ID,
            "client-secret": APP_CLIENT_SECRET,
            "session-secret": "test-k3s-app-session-secret",
        },
    )
    add_secret(
        chart,
        actions.mcp_oauth_secret(NAMESPACE).name,
        {"client-id": _MCP_LINKAGE.id, "client-secret": "test-k3s-actions-client-secret"},
    )
    # Keep this policy probe off service-owned SVID routes, whose receivers authorize their
    # requests after opaque CONNECT. The app origin is an ordinary intercepted destination.
    PolicySet(
        chart,
        f"policy-set-{runtime_grant.POLICY_SET}",
        metadata=ApiObjectMetadata(
            name=runtime_grant.POLICY_SET,
            namespace=NAMESPACE,
            annotations={runtime_grant.PORT_ANNOTATION: str(APP_SERVICE.port.number)},
        ),
        spec=PolicySetSpec(
            rules=[
                Rule.egress(
                    hosts=[APP_SERVICE.fqdn],
                    cluster_internal=True,
                    methods=[PolicySetSpecRulesMethods.GET],
                    paths=["/test-runtime-grant"],
                )
            ]
        ),
    )
    PolicySet(
        chart,
        f"policy-set-{send_grant.POLICY_SET}",
        metadata=ApiObjectMetadata(name=send_grant.POLICY_SET, namespace=NAMESPACE),
        spec=PolicySetSpec(rules=[Rule.send_message(inbox_tags=[send_grant.INBOX_TAG])]),
    )
    return AppFixture(
        construct=service,
        service=APP_SERVICE,
        origin=env.app.origin,
        operator_tokens=(
            OPERATOR_TOKENS
            if env.app.config.oidc is not None and env.app.config.oidc.static_ed25519_keys is not None
            else None
        ),
    )


def add_mcp_edge(chart: Chart, env: Environment) -> Construct:
    return mcp_edge.McpEdge(
        chart,
        "mcp-edge",
        env,
        database_url=Database.MCP_EDGE.url(chart, namespace=env.namespace),
        placement=None,
        registry_secret_name=None,
        image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
        memory_request=Size.mebibytes(32),
    )


def add_runner_templates(chart: Chart, env: Environment) -> environment_sandbox.EnvironmentSandboxTemplate:
    sandbox_sidecar_resources = k8s.ResourceRequirements(
        requests={
            "cpu": k8s.Quantity.from_string(Cpu.millis(10).amount),
            "memory": k8s.Quantity.from_string(Size.mebibytes(32).as_string()),
        }
    )
    sandbox_pod.add_tool_config(chart, env)
    scope = Construct(chart, "environment-sandbox")
    return environment_sandbox.EnvironmentSandboxTemplate(
        scope,
        "runner-template",
        env,
        name=POD_TEMPLATE,
        image=ENVIRONMENT_TOOLS_IMAGE,
        storage_class="local-path",
        # Sized so the pod suites' concurrent Sandboxes fit the nested node; the limits make an
        # outgrown request an OOM kill rather than a silent squeeze. Measured working sets:
        # `memory/peaks.json` in the suite's undeclared outputs.
        environment_runtime_resources=k8s.ResourceRequirements(
            requests={
                "cpu": k8s.Quantity.from_string(Cpu.millis(500).amount),
                "memory": k8s.Quantity.from_string(Size.mebibytes(256).as_string()),
            },
            limits={"memory": k8s.Quantity.from_string(Size.gibibytes(1).as_string())},
        ),
        executor_resources=k8s.ResourceRequirements(
            requests={
                "cpu": k8s.Quantity.from_string(Cpu.millis(250).amount),
                "memory": k8s.Quantity.from_string(Size.mebibytes(128).as_string()),
            },
            limits={
                "cpu": k8s.Quantity.from_string(Cpu.units(2).amount),
                "memory": k8s.Quantity.from_string(Size.gibibytes(1).as_string()),
            },
        ),
        sidecar_resources=sandbox_sidecar_resources,
        image_pull_secret_name=None,
        trust_mounts=sandbox_pod.egress_proxy_mounts(),
    )


def _fixture_vm_firmware(kernel_args: str) -> VirtualMachineSpecTemplateSpecDomainFirmware:
    return VirtualMachineSpecTemplateSpecDomainFirmware(
        kernel_boot=VirtualMachineSpecTemplateSpecDomainFirmwareKernelBoot(
            kernel_args=kernel_args,
            container=VirtualMachineSpecTemplateSpecDomainFirmwareKernelBootContainer(
                image=_VM_GUEST_BOOT_IMAGE,
                image_pull_policy="IfNotPresent",
                kernel_path="/boot/vmlinuz",
                initrd_path="/boot/initramfs.cpio",
            ),
        )
    )


def add_vm_templates(
    chart: Chart, env: Environment, runner_template: environment_sandbox.EnvironmentSandboxTemplate
) -> None:
    """Compose the ordinary and test-only VM templates from the fixture's runner Pod."""
    scope = chart
    relay_settings = runner_template.add_launcher_relay_settings(name="launcher-relay-egress-sidecar")
    launcher_relay.LauncherRelayPolicy(
        scope, "launcher-relay", namespace=env.namespace, template_name=POD_TEMPLATE, settings_config_map=relay_settings
    )
    state_storage_class = env.vm_storage_class or runner_template.storage_class
    config_data = environment_guest.guest_config_data(
        env,
        gateway=launcher_relay.GUEST_GATEWAY,
        block_devices=("/dev/disk/by-id/virtio-workspace",),
        codex_readiness=_VM_CODEX_READINESS,
    )
    config = ConfigMap(
        scope,
        "environment-vm-config",
        metadata=ApiObjectMetadata(name=f"{environment_vm.NAME}-config", namespace=env.namespace),
        data=config_data,
    )
    labels = environment_sandbox.ENVIRONMENT_RUNTIME_LABELS
    environment_vm.EnvironmentVmTemplate(
        scope,
        "environment-vm-template",
        env,
        template_name=environment_vm.NAME,
        root_image=_VM_GUEST_ROOT_IMAGE,
        root_disk_serial=_VM_GUEST_ROOT_SERIAL,
        firmware=_fixture_vm_firmware(_VM_GUEST_KERNEL_ARGS),
        config_map=config,
        labels=labels,
        state_storage_class=state_storage_class,
        workspace_storage_class=block_storage.STORAGE_CLASS,
    )
    oom_config = ConfigMap(
        scope,
        "oom-vm-config",
        metadata=ApiObjectMetadata(name=f"{vm_configuration.OOM_TEMPLATE}-config", namespace=env.namespace),
        data=config_data,
    )
    environment_vm.EnvironmentVmTemplate(
        scope,
        "oom-vm-template",
        env,
        template_name=vm_configuration.OOM_TEMPLATE,
        root_image=_VM_GUEST_ROOT_IMAGE,
        root_disk_serial=_VM_GUEST_ROOT_SERIAL,
        firmware=_fixture_vm_firmware(f"{_VM_GUEST_KERNEL_ARGS} agentplane.cgroup-observer=1"),
        config_map=oom_config,
        labels=labels,
        state_storage_class=state_storage_class,
        workspace_storage_class=block_storage.STORAGE_CLASS,
    )
    environment_vm.EnvironmentVmTemplate(
        scope,
        "supervision-vm-template",
        env,
        template_name=vm_configuration.SUPERVISION_TEMPLATE,
        root_image=_VM_GUEST_ROOT_IMAGE,
        root_disk_serial=_VM_GUEST_ROOT_SERIAL,
        firmware=_fixture_vm_firmware(f"{_VM_GUEST_KERNEL_ARGS} {vm_configuration.SUPERVISION_KERNEL_ARG}"),
        config_map=config,
        labels=labels,
        state_storage_class=state_storage_class,
        workspace_storage_class=block_storage.STORAGE_CLASS,
    )


def add_firecracker_template(
    chart: Chart, env: Environment, runner_template: environment_sandbox.EnvironmentSandboxTemplate
) -> None:
    """Compose the opt-in Firecracker template beside its runner Pod template."""
    environment_firecracker.EnvironmentFirecrackerTemplate(
        chart,
        "firecracker-template",
        env,
        labels=environment_sandbox.ENVIRONMENT_RUNTIME_LABELS,
        storage_class=env.vm_storage_class or runner_template.storage_class,
        runner_image=environment_vm.RUNNER_DISK_IMAGE,
        image_pull_secret_name=None,
        codex_readiness=_VM_CODEX_READINESS,
    )


def add_command_templates(chart: Chart, env: Environment) -> command_sandbox.CommandSandbox:
    return command_sandbox.CommandSandbox(
        chart,
        "command-sandbox",
        env,
        runner_zone=None,
        image_pull_secret_name=None,
        trust_mounts=sandbox_pod.egress_proxy_mounts(),
    )


def add_policy_controller(chart: Chart, env: Environment) -> None:
    tool_config_grant = sandbox_pod.tool_config_grant(NAMESPACE)
    PolicySet(
        chart,
        f"policy-set-{sandbox_pod.TOOL_CONFIG_GRANT}",
        metadata=ApiObjectMetadata(name=sandbox_pod.TOOL_CONFIG_GRANT, namespace=NAMESPACE),
        spec=PolicySetSpec(rules=access.rules_for_grants([tool_config_grant])),
    )
    controller = policy_controller.PolicyController(
        chart,
        "policy-controller",
        env,
        accounts_namespace=kubernetes_accounts_namespace(NAMESPACE),
        workload_namespaces={NAMESPACE},
        placement=None,
        registry_secret_name=None,
        memory_request=Size.mebibytes(32),
        memory_limit=None,
    )
    policy_controller_rbac.PolicyControllerRbac(
        chart,
        "policy-controller-rbac",
        env,
        account=controller.account,
        assignable_grants=[tool_config_grant],
        accounts_namespace=kubernetes_accounts_namespace(NAMESPACE),
        workload_namespaces={NAMESPACE},
    )


def add_model_credentials(chart: Chart, env: Environment, *, models: Models) -> None:
    key = models.key if isinstance(models, PublicLiteLLM) else "unused-without-litellm"
    add_secret(chart, _LITELLM_KEY_SECRET, {"api-key": key})
    EgressCredentials(
        chart, "egress-credentials", namespace=env.egress.credentials_namespace, proxy_namespace=NAMESPACE
    )
    KubernetesAccounts(
        chart, "kubernetes-accounts", namespace=kubernetes_accounts_namespace(NAMESPACE), proxy_namespace=NAMESPACE
    )
    if isinstance(models, PublicLiteLLM):
        add_secret(chart, _LITELLM_KEY_SECRET, {"api-key": key}, namespace=env.egress.credentials_namespace)


def add_svid_box_template(boxes: command_sandbox.CommandSandbox, env: Environment) -> k8s.KubePodTemplate:
    """Add the command box on the runner's workload image and read-only environment disk."""
    environment = k8s.Volume(
        name="environment",
        image=k8s.ImageVolumeSource(reference=environment_vm.RUNNER_DISK_IMAGE, pull_policy="IfNotPresent"),
    )
    return boxes.add_template(
        "svid-box",
        metadata=k8s.ObjectMeta(name=svid_box.TEMPLATE, namespace=env.namespace),
        workload=command_sandbox.workload(
            ENVIRONMENT_TOOLS_IMAGE,
            command_sandbox.COMMAND_RESOURCES,
            env,
            k8s.VolumeMount(name=environment.name, mount_path="/run/agentplane/environment", read_only=True),
            trust_mounts=sandbox_pod.egress_proxy_mounts(),
            image_pull_policy="IfNotPresent",
        ),
        volumes=[environment],
    )


def _litellm_url(models: Models) -> str:
    match models:
        case PublicLiteLLM():
            return "https://litellm.allegedly.works"
        case LocalModel(url=url):
            return url

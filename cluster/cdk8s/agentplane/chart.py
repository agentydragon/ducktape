"""The objects a deployed Agentplane environment has, as one chart. `staging.chart` calls this and
adds its environment-only objects to what it returns; that is what generate_manifests (writes it to
disk) and the tests (synthesize it in memory via `cdk8s.Testing`) build.

The caller names the Pod execution environment the chart offers and the image it runs
(`runner_name`, `runner_image`), plus any Secret a runner workload reads as a file
(`runner_key_files`). The chart provisions the shared cert-manager Issuer
and Gateway certificate; the Sandbox Controller provisions a unique leaf Certificate for each
workload incarnation.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata, App, Chart
from cert_manager_crds.io.cert_manager import (
    CertificateSpecIssuerRef,
    CertificateSpecPrivateKeyRotationPolicy,
    CertificateSpecUsages,
)
from cert_manager_issuer_crds.io.cert_manager import IssuerSpecCa
from constructs import Construct

from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s import namespaces, node_scheduling
from cluster.cdk8s.agentplane import (
    actions,
    app as app_component,
    database,
    egress,
    electric,
    frontend,
    history_service,
    llm_ingress,
    mcp_edge,
    notifications,
    runner_gateway,
    runner_gateway_workload,
    runner_sandbox,
    sandbox_controller,
    sandbox_pod,
    session_dispatcher,
    telemetry,
)
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.cert_manager import cluster_ca
from cluster.cdk8s.cert_manager.cluster_ca import INTERNAL_ISSUER
from cluster.cdk8s.forgejo_registry.pull_credentials import SECRET_NAME as REGISTRY_SECRET_NAME
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.providers.cert_manager.certificate import Certificate, CertificatePrivateKey
from cluster.cdk8s.providers.cert_manager.issuer import Issuer
from cluster.cdk8s.providers.cilium.network_policy import NetworkPolicy
from cluster.cdk8s.secret_ref import SecretRef

RUNNER_CERTIFICATE_ISSUER = runner_gateway.RUNNER_CERTIFICATE_ISSUER
RUNNER_CA_SECRET = "agentplane-runner-ca"
RUNNER_GATEWAY_CERTIFICATE = "agentplane-runner-gateway"
RUNNER_GATEWAY_SECRET = runner_gateway.GATEWAY_TLS_SECRET
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
            # Runner Pods are Sandbox-owned, not Deployments; nothing here is VPA-managed.
            vpa=Vpa.DISABLED,
            # Standing agent access to metadata and logs (Kyverno-generated bindings).
            # Environment-specific write access is layered on by its chart.
            labels={"name": env.namespace},
            annotations={"description": env.description},
        )


def add_runner_certificates(scope: Construct, env: Environment) -> None:
    """Provision the environment CA, Gateway leaf and namespaced Issuer with cert-manager."""
    Certificate(
        scope,
        "runner-issuer-ca-certificate",
        metadata=ApiObjectMetadata(name=RUNNER_CA_SECRET, namespace=env.namespace),
        secret_name=RUNNER_CA_SECRET,
        is_ca=True,
        common_name=RUNNER_CA_SECRET,
        duration="87600h",
        renew_before="8760h",
        private_key=CertificatePrivateKey.ecdsa_p256(CertificateSpecPrivateKeyRotationPolicy.ALWAYS),
        usages=[CertificateSpecUsages.CERT_SIGN, CertificateSpecUsages.CRL_SIGN],
        issuer_ref=CertificateSpecIssuerRef(name=INTERNAL_ISSUER.name, kind="ClusterIssuer"),
    )
    Issuer(
        scope,
        "runner-issuer",
        metadata=ApiObjectMetadata(name=RUNNER_CERTIFICATE_ISSUER, namespace=env.namespace),
        ca=IssuerSpecCa(secret_name=RUNNER_CA_SECRET),
    )
    Certificate(
        scope,
        "runner-gateway-certificate",
        metadata=ApiObjectMetadata(name=RUNNER_GATEWAY_CERTIFICATE, namespace=env.namespace),
        secret_name=RUNNER_GATEWAY_SECRET,
        uris=["spiffe://agentplane/runner-gateway"],
        duration="24h",
        renew_before="8h",
        private_key=CertificatePrivateKey.ecdsa_p256(CertificateSpecPrivateKeyRotationPolicy.ALWAYS),
        usages=[CertificateSpecUsages.DIGITAL_SIGNATURE, CertificateSpecUsages.CLIENT_AUTH],
        issuer_ref=CertificateSpecIssuerRef(name=RUNNER_CERTIFICATE_ISSUER, kind="Issuer"),
    )


def environment_chart(
    app: App,
    env: Environment,
    *,
    db: database.DbProps,
    runner_name: str,
    runner_image: str,
    runner_key_files: Sequence[runner_sandbox.KeyFile] = (),
) -> Chart:
    """The shared objects. The fleet rules attach as a synth-time validation, so a caller
    may keep adding objects to the returned chart and they are still checked."""
    chart = Chart(app, "agentplane", disable_resource_name_hashes=True)
    Namespace(chart, "namespace", env)
    add_runner_certificates(chart, env)
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
        runners=runner_sandbox.pods(env.namespace),
        admin_grpc_callers=[app_account],
        placement=node_scheduling.HIL_OVH,
        registry_secret_name=REGISTRY_SECRET_NAME,
    ).add_platform_resources()
    sandbox_pod.add_tool_config(chart, env)
    app_caller = {app_account: app_component.service(env.namespace).pods}
    notifications_caller = {
        ServiceAccountRef(namespace=env.namespace, name=notifications.NAME): notifications.service(env.namespace).pods
    }
    runner_gateway_workload.add_stable_services(chart, env)
    history_service.HistoryService(
        chart,
        "history-service",
        env,
        database_url=history_database_url,
        # The app reads Threads; the Notification Service reads command receipts; the orchestration_v1
        # group reads the Sessions it drives.
        callers=app_caller | notifications_caller | actions.orchestration_callers(env),
        placement=node_scheduling.HIL_OVH,
        registry_secret_name=REGISTRY_SECRET_NAME,
    ).add_platform_resources()
    app_component.App(
        chart,
        "app",
        env,
        electric=electric.service(env.namespace),
        database_url=app_database_url,
        placement=node_scheduling.HIL_OVH,
        registry_secret_name=REGISTRY_SECRET_NAME,
        runner_control_target=runner_gateway.control_target(env.namespace),
    ).add_platform_resources()
    frontend.Frontend(chart, "frontend", env).add_platform_resources()
    runner_sandbox.RunnerSandbox(
        chart,
        "runner-sandbox",
        env,
        name=runner_name,
        image=runner_image,
        key_files=runner_key_files,
        image_pull_secret_name=sandbox_pod.SECRET_NAME,
        egress_api_service=egress.agent_api(env.namespace),
    ).add_platform_resources()
    sandbox_controller.SandboxController(
        chart,
        "sandbox-controller",
        env,
        gateway=runner_gateway.service(env.namespace),
        gateway_audience=runner_gateway.TOKEN_AUDIENCE,
        gateway_control_target=runner_gateway.control_target(env.namespace),
        runner_certificate_issuer=RUNNER_CERTIFICATE_ISSUER,
        placement=node_scheduling.HIL_OVH,
        registry_secret_name=REGISTRY_SECRET_NAME,
    ).add_platform_resources()
    actions.Actions(
        chart,
        "actions",
        env,
        database_url=actions_database_url,
        history=history_service.service(env.namespace),
        history_audience=history_service.TOKEN_AUDIENCE,
        dispatcher=session_dispatcher.service(env.namespace),
        dispatcher_audience=session_dispatcher.TOKEN_AUDIENCE,
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
        gateway=runner_gateway.service(env.namespace),
        gateway_audience=runner_gateway.TOKEN_AUDIENCE,
        gateway_control_target=runner_gateway.control_target(env.namespace),
        history=history_service.service(env.namespace),
        history_audience=history_service.TOKEN_AUDIENCE,
        dispatcher=session_dispatcher.service(env.namespace),
        dispatcher_audience=session_dispatcher.TOKEN_AUDIENCE,
        callers=[egress.proxy(env.namespace).pods, app_component.service(env.namespace).pods],
        placement=node_scheduling.HIL_OVH,
        registry_secret_name=REGISTRY_SECRET_NAME,
    ).add_platform_resources()
    return chart

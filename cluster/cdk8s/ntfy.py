"""The self-hosted ntfy Namespace, PostgreSQL cluster, auth ExternalSecret, and app.

The SOPS-managed source credentials stay hand-written beside the generated manifest
(cluster/docs/cdk8s.md). ESO derives bcrypt users and declarative tokens from those source
credentials; CNPG generates the app connection Secret and the Deployment reads both derived
Secrets.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart, Size
from cdk8s_plus_34 import (
    Capability,
    ContainerResources,
    ContainerSecurityContextProps,
    ContainerSecutiryContextCapabilities,
    Cpu,
    CpuResources,
    Deployment,
    EnvValue,
    ImagePullPolicy,
    LabelSelector,
    MemoryResources,
    PodSecurityContextProps,
    Service,
)
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecRefreshPolicy,
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateEngineVersion,
)
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector

from cluster.cdk8s import cnpg, fleet_rules, namespaces, node_scheduling, pod_policy
from cluster.cdk8s.external_secrets.kubernetes_store import ESO_SERVICE_ACCOUNT, cluster_secret_store
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, SecretStoreRef, remote_data
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "ntfy"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/ntfy"
NAMESPACE = NAME
HOSTNAME = "ntfy.allegedly.works"
_IMAGE = "binwiederhier/ntfy:v2.28.0"
SERVICE = ServiceRef(
    name=NAME,
    port=Port(name="http", number=2586),
    pods=Pods(namespace=NAMESPACE, labels=(("app.kubernetes.io/name", NAME),)),
)
DATABASE = cnpg.PostgresRef.generated(name="ntfy-db", namespace=NAMESPACE)
_AUTH_SOURCE_SECRET = "ntfy-credentials"
_AUTH = SecretRef(namespace=NAMESPACE, name="ntfy-auth")
SECRET_STORE = "kubernetes-ntfy-secret-store"


def _secret_store(scope: Construct) -> None:
    """Keep the shared credential source store owned by the ntfy package."""
    cluster_secret_store(
        scope,
        "secret-store",
        metadata=ApiObjectMetadata(
            name=SECRET_STORE,
            annotations={"description": "Scoped ESO access to ntfy credentials for ntfy, Flux, and Alertmanager."},
        ),
        namespaces=[NAMESPACE, "flux-system", "monitoring"],
        remote_namespace=NAMESPACE,
        service_account=ESO_SERVICE_ACCOUNT,
    )


def _auth_external_secret(scope: Construct) -> None:
    """Derive stable-on-change ntfy auth inputs from the SOPS source Secret."""
    ExternalSecret(
        scope,
        "auth-external-secret",
        metadata=ApiObjectMetadata(
            name=_AUTH.name,
            namespace=NAMESPACE,
            annotations={
                "description": "Derives ntfy bcrypt users and declarative tokens from SOPS values.",
                # Sprig bcrypt uses a fresh salt on every render. Keep this ExternalSecret
                # OnChange and bump the generation on deliberate credential rotation instead
                # of regenerating hashes on every ESO refresh.
                "ntfy.ducktape.io/auth-generation": "1",
            },
        ),
        refresh_policy=ExternalSecretSpecRefreshPolicy.ON_CHANGE,
        secret_store_ref=SecretStoreRef.cluster(SECRET_STORE),
        data=[
            remote_data(_AUTH_SOURCE_SECRET, "alertmanager-password", secret_key="alertmanager_password"),
            remote_data(_AUTH_SOURCE_SECRET, "alertmanager-token", secret_key="alertmanager_token"),
            remote_data(_AUTH_SOURCE_SECRET, "android-password", secret_key="android_password"),
            remote_data(_AUTH_SOURCE_SECRET, "android-token", secret_key="android_token"),
        ],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
        template=ExternalSecretSpecTargetTemplate(
            engine_version=ExternalSecretSpecTargetTemplateEngineVersion.V2,
            type="Opaque",
            data={
                "NTFY_AUTH_USERS": (
                    '{{ htpasswd "alertmanager" .alertmanager_password "bcrypt" }}:user,'
                    '{{ htpasswd "android" .android_password "bcrypt" }}:user'
                ),
                "NTFY_AUTH_TOKENS": (
                    "alertmanager:{{ .alertmanager_token }}:alertmanager,android:{{ .android_token }}:android"
                ),
            },
        ),
    )


def _alertmanager_webhook_secret(scope: Construct) -> None:
    """Publish the ntfy bearer credential as Alertmanager's webhook Secret."""
    ExternalSecret(
        scope,
        "alertmanager-webhook-external-secret",
        metadata=ApiObjectMetadata(
            name="alertmanager-ntfy-webhook",
            namespace="monitoring",
            annotations={
                "description": "Alertmanager bearer credential for the self-hosted ntfy instance",
                "ntfy.ducktape.io/auth-generation": "1",
            },
        ),
        refresh_policy=ExternalSecretSpecRefreshPolicy.ON_CHANGE,
        secret_store_ref=SecretStoreRef.cluster(SECRET_STORE),
        data=[remote_data(_AUTH_SOURCE_SECRET, "alertmanager-token", secret_key="alertmanager_token")],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
        template=ExternalSecretSpecTargetTemplate(
            engine_version=ExternalSecretSpecTargetTemplateEngineVersion.V2,
            type="Opaque",
            data={"address": f"https://{HOSTNAME}/alerts", "token": "{{ .alertmanager_token }}"},
        ),
    )


def _database(scope: Construct) -> None:
    cnpg.cluster(
        scope,
        "database",
        ref=DATABASE,
        placement=node_scheduling.HIL_OVH,
        storage_class="local-path-ovh-hdd",
        size="2Gi",
        initdb=cnpg.same_owner_initdb(NAME),
        wal_archive=False,
    )


class Ntfy(Construct):
    """The complete generated ntfy workload and its namespace-local dependencies."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        namespaces.namespace(
            self, "namespace", name=NAMESPACE, vpa=Vpa.RECOMMEND, labels={"app.kubernetes.io/name": NAMESPACE}
        )
        _secret_store(self)
        _database(self)
        _auth_external_secret(self)
        _alertmanager_webhook_secret(self)
        deployment = self._add_deployment()
        self._add_service(deployment)
        https_route(
            self,
            "httproute",
            metadata=ApiObjectMetadata(name="ntfy", namespace=NAMESPACE),
            hostnames=[HOSTNAME],
            backend=SERVICE,
        )
        self._add_service_monitor()

    def _add_deployment(self) -> Deployment:
        deployment = Deployment(
            self,
            "deployment",
            metadata=ApiObjectMetadata(
                name=NAME,
                namespace=NAMESPACE,
                labels=SERVICE.pods.selector,
                annotations={"description": "Single ntfy server backed by the two-instance ntfy PostgreSQL cluster."},
            ),
            pod_metadata=ApiObjectMetadata(labels=SERVICE.pods.selector),
            replicas=1,
            select=False,
            automount_service_account_token=False,
            enable_service_links=False,
            security_context=PodSecurityContextProps(ensure_non_root=True, user=65532, group=65532),
        )
        deployment.select(LabelSelector.of(labels=SERVICE.pods.selector))

        deployment.add_container(
            name=NAME,
            image=_IMAGE,
            image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
            args=["serve"],
            ports=[SERVICE.port.container_port()],
            env_variables={
                "NTFY_BASE_URL": EnvValue.from_value(f"https://{HOSTNAME}"),
                # Run above Linux's privileged-port range with all capabilities dropped.
                "NTFY_LISTEN_HTTP": EnvValue.from_value(f":{SERVICE.pod_port}"),
                "NTFY_AUTH_DEFAULT_ACCESS": EnvValue.from_value("deny-all"),
                "NTFY_AUTH_ACCESS": EnvValue.from_value("alertmanager:alerts:wo,android:alerts:ro"),
                "NTFY_BEHIND_PROXY": EnvValue.from_value("true"),
                "NTFY_ENABLE_METRICS": EnvValue.from_value("true"),
                "NTFY_DATABASE_URL": DATABASE.app_secret.key("uri").env_value(self, "database-url-ref"),
                "NTFY_AUTH_USERS": _AUTH.key("NTFY_AUTH_USERS").env_value(self, "auth-users-ref"),
                "NTFY_AUTH_TOKENS": _AUTH.key("NTFY_AUTH_TOKENS").env_value(self, "auth-tokens-ref"),
            },
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(20), limit=Cpu.millis(200)),
                memory=MemoryResources(request=Size.mebibytes(64), limit=Size.mebibytes(256)),
            ),
            readiness=http_probe("/v1/health", port=SERVICE.pod_port, initial_delay_seconds=10, failure_threshold=12),
            liveness=http_probe("/v1/health", port=SERVICE.pod_port, initial_delay_seconds=20, period_seconds=20),
            security_context=ContainerSecurityContextProps(
                capabilities=ContainerSecutiryContextCapabilities(drop=[Capability.ALL]),
                user=65532,
                group=65532,
                read_only_root_filesystem=True,
            ),
        )
        pod_policy.harden(deployment)
        return deployment

    def _add_service(self, deployment: Deployment) -> None:
        Service(
            self,
            "service",
            metadata=ApiObjectMetadata(name=SERVICE.name, namespace=NAMESPACE, labels=SERVICE.labels),
            selector=deployment,
            ports=[SERVICE.port.service_port()],
        )

    def _add_service_monitor(self) -> None:
        ServiceMonitor(
            self,
            "servicemonitor",
            metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE, labels=SERVICE.labels),
            selector=ServiceMonitorSpecSelector(match_labels=SERVICE.labels),
            endpoints=[Endpoint.plain(port=SERVICE.port.name)],
        )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    Ntfy(chart, NAME)
    fleet_rules.add_fleet_rules(chart)
    return chart


def ntfy(
    flux_chart: Chart,
    directory: RenderedDirectory,
    cnpg: Kustomization,
    external_secrets_operator: Kustomization,
    monitoring_crds: Kustomization,
    kyverno: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        NAME,
        directory,
        description="Self-hosted ntfy for Android and cluster alert notifications.",
        timeout="10m",
        depends_on=flux_kustomization_depends_on_many(
            cnpg,
            external_secrets_operator,
            monitoring_crds,
            # Kyverno's failurePolicy: Fail webhooks admit the Deployment and HTTPRoute.
            kyverno,
        ),
    )

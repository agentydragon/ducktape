"""The self-hosted ntfy Namespace, PostgreSQL cluster, auth ExternalSecret, and app.

The SOPS-managed source credentials stay hand-written beside the generated manifest
(cluster/docs/cdk8s.md). ESO derives bcrypt users and declarative tokens from those source
credentials; CNPG generates the app connection Secret and the Deployment reads both derived
Secrets.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObject, ApiObjectMetadata, App, Chart, Size
from cdk8s_plus_34 import (
    Capability,
    ContainerPort,
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
    Namespace,
    PodSecurityContextProps,
    Protocol,
    Secret,
    SecretValue,
    Service,
    ServicePort,
)
from cnpg_cluster_crds.io.cnpg.postgresql import (
    Cluster,
    ClusterSpec,
    ClusterSpecAffinity,
    ClusterSpecBootstrap,
    ClusterSpecBootstrapInitdb,
    ClusterSpecMonitoring,
    ClusterSpecProbes,
    ClusterSpecProbesLiveness,
    ClusterSpecProbesLivenessIsolationCheck,
    ClusterSpecStorage,
)
from constructs import Construct
from external_secret_store_crds.io.external_secrets import (
    ClusterSecretStore,
    ClusterSecretStoreSpec,
    ClusterSecretStoreSpecConditions,
    ClusterSecretStoreSpecProvider,
    ClusterSecretStoreSpecProviderKubernetes,
    ClusterSecretStoreSpecProviderKubernetesAuth,
    ClusterSecretStoreSpecProviderKubernetesAuthServiceAccount,
    ClusterSecretStoreSpecProviderKubernetesServer,
    ClusterSecretStoreSpecProviderKubernetesServerCaProvider,
    ClusterSecretStoreSpecProviderKubernetesServerCaProviderType,
)
from external_secrets_crds.io.external_secrets import (
    ExternalSecret,
    ExternalSecretSpec,
    ExternalSecretSpecData,
    ExternalSecretSpecDataRemoteRef,
    ExternalSecretSpecRefreshPolicy,
    ExternalSecretSpecSecretStoreRef,
    ExternalSecretSpecSecretStoreRefKind,
    ExternalSecretSpecTarget,
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateEngineVersion,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDependsOn,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)
from prometheus_operator_crds.com.coreos.monitoring import (
    ServiceMonitor,
    ServiceMonitorSpec,
    ServiceMonitorSpecEndpoints,
    ServiceMonitorSpecSelector,
)

from cluster.cdk8s import fleet_rules
from cluster.cdk8s.agentplane import node_scheduling
from cluster.cdk8s.cnpg import OFF_CONTROL_PLANE_NODE_AFFINITY
from cluster.cdk8s.flux import NAMESPACE as FLUX_NAMESPACE, flux_kustomization, health_checks, kustomize_kustomization
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import sops_decryption, write_yaml
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.pod_spec_patches import runtime_default_seccomp_patch
from cluster.cdk8s.probes import http_probe

NAME = "ntfy"
OUTPUT_DIR = "cluster/k8s/ntfy"
NAMESPACE = NAME
HOSTNAME = "ntfy.allegedly.works"
PORT = 2586
_IMAGE = "binwiederhier/ntfy:v2.28.0"
_LABELS = {"app.kubernetes.io/name": NAME}
_DATABASE_CLUSTER = "ntfy-db"
_DATABASE_APP_SECRET = f"{_DATABASE_CLUSTER}-app"
_AUTH_SOURCE_SECRET = "ntfy-credentials"
_AUTH_SECRET = "ntfy-auth"
_SECRET_STORE = "kubernetes-ntfy-secret-store"


def _secret_env(scope: Construct, id: str, *, name: str, key: str) -> EnvValue:
    return EnvValue.from_secret_value(SecretValue(secret=Secret.from_secret_name(scope, f"{id}-ref", name), key=key))


def _secret_store(scope: Construct) -> None:
    """Keep the shared credential source store owned by the ntfy package."""
    ClusterSecretStore(
        scope,
        "secret-store",
        metadata=ApiObjectMetadata(
            name=_SECRET_STORE,
            annotations={"description": "Scoped ESO access to ntfy credentials for ntfy, Flux, and Alertmanager."},
        ),
        spec=ClusterSecretStoreSpec(
            conditions=[ClusterSecretStoreSpecConditions(namespaces=[NAMESPACE, "flux-system", "monitoring"])],
            provider=ClusterSecretStoreSpecProvider(
                kubernetes=ClusterSecretStoreSpecProviderKubernetes(
                    auth=ClusterSecretStoreSpecProviderKubernetesAuth(
                        service_account=ClusterSecretStoreSpecProviderKubernetesAuthServiceAccount(
                            name="external-secrets", namespace="external-secrets-system"
                        )
                    ),
                    remote_namespace=NAMESPACE,
                    server=ClusterSecretStoreSpecProviderKubernetesServer(
                        ca_provider=ClusterSecretStoreSpecProviderKubernetesServerCaProvider(
                            type=ClusterSecretStoreSpecProviderKubernetesServerCaProviderType.CONFIG_MAP,
                            name="kube-root-ca.crt",
                            key="ca.crt",
                            namespace="default",
                        )
                    ),
                )
            ),
        ),
    )


def _auth_external_secret(scope: Construct) -> None:
    """Derive stable-on-change ntfy auth inputs from the SOPS source Secret."""
    ExternalSecret(
        scope,
        "auth-external-secret",
        metadata=metadata(
            _AUTH_SECRET,
            NAMESPACE,
            annotations={
                "description": "Derives ntfy bcrypt users and declarative tokens from SOPS values.",
                # Sprig bcrypt uses a fresh salt on every render. Keep this ExternalSecret
                # OnChange and bump the generation on deliberate credential rotation instead
                # of regenerating hashes on every ESO refresh.
                "ntfy.ducktape.io/auth-generation": "1",
            },
        ),
        spec=ExternalSecretSpec(
            refresh_policy=ExternalSecretSpecRefreshPolicy.ON_CHANGE,
            secret_store_ref=ExternalSecretSpecSecretStoreRef(
                name=_SECRET_STORE, kind=ExternalSecretSpecSecretStoreRefKind.CLUSTER_SECRET_STORE
            ),
            target=ExternalSecretSpecTarget(
                name=_AUTH_SECRET,
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
            ),
            data=[
                ExternalSecretSpecData(
                    secret_key="alertmanager_password",
                    remote_ref=ExternalSecretSpecDataRemoteRef(
                        key=_AUTH_SOURCE_SECRET, property="alertmanager-password"
                    ),
                ),
                ExternalSecretSpecData(
                    secret_key="alertmanager_token",
                    remote_ref=ExternalSecretSpecDataRemoteRef(key=_AUTH_SOURCE_SECRET, property="alertmanager-token"),
                ),
                ExternalSecretSpecData(
                    secret_key="android_password",
                    remote_ref=ExternalSecretSpecDataRemoteRef(key=_AUTH_SOURCE_SECRET, property="android-password"),
                ),
                ExternalSecretSpecData(
                    secret_key="android_token",
                    remote_ref=ExternalSecretSpecDataRemoteRef(key=_AUTH_SOURCE_SECRET, property="android-token"),
                ),
            ],
        ),
    )


def _alertmanager_webhook_secret(scope: Construct) -> None:
    """Publish the ntfy bearer credential as Alertmanager's webhook Secret."""
    ExternalSecret(
        scope,
        "alertmanager-webhook-external-secret",
        metadata=metadata(
            "alertmanager-ntfy-webhook",
            "monitoring",
            annotations={
                "description": "Alertmanager bearer credential for the self-hosted ntfy instance",
                "ntfy.ducktape.io/auth-generation": "1",
            },
        ),
        spec=ExternalSecretSpec(
            refresh_policy=ExternalSecretSpecRefreshPolicy.ON_CHANGE,
            secret_store_ref=ExternalSecretSpecSecretStoreRef(
                name=_SECRET_STORE, kind=ExternalSecretSpecSecretStoreRefKind.CLUSTER_SECRET_STORE
            ),
            target=ExternalSecretSpecTarget(
                name="alertmanager-ntfy-webhook",
                creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
                deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
                template=ExternalSecretSpecTargetTemplate(
                    engine_version=ExternalSecretSpecTargetTemplateEngineVersion.V2,
                    type="Opaque",
                    data={"address": f"https://{HOSTNAME}/alerts", "token": "{{ .alertmanager_token }}"},
                ),
            ),
            data=[
                ExternalSecretSpecData(
                    secret_key="alertmanager_token",
                    remote_ref=ExternalSecretSpecDataRemoteRef(key=_AUTH_SOURCE_SECRET, property="alertmanager-token"),
                )
            ],
        ),
    )


def _database(scope: Construct) -> None:
    Cluster(
        scope,
        "database",
        metadata=metadata(_DATABASE_CLUSTER, NAMESPACE),
        spec=ClusterSpec(
            instances=2,
            image_name="ghcr.io/cloudnative-pg/postgresql:18.1-system-trixie",
            probes=ClusterSpecProbes(
                liveness=ClusterSpecProbesLiveness(
                    isolation_check=ClusterSpecProbesLivenessIsolationCheck(enabled=False)
                )
            ),
            affinity=ClusterSpecAffinity(
                enable_pod_anti_affinity=True,
                pod_anti_affinity_type="preferred",
                node_selector={"topology.kubernetes.io/zone": node_scheduling.ZONE},
                topology_key="kubernetes.io/hostname",
                node_affinity=OFF_CONTROL_PLANE_NODE_AFFINITY,
            ),
            storage=ClusterSpecStorage(storage_class="local-path-ovh-hdd", size="2Gi"),
            monitoring=ClusterSpecMonitoring(enable_pod_monitor=True),
            bootstrap=ClusterSpecBootstrap(initdb=ClusterSpecBootstrapInitdb(database=NAME, owner=NAME)),
        ),
    )


class Ntfy(Construct):
    """The complete generated ntfy workload and its namespace-local dependencies."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        Namespace(
            self,
            "namespace",
            metadata=ApiObjectMetadata(
                name=NAMESPACE, labels={"app.kubernetes.io/name": NAMESPACE, "goldilocks.fairwinds.com/enabled": "true"}
            ),
        )
        _secret_store(self)
        _database(self)
        _auth_external_secret(self)
        _alertmanager_webhook_secret(self)
        deployment = self._add_deployment()
        self._add_service(deployment)
        https_route(self, "httproute", metadata=metadata("ntfy", NAMESPACE), hostname=HOSTNAME, backend=NAME, port=PORT)
        self._add_service_monitor()

    def _add_deployment(self) -> Deployment:
        deployment = Deployment(
            self,
            "deployment",
            metadata=metadata(
                NAME,
                NAMESPACE,
                labels=_LABELS,
                annotations={
                    "description": "Single ntfy server backed by the two-instance ntfy PostgreSQL cluster.",
                    "reloader.stakater.com/auto": "true",
                },
            ),
            pod_metadata=ApiObjectMetadata(labels=_LABELS),
            replicas=1,
            select=False,
            automount_service_account_token=False,
            enable_service_links=False,
            security_context=PodSecurityContextProps(ensure_non_root=True, user=65532, group=65532),
        )
        deployment.select(LabelSelector.of(labels=_LABELS))

        deployment.add_container(
            name=NAME,
            image=_IMAGE,
            image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
            args=["serve"],
            ports=[ContainerPort(name="http", number=PORT, protocol=Protocol.TCP)],
            env_variables={
                "NTFY_BASE_URL": EnvValue.from_value(f"https://{HOSTNAME}"),
                # Run above Linux's privileged-port range with all capabilities dropped.
                "NTFY_LISTEN_HTTP": EnvValue.from_value(f":{PORT}"),
                "NTFY_AUTH_DEFAULT_ACCESS": EnvValue.from_value("deny-all"),
                "NTFY_AUTH_ACCESS": EnvValue.from_value("alertmanager:alerts:wo,android:alerts:ro"),
                "NTFY_BEHIND_PROXY": EnvValue.from_value("true"),
                "NTFY_ENABLE_METRICS": EnvValue.from_value("true"),
                "NTFY_DATABASE_URL": _secret_env(self, "database-url", name=_DATABASE_APP_SECRET, key="uri"),
                "NTFY_AUTH_USERS": _secret_env(self, "auth-users", name=_AUTH_SECRET, key="NTFY_AUTH_USERS"),
                "NTFY_AUTH_TOKENS": _secret_env(self, "auth-tokens", name=_AUTH_SECRET, key="NTFY_AUTH_TOKENS"),
            },
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(20), limit=Cpu.millis(200)),
                memory=MemoryResources(request=Size.mebibytes(64), limit=Size.mebibytes(256)),
            ),
            readiness=http_probe("/v1/health", port=PORT, initial_delay_seconds=10, failure_threshold=12),
            liveness=http_probe("/v1/health", port=PORT, initial_delay_seconds=20, period_seconds=20),
            security_context=ContainerSecurityContextProps(
                capabilities=ContainerSecutiryContextCapabilities(drop=[Capability.ALL]),
                user=65532,
                group=65532,
                read_only_root_filesystem=True,
            ),
        )
        ApiObject.of(deployment).add_json_patch(runtime_default_seccomp_patch())
        return deployment

    def _add_service(self, deployment: Deployment) -> None:
        Service(
            self,
            "service",
            metadata=metadata(NAME, NAMESPACE, labels=_LABELS),
            selector=deployment,
            ports=[ServicePort(name="http", port=PORT, target_port=PORT, protocol=Protocol.TCP)],
        )

    def _add_service_monitor(self) -> None:
        ServiceMonitor(
            self,
            "servicemonitor",
            metadata=metadata(NAME, NAMESPACE, labels={"release": "kube-prometheus-stack", **_LABELS}),
            spec=ServiceMonitorSpec(
                selector=ServiceMonitorSpecSelector(match_labels=_LABELS),
                endpoints=[ServiceMonitorSpecEndpoints(port="http", path="/metrics", interval="30s")],
            ),
        )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    Ntfy(chart, NAME)
    fleet_rules.add_fleet_rules(
        chart,
        providers=frozenset({"cnpg", "external-secrets-config", "gateway", "monitoring-crds"}),
        provided_secrets={},
    )
    return chart


def write_manifests(root: Path) -> None:
    """Generate ntfy's namespace, CNPG cluster, auth ESO, and app resources.

    The SOPS source Secret remains hand-written in this flat directory; the generated
    Kustomization lists them and therefore enables Flux SOPS decryption.
    """
    out_dir = root / OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(out_dir))
    rendered_chart = chart(app)
    app.synth()

    resources = ["ntfy.k8s.yaml", "credentials.sops.yaml"]
    write_yaml(
        out_dir / "flux-kustomization.yaml",
        flux_kustomization(
            NAME,
            description="Self-hosted ntfy for Android and cluster alert notifications.",
            spec=KustomizationSpec(
                retry_interval="1m",
                interval="10m",
                path=f"./{OUTPUT_DIR}",
                prune=True,
                wait=True,
                source_ref=KustomizationSpecSourceRef(
                    kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=NAME, namespace=FLUX_NAMESPACE
                ),
                timeout="10m",
                decryption=sops_decryption(resources),
                health_checks=health_checks(
                    rendered_chart,
                    (
                        "Namespace",
                        "ClusterSecretStore",
                        "Cluster",
                        "ExternalSecret",
                        "Deployment",
                        "HTTPRoute",
                        "ServiceMonitor",
                    ),
                ),
                depends_on=[
                    KustomizationSpecDependsOn(name=dependency)
                    for dependency in ("cnpg", "external-secrets-config", "gateway", "monitoring-crds")
                ],
            ),
        ),
    )
    write_yaml(out_dir / "kustomization.yaml", kustomize_kustomization(resources=resources))

"""Kubernetes resources for the LiteLLM proxy.

The Deployment's image tag is a deliberate placeholder ("unset") -- the
image-pins/ Kustomize Component (hand-written, see
cluster/k8s/litellm/app/image-pins/kustomization.yaml) overrides it at
`kustomize build` time via Flux's image-automation marker. See
cluster/docs/cdk8s.md.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart, Duration, Size
from cdk8s_plus_34 import (
    ConfigMap,
    ContainerResources,
    ContainerSecurityContextProps,
    Cpu,
    CpuResources,
    Deployment,
    DeploymentStrategy,
    EnvValue,
    ImagePullPolicy,
    MemoryResources,
    Node,
    NodeLabelQuery,
    NodeTaintQuery,
    PathMapping,
    PercentOrAbsolute,
    PodSecurityContextProps,
    Probe,
    Service,
    ServiceAccount,
    ServiceType,
    TaintEffect,
    Volume,
    k8s,
)
from constructs import Construct
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy, KustomizationSpecHealthChecks
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitor
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import disruption_budget, node_scheduling, pod_policy
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.forgejo_registry.pull_credentials import (
    forgejo_images_creds_external_secret,
    forgejo_images_creds_secret_ref,
)
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_app, write_yaml
from cluster.cdk8s.litellm import database
from cluster.cdk8s.litellm.config import main_proxy_config
from cluster.cdk8s.litellm.database import NAMESPACE
from cluster.cdk8s.litellm.upstreams import (
    RUGGED_LOCAL_LLM_KEY_ENV,
    RUGGED_LOCAL_LLM_PORT,
    RUGGED_LOCAL_LLM_SECRET,
    RUGGED_LOCAL_LLM_SERVICE,
)
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint
from cluster.cdk8s.secret_ref import SecretKey, SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.scripts.nebula_mesh import Mesh

_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
APP_DIR = f"{HAND_WRITTEN_ROOT}/litellm/app"
NAME = "litellm"
SERVICE = ServiceRef(
    name=NAME,
    port=Port(name="http", number=4000),
    pods=Pods(namespace=NAMESPACE, labels=(("app.kubernetes.io/name", NAME),)),
)
# The proxy's admin key: its env and its ServiceMonitor's scrape bearer.
_MASTER_KEY = SecretRef(namespace=NAMESPACE, name="litellm-master-key").key("api-key")
_LANGFUSE_OTEL_CONFIG = SecretRef(namespace=NAMESPACE, name="litellm-langfuse-otel-config")
_CONFIG_DIR = "/etc/litellm"
_CONFIG_FILE = "config.yaml"
_RUGGED_LOCAL_LLM_PORT = Port(name="http", number=RUGGED_LOCAL_LLM_PORT)


def _http_probe(path: str, initial_delay_seconds: int, failure_threshold: int) -> Probe:
    return http_probe(
        path,
        port=SERVICE.port.number,
        initial_delay_seconds=initial_delay_seconds,
        timeout_seconds=5,
        failure_threshold=failure_threshold,
    )


class LiteLLMProxy(Construct):
    """Compose the LiteLLM ConfigMap, Deployment, Service, and route."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)

        config_map = self._add_config_map()
        forgejo_images_creds_external_secret(self, "forgejo-images-creds", namespace=NAMESPACE)
        service_account = self._add_service_account()
        deployment = self._add_deployment(config_map, service_account)
        self._add_service(deployment)
        self._add_http_route()

    def _add_config_map(self) -> ConfigMap:
        return ConfigMap(
            self,
            "config",
            metadata=ApiObjectMetadata(
                name="config",
                namespace=NAMESPACE,
                labels={"app.kubernetes.io/managed-by": "cdk8s", "app.kubernetes.io/part-of": "litellm"},
                annotations={
                    "ducktape.dev/generated": "by cdk8s under Bazel",
                    "ducktape.dev/delivery": "Flux can consume this ordinary Kubernetes YAML",
                },
            ),
            data={_CONFIG_FILE: yaml_config(main_proxy_config())},
        )

    def _env_variables(self) -> dict[str, EnvValue]:
        def secret(name: str, key: SecretKey, *, optional: bool = False) -> tuple[str, EnvValue]:
            return name, key.env_value(self, f"{name}-secret", optional=optional)

        return dict(
            [
                ("HOST", EnvValue.from_value("0.0.0.0")),
                ("PORT", EnvValue.from_value(str(SERVICE.port.number))),
                ("LITELLM_OTEL_V2", EnvValue.from_value("true")),
                ("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", EnvValue.from_value("span_only")),
                # Export only model-call spans, each as its trace root, so Langfuse stores
                # each request once (cluster/cdk8s/litellm/docs/langfuse_tracing.md).
                ("LITELLM_OTEL_LANGFUSE_SPAN_SCOPE", EnvValue.from_value("llm_only")),
                secret("LITELLM_MASTER_KEY", _MASTER_KEY),
                secret("DATABASE_URL", database.DATABASE.app_secret.key("uri")),
                secret("LITELLM_SALT_KEY", SecretRef(namespace=NAMESPACE, name="litellm-salt-key").key("key")),
                secret(
                    "ANTHROPIC_API_KEY", SecretRef(namespace=NAMESPACE, name="litellm-anthropic-key").key("api-key")
                ),
                secret("GROQ_API_KEY", SecretRef(namespace=NAMESPACE, name="litellm-groq-key").key("GROQ_API_KEY")),
                secret(
                    "GEMINI_API_KEY", SecretRef(namespace=NAMESPACE, name="litellm-gemini-key").key("GEMINI_API_KEY")
                ),
                secret(
                    "MISTRAL_API_KEY", SecretRef(namespace=NAMESPACE, name="litellm-mistral-key").key("MISTRAL_API_KEY")
                ),
                secret(
                    "CLIPROXY_CLIENT_KEY",
                    SecretRef(namespace=NAMESPACE, name="litellm-cliproxy-key").key("CLIPROXY_CLIENT_KEY"),
                ),
                secret(
                    "TANA_FIREBASE_REFRESH_TOKEN",
                    SecretRef(namespace=NAMESPACE, name="tana-firebase-refresh-token").key("refresh_token"),
                    optional=True,
                ),
                secret(
                    RUGGED_LOCAL_LLM_KEY_ENV, SecretRef(namespace=NAMESPACE, name=RUGGED_LOCAL_LLM_SECRET).key("token")
                ),
                # Without the optional Langfuse exporter, LiteLLM uses its console exporter.
                secret("EXPORTERS", _LANGFUSE_OTEL_CONFIG.key("EXPORTERS"), optional=True),
            ]
        )

    def _add_deployment(self, config_map: ConfigMap, service_account: ServiceAccount) -> Deployment:
        deployment = Deployment(
            self,
            "deployment",
            metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE, labels=SERVICE.pods.selector),
            pod_metadata=ApiObjectMetadata(labels=SERVICE.pods.selector),
            replicas=2,
            strategy=DeploymentStrategy.rolling_update(
                max_surge=PercentOrAbsolute.absolute(1), max_unavailable=PercentOrAbsolute.absolute(0)
            ),
            service_account=service_account,
            termination_grace_period=Duration.seconds(90),
            docker_registry_auth=forgejo_images_creds_secret_ref(self, "forgejo-images-creds-ref"),
            # cdk8s_plus_34 defaults pods to a hardened SecurityContext
            # (runAsNonRoot). Opt out explicitly to preserve today's actual behavior
            # -- the real container's root needs haven't been audited, so silently
            # hardening it here could break the running proxy.
            security_context=PodSecurityContextProps(ensure_non_root=False),
        )

        deployment.add_container(
            name="litellm",
            image=f"git.allegedly.works/ducktape-ci/tana-litellm-proxy:{_PLACEHOLDER_TAG}",
            args=["--config", f"{_CONFIG_DIR}/{_CONFIG_FILE}"],
            ports=[SERVICE.port.container_port()],
            env_variables=self._env_variables(),
            image_pull_policy=ImagePullPolicy.ALWAYS,
            liveness=_http_probe("/health/liveliness", 30, 3),
            readiness=_http_probe("/health/readiness", 10, 3),
            startup=_http_probe("/health/liveliness", 5, 36),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(100), limit=Cpu.units(2)),
                memory=MemoryResources(request=Size.gibibytes(1), limit=Size.gibibytes(4)),
            ),
            # Same rationale as the pod-level override above.
            security_context=ContainerSecurityContextProps(read_only_root_filesystem=False, ensure_non_root=False),
        )
        pod_policy.harden(deployment)
        volume = Volume.from_config_map(
            self, "config-volume", config_map, items={_CONFIG_FILE: PathMapping(path=_CONFIG_FILE)}
        )
        deployment.containers[0].mount(_CONFIG_DIR, volume, read_only=True)

        deployment.scheduling.attract(
            Node.labeled(NodeLabelQuery.is_(node_scheduling.ZONE_LABEL, node_scheduling.HIL_OVH_ZONE))
        )
        deployment.scheduling.tolerate(
            Node.tainted(NodeTaintQuery.exists(node_scheduling.CONTROL_PLANE_TAINT_KEY, effect=TaintEffect.NO_SCHEDULE))
        )
        node_scheduling.set_topology_spread(
            deployment,
            k8s.TopologySpreadConstraint(
                max_skew=1,
                topology_key="kubernetes.io/hostname",
                when_unsatisfiable="ScheduleAnyway",
                label_selector=k8s.LabelSelector(match_labels=SERVICE.pods.selector),
            ),
        )
        return deployment

    def _add_service(self, deployment: Deployment) -> None:
        disruption_budget.add_disruption_budget(self, "disruption-budget", name=NAME, pods=SERVICE.pods)
        Service(
            self,
            "service",
            metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE, labels=SERVICE.labels),
            selector=deployment,
            ports=[SERVICE.port.service_port()],
            type=ServiceType.CLUSTER_IP,
        )

    def _add_service_account(self) -> ServiceAccount:
        return ServiceAccount(
            self, "serviceaccount", metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE), automount_token=False
        )

    def _add_http_route(self) -> None:
        https_route(
            self,
            "httproute",
            metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
            hostnames=["litellm.allegedly.works"],
            backend=SERVICE,
            timeout="2400s",
            hsts=False,
            listener=None,
        )


class LiteLLMServiceMonitor(Construct):
    """Scrape metrics from the LiteLLM Service using its admin key."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        ServiceMonitor(
            self,
            "servicemonitor",
            metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
            spec=SERVICE.service_monitor_spec(
                Endpoint.bearer_token_secret(
                    port=SERVICE.port.name,
                    secret_name=_MASTER_KEY.secret.name,
                    key=_MASTER_KEY.key,
                    scrape_timeout="10s",
                )
            ),
        )


def _chart(app: App, mesh: Mesh) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    _rugged_local_llm_endpoint(chart, mesh)
    LiteLLMProxy(chart, "proxy")
    LiteLLMServiceMonitor(chart, "monitoring")
    return chart


def _rugged_local_llm_endpoint(scope: Construct, mesh: Mesh) -> None:
    host = mesh.hosts["rugged"]
    Service(
        scope,
        "rugged-local-llm-service",
        metadata=ApiObjectMetadata(name=RUGGED_LOCAL_LLM_SERVICE, namespace=NAMESPACE),
        cluster_ip="None",
        ports=[_RUGGED_LOCAL_LLM_PORT.service_port()],
    )
    k8s.KubeEndpointSlice(
        scope,
        "rugged-local-llm-endpoints",
        metadata=k8s.ObjectMeta(
            name=RUGGED_LOCAL_LLM_SERVICE,
            namespace=NAMESPACE,
            labels={
                "kubernetes.io/service-name": RUGGED_LOCAL_LLM_SERVICE,
                "endpointslice.kubernetes.io/managed-by": "ducktape-static",
            },
        ),
        address_type="IPv4",
        ports=[k8s.EndpointPort(name=_RUGGED_LOCAL_LLM_PORT.name, port=_RUGGED_LOCAL_LLM_PORT.number, protocol="TCP")],
        endpoints=[
            k8s.Endpoint(addresses=[host.nebula_ip], hostname="rugged", conditions=k8s.EndpointConditions(ready=True))
        ],
    )


def write_manifests(root: Path, mesh: Mesh) -> Chart:
    app = App()
    chart = _chart(app, mesh)
    write_yaml(
        root / APP_DIR / "kustomization.yaml",
        kustomize_kustomization(
            namespace=NAMESPACE, resources=[write_app(root, APP_DIR, app)], components=["./image-pins"]
        ),
    )

    return chart


def litellm(
    flux_chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    cnpg: Kustomization,
    external_secrets_operator: Kustomization,
    monitoring_crds: Kustomization,
    seaweedfs_operator: Kustomization,
    *,
    health_checks: list[KustomizationSpecHealthChecks],
) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        NAME,
        artifact,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        decryption=SOPS_DECRYPTION,
        timeout="10m",
        wait=None,
        health_checks=health_checks,
        depends_on=flux_kustomization_depends_on_many(
            cnpg, external_secrets_operator, monitoring_crds, seaweedfs_operator
        ),
    )

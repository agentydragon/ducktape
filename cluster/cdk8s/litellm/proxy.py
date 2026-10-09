"""Reusable cdk8s constructs for the LiteLLM proxy deployments.

The Deployment's image tag is a deliberate placeholder ("unset") -- the
image-pins/ Kustomize Component (hand-written, see
cluster/k8s/litellm/app/image-pins/kustomization.yaml) overrides it at
`kustomize build` time via Flux's image-automation marker. See
cluster/docs/cdk8s.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from cdk8s import ApiObject, ApiObjectMetadata, App, Chart, Duration, JsonPatch, Size
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
    LabeledNode,
    MemoryResources,
    Node,
    NodeLabelQuery,
    NodeTaintQuery,
    PathMapping,
    PercentOrAbsolute,
    PodSecurityContextProps,
    Probe,
    Secret,
    Service,
    ServiceAccount,
    ServiceType,
    TaintedNode,
    TaintEffect,
    Volume,
    k8s,
)
from constructs import Construct
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import node_scheduling, pod_policy
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.fleet_rules import add_fleet_rules
from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.forgejo_registry.chart import forgejo_images_creds_external_secret
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.litellm import database
from cluster.cdk8s.litellm.config import ConfigMapSpec, proxy_configs
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor
from cluster.cdk8s.secret_ref import SecretKey, SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
APP_DIR = f"{HAND_WRITTEN_ROOT}/litellm/app"
_HTTP = Port(name="http", number=4000)
# The namespace the proxy runs in and reads its Secrets from.
_NAMESPACE = "litellm"
# The proxy's admin key: its env and its ServiceMonitor's scrape bearer.
_MASTER_KEY = SecretRef(namespace=_NAMESPACE, name="litellm-master-key").key("api-key")
# Langfuse's project keys.
_LANGFUSE = SecretRef(namespace=_NAMESPACE, name="langfuse-secrets")
_CONFIG_DIR = "/etc/litellm"


@dataclass(frozen=True)
class _LiteralEnv:
    name: str
    value: str


@dataclass(frozen=True)
class _SecretEnv:
    name: str
    secret: SecretKey
    optional: bool = False


_EnvEntry = _LiteralEnv | _SecretEnv


@dataclass(frozen=True)
class ServiceSpec:
    """The small set of Service fields that differs between the proxies."""

    type: ServiceType | None = ServiceType.CLUSTER_IP


@dataclass(frozen=True)
class ProxySpec:
    """Configuration for one instance of the reusable LiteLLM construct."""

    config: ConfigMapSpec
    image_name: str  # untagged -- e.g. "git.allegedly.works/ducktape-ci/tana-litellm-proxy"
    replicas: int
    env: tuple[_EnvEntry, ...]
    startup_failure_threshold: int
    resources: ContainerResources | None = None
    image_pull_policy: ImagePullPolicy | None = None
    image_pull_secret_name: str | None = None
    service_account_name: str | None = None
    termination_grace_period_seconds: int | None = None
    node_affinity: LabeledNode | None = None
    tolerations: tuple[TaintedNode, ...] = ()
    # cdk8s_plus_34's fluent Deployment/Workload has no builder for custom
    # topologySpreadConstraints (only an all-or-nothing `spread: bool`
    # auto-toggle), but k8s.TopologySpreadConstraint (the raw generated struct)
    # is real API-schema-validated input to the ApiObject escape hatch in
    # _add_deployment, not a raw dict.
    topology_spread_constraints: tuple[k8s.TopologySpreadConstraint, ...] = ()
    strategy: DeploymentStrategy | None = None
    service: ServiceSpec = field(default_factory=ServiceSpec)
    hostname: str | None = None
    forgejo_image_credentials: bool = False

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def namespace(self) -> str:
        return self.config.namespace


def service(spec: ProxySpec) -> ServiceRef:
    return ServiceRef(
        name=spec.name, port=_HTTP, pods=Pods(namespace=spec.namespace, labels=(("app.kubernetes.io/name", spec.name),))
    )


def _base_env(*entries: _EnvEntry) -> tuple[_EnvEntry, ...]:
    return (_LiteralEnv("HOST", "0.0.0.0"), _LiteralEnv("PORT", str(_HTTP.number)), *entries)


def _langfuse_env(*entries: _EnvEntry) -> tuple[_EnvEntry, ...]:
    return (
        *_base_env(*entries),
        _LiteralEnv("LANGFUSE_OTEL_HOST", "http://langfuse-web.langfuse.svc.cluster.local:3000"),
        _SecretEnv("LANGFUSE_PUBLIC_KEY", _LANGFUSE.key("LANGFUSE_INIT_PROJECT_PUBLIC_KEY")),
        _SecretEnv("LANGFUSE_SECRET_KEY", _LANGFUSE.key("LANGFUSE_INIT_PROJECT_SECRET_KEY")),
    )


def proxy_specs() -> tuple[ProxySpec, ...]:
    """Return the proxy-specific values consumed by :class:`LiteLLMProxy`."""
    configs = {config.name: config for config in proxy_configs()}
    return (
        ProxySpec(
            config=configs["litellm"],
            image_name="git.allegedly.works/ducktape-ci/tana-litellm-proxy",
            replicas=2,
            env=_langfuse_env(
                _SecretEnv("LITELLM_MASTER_KEY", _MASTER_KEY),
                _SecretEnv("DATABASE_URL", database.DATABASE.app_secret.key("uri")),
                _SecretEnv("LITELLM_SALT_KEY", SecretRef(namespace=_NAMESPACE, name="litellm-salt-key").key("key")),
                _SecretEnv(
                    "ANTHROPIC_API_KEY", SecretRef(namespace=_NAMESPACE, name="litellm-anthropic-key").key("api-key")
                ),
                _SecretEnv(
                    "GROQ_API_KEY", SecretRef(namespace=_NAMESPACE, name="litellm-groq-key").key("GROQ_API_KEY")
                ),
                _SecretEnv(
                    "GEMINI_API_KEY", SecretRef(namespace=_NAMESPACE, name="litellm-gemini-key").key("GEMINI_API_KEY")
                ),
                _SecretEnv(
                    "MISTRAL_API_KEY",
                    SecretRef(namespace=_NAMESPACE, name="litellm-mistral-key").key("MISTRAL_API_KEY"),
                ),
                _SecretEnv(
                    "CLIPROXY_CLIENT_KEY",
                    SecretRef(namespace=_NAMESPACE, name="litellm-cliproxy-key").key("CLIPROXY_CLIENT_KEY"),
                ),
                _SecretEnv(
                    "TANA_FIREBASE_REFRESH_TOKEN",
                    SecretRef(namespace=_NAMESPACE, name="tana-firebase-refresh-token").key("refresh_token"),
                    optional=True,
                ),
            ),
            startup_failure_threshold=36,
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(100), limit=Cpu.units(2)),
                memory=MemoryResources(request=Size.gibibytes(1), limit=Size.gibibytes(4)),
            ),
            image_pull_policy=ImagePullPolicy.ALWAYS,
            image_pull_secret_name="forgejo-images-creds",
            service_account_name="litellm",
            termination_grace_period_seconds=90,
            node_affinity=Node.labeled(NodeLabelQuery.is_(node_scheduling.ZONE_LABEL, node_scheduling.HIL_OVH_ZONE)),
            tolerations=(
                Node.tainted(
                    NodeTaintQuery.exists(node_scheduling.CONTROL_PLANE_TAINT_KEY, effect=TaintEffect.NO_SCHEDULE)
                ),
            ),
            topology_spread_constraints=(
                k8s.TopologySpreadConstraint(
                    max_skew=1,
                    topology_key="kubernetes.io/hostname",
                    when_unsatisfiable="ScheduleAnyway",
                    label_selector=k8s.LabelSelector(match_labels={"app.kubernetes.io/name": "litellm"}),
                ),
            ),
            strategy=DeploymentStrategy.rolling_update(
                max_surge=PercentOrAbsolute.absolute(1), max_unavailable=PercentOrAbsolute.absolute(0)
            ),
            hostname="litellm.allegedly.works",
            forgejo_image_credentials=True,
        ),
    )


def _formatted_config_map_data(data: dict[str, object]) -> dict[str, str]:
    formatted: dict[str, str] = {}
    for filename, value in data.items():
        if isinstance(value, dict):
            formatted[filename] = yaml_config(value)
        else:
            assert isinstance(value, str)
            formatted[filename] = value
    return formatted


def _http_probe(path: str, initial_delay_seconds: int, failure_threshold: int) -> Probe:
    return http_probe(
        path,
        port=_HTTP.number,
        initial_delay_seconds=initial_delay_seconds,
        timeout_seconds=5,
        failure_threshold=failure_threshold,
    )


class LiteLLMProxy(Construct):
    """Compose one LiteLLM ConfigMap, Deployment, Service, and optional extras."""

    def __init__(self, scope: Construct, id: str, spec: ProxySpec) -> None:
        super().__init__(scope, id)
        self.spec = spec

        config_map = self._add_config_map()
        if spec.forgejo_image_credentials:
            self._add_forgejo_image_credentials()
        service_account = self._add_service_account() if spec.service_account_name is not None else None
        deployment = self._add_deployment(config_map, service_account)
        self._add_service(deployment)
        if spec.hostname is not None:
            self._add_http_route(spec.hostname)

    def _add_config_map(self) -> ConfigMap:
        return ConfigMap(
            self,
            "config",
            metadata=ApiObjectMetadata(
                name=self.spec.config.config_map_name,
                namespace=self.spec.namespace,
                labels={"app.kubernetes.io/managed-by": "cdk8s", "app.kubernetes.io/part-of": "litellm"},
                annotations={
                    "ducktape.dev/generated": "by cdk8s under Bazel",
                    "ducktape.dev/delivery": "Flux can consume this ordinary Kubernetes YAML",
                },
            ),
            data=_formatted_config_map_data(self.spec.config.data),
        )

    def _env_variables(self) -> dict[str, EnvValue]:
        return {
            entry.name: (
                EnvValue.from_value(entry.value)
                if isinstance(entry, _LiteralEnv)
                else entry.secret.env_value(self, f"{entry.name}-secret", optional=entry.optional)
            )
            for entry in self.spec.env
        }

    def _add_deployment(self, config_map: ConfigMap, service_account: ServiceAccount | None) -> Deployment:
        ref = service(self.spec)
        deployment = Deployment(
            self,
            "deployment",
            metadata=ApiObjectMetadata(name=self.spec.name, namespace=self.spec.namespace, labels=ref.pods.selector),
            pod_metadata=ApiObjectMetadata(labels=ref.pods.selector),
            replicas=self.spec.replicas,
            strategy=self.spec.strategy,
            service_account=service_account,
            termination_grace_period=(
                Duration.seconds(self.spec.termination_grace_period_seconds)
                if self.spec.termination_grace_period_seconds is not None
                else None
            ),
            docker_registry_auth=(
                Secret.from_secret_name(self, "forgejo-images-creds-ref", self.spec.image_pull_secret_name)
                if self.spec.image_pull_secret_name is not None
                else None
            ),
            # cdk8s_plus_34 defaults pods to a hardened SecurityContext
            # (runAsNonRoot). Opt out explicitly to preserve today's actual behavior
            # -- the real container's root needs haven't been audited, so silently
            # hardening it here could break the running proxy.
            security_context=PodSecurityContextProps(ensure_non_root=False),
        )

        deployment.add_container(
            name="litellm",
            image=f"{self.spec.image_name}:{_PLACEHOLDER_TAG}",
            args=["--config", f"{_CONFIG_DIR}/config.yaml"],
            ports=[ref.port.container_port()],
            env_variables=self._env_variables(),
            image_pull_policy=self.spec.image_pull_policy,
            liveness=_http_probe("/health/liveliness", 30, 3),
            readiness=_http_probe("/health/readiness", 10, 3),
            startup=_http_probe("/health/liveliness", 5, self.spec.startup_failure_threshold),
            resources=self.spec.resources,
            # Same rationale as the pod-level override above.
            security_context=ContainerSecurityContextProps(read_only_root_filesystem=False, ensure_non_root=False),
        )
        pod_policy.harden(deployment)
        volume = Volume.from_config_map(
            self, "config-volume", config_map, items={"config.yaml": PathMapping(path="config.yaml")}
        )
        deployment.containers[0].mount(_CONFIG_DIR, volume, read_only=True)

        if self.spec.node_affinity is not None:
            deployment.scheduling.attract(self.spec.node_affinity)
        for toleration in self.spec.tolerations:
            deployment.scheduling.tolerate(toleration)
        if self.spec.topology_spread_constraints:
            # cdk8s_plus_34's Deployment (a Workload, not an ApiObject subclass) has
            # no direct escape hatch; ApiObject.of() reaches the ApiObject it
            # manages internally.
            ApiObject.of(deployment).add_json_patch(
                JsonPatch.add(
                    "/spec/template/spec/topologySpreadConstraints", list(self.spec.topology_spread_constraints)
                )
            )
        return deployment

    def _add_service(self, deployment: Deployment) -> None:
        ref = service(self.spec)
        Service(
            self,
            "service",
            metadata=ApiObjectMetadata(name=ref.name, namespace=ref.pods.namespace, labels=ref.labels),
            selector=deployment,
            ports=[ref.port.service_port()],
            type=self.spec.service.type,
        )

    def _add_service_account(self) -> ServiceAccount:
        assert self.spec.service_account_name is not None
        return ServiceAccount(
            self,
            "serviceaccount",
            metadata=ApiObjectMetadata(name=self.spec.service_account_name, namespace=self.spec.namespace),
            automount_token=False,
        )

    def _add_http_route(self, hostname: str) -> None:
        https_route(
            self,
            "httproute",
            metadata=ApiObjectMetadata(name=self.spec.name, namespace=self.spec.namespace),
            hostnames=[hostname],
            backend=service(self.spec),
            timeout="600s",
            hsts=False,
            listener=None,
        )

    def _add_forgejo_image_credentials(self) -> None:
        forgejo_images_creds_external_secret(self, "forgejo-images-creds", namespace=self.spec.namespace)


class LiteLLMServiceMonitor(Construct):
    """The monitoring resource shared by the main LiteLLM service."""

    def __init__(self, scope: Construct, id: str, service: ServiceRef) -> None:
        super().__init__(scope, id)
        ServiceMonitor(
            self,
            "servicemonitor",
            metadata=ApiObjectMetadata(name=service.name, namespace=service.pods.namespace),
            selector=ServiceMonitorSpecSelector(match_labels=service.labels),
            endpoints=[
                Endpoint.bearer_token_secret(
                    port=service.port.name,
                    secret_name=_MASTER_KEY.secret.name,
                    key=_MASTER_KEY.key,
                    scrape_timeout="10s",
                )
            ],
        )


def _chart(app: App) -> Chart:
    (spec,) = proxy_specs()  # only one LiteLLM proxy today; extend proxy_specs() when a second lands
    chart = Chart(app, spec.name, disable_resource_name_hashes=True)
    LiteLLMProxy(chart, "proxy", spec)
    LiteLLMServiceMonitor(chart, "monitoring", service(spec))
    add_fleet_rules(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_yaml(
        root / APP_DIR / "kustomization.yaml",
        kustomize_kustomization(
            namespace="litellm", resources=[write_charts(root, APP_DIR, _chart)], components=["./image-pins"]
        ),
    )


def litellm(
    flux_chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    cnpg: Kustomization,
    external_secrets_operator: Kustomization,
    monitoring_crds: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        "litellm",
        artifact,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        decryption=SOPS_DECRYPTION,
        timeout="10m",
        depends_on=flux_kustomization_depends_on_many(cnpg, external_secrets_operator, monitoring_crds),
    )

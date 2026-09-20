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
    ContainerPort,
    ContainerResources,
    ContainerSecurityContextProps,
    Cpu,
    CpuResources,
    Deployment,
    DeploymentStrategy,
    EnvValue,
    ImagePullPolicy,
    ISecret,
    LabeledNode,
    MemoryResources,
    Node,
    NodeLabelQuery,
    NodeTaintQuery,
    PathMapping,
    PercentOrAbsolute,
    PodSecurityContextProps,
    Probe,
    Protocol,
    Secret,
    SecretValue,
    Service,
    ServiceAccount,
    ServicePort,
    ServiceType,
    TaintedNode,
    TaintEffect,
    Volume,
    k8s,
)
from constructs import Construct
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    Kustomization,
    KustomizationSpec,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)
from prometheus_operator_crds.com.coreos.monitoring import (
    ServiceMonitor,
    ServiceMonitorSpec,
    ServiceMonitorSpecEndpoints,
    ServiceMonitorSpecEndpointsBearerTokenSecret,
    ServiceMonitorSpecSelector,
)

from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.fleet_rules import add_fleet_rules
from cluster.cdk8s.flux import (
    NAMESPACE,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.forgejo_images import forgejo_images_creds_external_secret
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_yaml
from cluster.cdk8s.litellm.config import ConfigMapSpec, proxy_configs
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.pod_spec_patches import runtime_default_seccomp_patch
from cluster.cdk8s.probes import http_probe

_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
APP_DIR = "cluster/k8s/litellm/app"
_CONTAINER_PORT = 4000
_CONFIG_DIR = "/etc/litellm"


@dataclass(frozen=True)
class _LiteralEnv:
    name: str
    value: str


@dataclass(frozen=True)
class _SecretEnv:
    name: str
    secret_name: str
    key: str


_EnvEntry = _LiteralEnv | _SecretEnv


@dataclass(frozen=True)
class ServiceSpec:
    """The small set of Service fields that differs between the proxies."""

    labels: dict[str, str] | None = None
    type: ServiceType | None = ServiceType.CLUSTER_IP
    protocol: Protocol | None = Protocol.TCP


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


def _literal_env(name: str, value: str) -> _EnvEntry:
    return _LiteralEnv(name, value)


def _secret_env(name: str, secret_name: str, key: str) -> _EnvEntry:
    return _SecretEnv(name, secret_name, key)


def _base_env(*entries: _EnvEntry) -> tuple[_EnvEntry, ...]:
    return (_literal_env("HOST", "0.0.0.0"), _literal_env("PORT", "4000"), *entries)


def _langfuse_env(*entries: _EnvEntry) -> tuple[_EnvEntry, ...]:
    return (
        *_base_env(*entries),
        _literal_env("LANGFUSE_OTEL_HOST", "http://langfuse-web.langfuse.svc.cluster.local:3000"),
        _secret_env("LANGFUSE_PUBLIC_KEY", "langfuse-secrets", "LANGFUSE_INIT_PROJECT_PUBLIC_KEY"),
        _secret_env("LANGFUSE_SECRET_KEY", "langfuse-secrets", "LANGFUSE_INIT_PROJECT_SECRET_KEY"),
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
                _secret_env("LITELLM_MASTER_KEY", "litellm-master-key", "api-key"),
                _secret_env("DATABASE_URL", "litellm-db-app", "uri"),
                _secret_env("LITELLM_SALT_KEY", "litellm-salt-key", "key"),
                _secret_env("ANTHROPIC_API_KEY", "litellm-anthropic-key", "api-key"),
                _secret_env("GROQ_API_KEY", "litellm-groq-key", "GROQ_API_KEY"),
                _secret_env("GEMINI_API_KEY", "litellm-gemini-key", "GEMINI_API_KEY"),
                _secret_env("MISTRAL_API_KEY", "litellm-mistral-key", "MISTRAL_API_KEY"),
                _secret_env("CLIPROXY_CLIENT_KEY", "litellm-cliproxy-key", "CLIPROXY_CLIENT_KEY"),
                _secret_env("TANA_FIREBASE_REFRESH_TOKEN", "tana-firebase-refresh-token", "refresh_token"),
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
            node_affinity=Node.labeled(NodeLabelQuery.is_("topology.kubernetes.io/zone", "hil-ovh")),
            tolerations=(
                Node.tainted(
                    NodeTaintQuery.exists("node-role.kubernetes.io/control-plane", effect=TaintEffect.NO_SCHEDULE)
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
            service=ServiceSpec(labels={"app.kubernetes.io/name": "litellm"}),
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
        port=_CONTAINER_PORT,
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
            metadata=metadata(
                self.spec.config.config_map_name,
                self.spec.namespace,
                labels={"app.kubernetes.io/managed-by": "cdk8s", "app.kubernetes.io/part-of": "litellm"},
                annotations={
                    "ducktape.dev/generated": "by cdk8s under Bazel",
                    "ducktape.dev/delivery": "Flux can consume this ordinary Kubernetes YAML",
                },
            ),
            data=_formatted_config_map_data(self.spec.config.data),
        )

    def _env_variables(self) -> dict[str, EnvValue]:
        secrets: dict[str, ISecret] = {}

        def secret_for(name: str) -> ISecret:
            if name not in secrets:
                secrets[name] = Secret.from_secret_name(self, f"{name}-secret", name)
            return secrets[name]

        result: dict[str, EnvValue] = {}
        for entry in self.spec.env:
            if isinstance(entry, _LiteralEnv):
                result[entry.name] = EnvValue.from_value(entry.value)
            else:
                result[entry.name] = EnvValue.from_secret_value(
                    SecretValue(secret=secret_for(entry.secret_name), key=entry.key)
                )
        return result

    def _add_deployment(self, config_map: ConfigMap, service_account: ServiceAccount | None) -> Deployment:
        labels = {"app.kubernetes.io/name": self.spec.name}
        deployment = Deployment(
            self,
            "deployment",
            metadata=metadata(
                self.spec.name, self.spec.namespace, labels=labels, annotations={"reloader.stakater.com/auto": "true"}
            ),
            pod_metadata=ApiObjectMetadata(labels=labels),
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
            ports=[ContainerPort(name="http", number=_CONTAINER_PORT, protocol=Protocol.TCP)],
            env_variables=self._env_variables(),
            image_pull_policy=self.spec.image_pull_policy,
            liveness=_http_probe("/health/liveliness", 30, 3),
            readiness=_http_probe("/health/readiness", 10, 3),
            startup=_http_probe("/health/liveliness", 5, self.spec.startup_failure_threshold),
            resources=self.spec.resources,
            # Same rationale as the pod-level override above.
            security_context=ContainerSecurityContextProps(read_only_root_filesystem=False, ensure_non_root=False),
        )
        ApiObject.of(deployment).add_json_patch(runtime_default_seccomp_patch())
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
        Service(
            self,
            "service",
            metadata=metadata(self.spec.name, self.spec.namespace, labels=self.spec.service.labels),
            selector=deployment,
            ports=[
                ServicePort(
                    name="http", port=_CONTAINER_PORT, target_port=_CONTAINER_PORT, protocol=self.spec.service.protocol
                )
            ],
            type=self.spec.service.type,
        )

    def _add_service_account(self) -> ServiceAccount:
        assert self.spec.service_account_name is not None
        return ServiceAccount(
            self,
            "serviceaccount",
            metadata=metadata(self.spec.service_account_name, self.spec.namespace),
            automount_token=False,
        )

    def _add_http_route(self, hostname: str) -> None:
        https_route(
            self,
            "httproute",
            metadata=metadata(self.spec.name, self.spec.namespace),
            hostname=hostname,
            backend=self.spec.name,
            port=4000,
            timeout="600s",
            hsts=False,
            listener=None,
        )

    def _add_forgejo_image_credentials(self) -> None:
        forgejo_images_creds_external_secret(self, "forgejo-images-creds", namespace=self.spec.namespace)


class LiteLLMServiceMonitor(Construct):
    """The monitoring resource shared by the main LiteLLM service."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        ServiceMonitor(
            self,
            "servicemonitor",
            metadata=metadata("litellm", "litellm"),
            spec=ServiceMonitorSpec(
                selector=ServiceMonitorSpecSelector(match_labels={"app.kubernetes.io/name": "litellm"}),
                endpoints=[
                    ServiceMonitorSpecEndpoints(
                        port="http",
                        path="/metrics",
                        scrape_timeout="10s",
                        bearer_token_secret=ServiceMonitorSpecEndpointsBearerTokenSecret(
                            name="litellm-master-key", key="api-key"
                        ),
                    )
                ],
            ),
        )


def litellm(
    flux_chart: Chart,
    root: Path,
    external_secrets_config: Kustomization,
    forgejo_images: Kustomization,
    litellm_secrets: Kustomization,
    litellm_db: Kustomization,
    gateway: Kustomization,
    cert_manager_environment: Kustomization,
    langfuse_secrets: Kustomization,
    reflector: Kustomization,
    tana_mcp: Kustomization,
    monitoring_crds: Kustomization,
) -> Kustomization:
    (spec,) = proxy_specs()  # only one LiteLLM proxy today; extend proxy_specs() when a second lands

    app_dir = root / APP_DIR
    app_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(app_dir))
    chart = Chart(app, spec.name, disable_resource_name_hashes=True)
    LiteLLMProxy(chart, "proxy", spec)
    LiteLLMServiceMonitor(chart, "monitoring")
    add_fleet_rules(chart)
    app.synth()

    kustomization = flux_kustomization(
        flux_chart,
        "litellm",
        spec=KustomizationSpec(
            interval="10m",
            path=f"./{APP_DIR}",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name="litellm", namespace=NAMESPACE
            ),
            timeout="10m",
            depends_on=flux_kustomization_depends_on_many(
                external_secrets_config,
                forgejo_images,
                litellm_secrets,
                litellm_db,
                gateway,
                cert_manager_environment,
                langfuse_secrets,
                reflector,
                tana_mcp,
                # The ServiceMonitor/PodMonitor CRD (folded in from the retired
                # litellm-servicemonitor Kustomization, #7103).
                monitoring_crds,
            ),
        ),
    )
    write_yaml(
        app_dir / "kustomization.yaml",
        kustomize_kustomization(namespace="litellm", resources=[f"{spec.name}.k8s.yaml"], components=["./image-pins"]),
    )
    return kustomization

"""Reusable cdk8s constructs for the LiteLLM proxy deployments.

The Deployment's image tag is a deliberate placeholder ("unset") -- the
image-pins/ Kustomize Component (hand-written, see
cluster/k8s/litellm/app/image-pins/kustomization.yaml) overrides it at
`kustomize build` time via Flux's image-automation marker. See
cluster/docs/cdk8s.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cdk8s import ApiObject, JsonPatch, Yaml
from cdk8s_plus_33 import ConfigMap
from constructs import Construct
from prometheus_operator_crds.com.coreos.monitoring import (
    ServiceMonitor,
    ServiceMonitorSpec,
    ServiceMonitorSpecEndpoints,
    ServiceMonitorSpecEndpointsBearerTokenSecret,
    ServiceMonitorSpecSelector,
)

from cluster.litellm_config import ConfigMapSpec, proxy_configs

_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml


@dataclass(frozen=True)
class ServiceSpec:
    """The small set of Service fields that differs between the proxies."""

    labels: dict[str, str] | None = None
    target_port: str | int = "http"
    type: str | None = "ClusterIP"
    protocol: str | None = "TCP"


@dataclass(frozen=True)
class ProxySpec:
    """Configuration for one instance of the reusable LiteLLM construct."""

    config: ConfigMapSpec
    image_name: str  # untagged -- e.g. "git.allegedly.works/ducktape-ci/tana-litellm-proxy"
    replicas: int
    env: tuple[dict[str, object], ...]
    startup_failure_threshold: int
    resources: dict[str, object] | None = None
    image_pull_policy: str | None = None
    image_pull_secrets: tuple[dict[str, str], ...] = ()
    service_account_name: str | None = None
    termination_grace_period_seconds: int | None = None
    node_selector: dict[str, str] | None = None
    tolerations: tuple[dict[str, object], ...] = ()
    topology_spread_constraints: tuple[dict[str, object], ...] = ()
    strategy: dict[str, object] | None = None
    service: ServiceSpec = field(default_factory=ServiceSpec)
    hostname: str | None = None
    forgejo_image_credentials: bool = False

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def namespace(self) -> str:
        return self.config.namespace


def _literal_env(name: str, value: str) -> dict[str, object]:
    return {"name": name, "value": value}


def _secret_env(name: str, secret_name: str, key: str) -> dict[str, object]:
    return {"name": name, "valueFrom": {"secretKeyRef": {"name": secret_name, "key": key}}}


def _base_env(*entries: dict[str, object]) -> tuple[dict[str, object], ...]:
    return (_literal_env("HOST", "0.0.0.0"), _literal_env("PORT", "4000"), *entries)


def _langfuse_env(*entries: dict[str, object]) -> tuple[dict[str, object], ...]:
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
            resources={"requests": {"cpu": "100m", "memory": "1Gi"}, "limits": {"cpu": "2", "memory": "4Gi"}},
            image_pull_policy="Always",
            image_pull_secrets=({"name": "forgejo-images-creds"},),
            service_account_name="litellm",
            termination_grace_period_seconds=90,
            node_selector={"topology.kubernetes.io/zone": "hil-ovh"},
            tolerations=(
                {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"},
            ),
            topology_spread_constraints=(
                {
                    "maxSkew": 1,
                    "topologyKey": "kubernetes.io/hostname",
                    "whenUnsatisfiable": "ScheduleAnyway",
                    "labelSelector": {"matchLabels": {"app.kubernetes.io/name": "litellm"}},
                },
            ),
            strategy={"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}},
            service=ServiceSpec(labels={"app.kubernetes.io/name": "litellm"}),
            hostname="litellm.allegedly.works",
            forgejo_image_credentials=True,
        ),
    )


def _yaml_config(config: dict) -> str:
    return Yaml.format_objects([config])


def _formatted_config_map_data(data: dict[str, object]) -> dict[str, str]:
    formatted: dict[str, str] = {}
    for filename, value in data.items():
        if isinstance(value, dict):
            formatted[filename] = _yaml_config(value)
        else:
            assert isinstance(value, str)
            formatted[filename] = value
    return formatted


def _api_resource(
    scope: Construct, resource_id: str, *, api_version: str, kind: str, metadata: dict, fields: dict[str, object]
) -> None:
    """Emit an unstructured Kubernetes resource through cdk8s's escape hatch."""
    resource = ApiObject(scope, resource_id, api_version=api_version, kind=kind, metadata=metadata)
    for field_name, value in fields.items():
        resource.add_json_patch(JsonPatch.add(f"/{field_name}", value))


def _metadata(
    name: str, namespace: str, *, labels: dict[str, str] | None = None, annotations: dict[str, str] | None = None
) -> dict[str, object]:
    result: dict[str, object] = {"name": name, "namespace": namespace}
    if labels is not None:
        result["labels"] = labels
    if annotations is not None:
        result["annotations"] = annotations
    return result


def _health_probe(path: str, initial_delay_seconds: int, failure_threshold: int) -> dict[str, object]:
    return {
        "httpGet": {"path": path, "port": "http"},
        "initialDelaySeconds": initial_delay_seconds,
        "periodSeconds": 10,
        "timeoutSeconds": 5,
        "failureThreshold": failure_threshold,
    }


class LiteLLMProxy(Construct):
    """Compose one LiteLLM ConfigMap, Deployment, Service, and optional extras."""

    def __init__(self, scope: Construct, id: str, spec: ProxySpec) -> None:
        super().__init__(scope, id)
        self.spec = spec

        self._add_config_map()
        if spec.forgejo_image_credentials:
            self._add_forgejo_image_credentials()
        self._add_deployment()
        self._add_service()
        if spec.service_account_name is not None:
            self._add_service_account()
        if spec.hostname is not None:
            self._add_http_route()

    @property
    def _config_items(self) -> list[dict[str, str]]:
        return [{"key": filename, "path": filename} for filename in self.spec.config.data]

    def _add_config_map(self) -> None:
        ConfigMap(
            self,
            "config",
            metadata={
                "name": self.spec.config.config_map_name,
                "namespace": self.spec.namespace,
                "labels": {"app.kubernetes.io/managed-by": "cdk8s", "app.kubernetes.io/part-of": "litellm"},
                "annotations": {
                    "ducktape.dev/generated": "by cdk8s under Bazel",
                    "ducktape.dev/delivery": "Flux can consume this ordinary Kubernetes YAML",
                },
            },
            data=_formatted_config_map_data(self.spec.config.data),
        )

    def _add_deployment(self) -> None:
        labels = {"app.kubernetes.io/name": self.spec.name}
        container: dict[str, object] = {
            "name": "litellm",
            "image": f"{self.spec.image_name}:{_PLACEHOLDER_TAG}",
            "args": ["--config", "/etc/litellm/config.yaml"],
            "ports": [{"name": "http", "containerPort": 4000, "protocol": "TCP"}],
            "env": list(self.spec.env),
            "livenessProbe": _health_probe("/health/liveliness", 30, 3),
            "readinessProbe": _health_probe("/health/readiness", 10, 3),
            "startupProbe": _health_probe("/health/liveliness", 5, self.spec.startup_failure_threshold),
            "volumeMounts": [{"name": "config", "mountPath": "/etc/litellm", "readOnly": True}],
        }
        pod_spec: dict[str, object] = {
            "containers": [container],
            "volumes": [
                {"name": "config", "configMap": {"name": self.spec.config.config_map_name, "items": self._config_items}}
            ],
        }
        if self.spec.image_pull_policy is not None:
            container["imagePullPolicy"] = self.spec.image_pull_policy
        if self.spec.resources is not None:
            container["resources"] = self.spec.resources
        if self.spec.image_pull_secrets:
            pod_spec["imagePullSecrets"] = list(self.spec.image_pull_secrets)
        if self.spec.service_account_name is not None:
            pod_spec["serviceAccountName"] = self.spec.service_account_name
        if self.spec.termination_grace_period_seconds is not None:
            pod_spec["terminationGracePeriodSeconds"] = self.spec.termination_grace_period_seconds
        if self.spec.node_selector is not None:
            pod_spec["nodeSelector"] = self.spec.node_selector
        if self.spec.tolerations:
            pod_spec["tolerations"] = list(self.spec.tolerations)
        if self.spec.topology_spread_constraints:
            pod_spec["topologySpreadConstraints"] = list(self.spec.topology_spread_constraints)

        deployment_spec: dict[str, object] = {
            "replicas": self.spec.replicas,
            "selector": {"matchLabels": labels},
            "template": {"metadata": {"labels": labels}, "spec": pod_spec},
        }
        if self.spec.strategy is not None:
            deployment_spec["strategy"] = self.spec.strategy
        _api_resource(
            self,
            "deployment",
            api_version="apps/v1",
            kind="Deployment",
            metadata=_metadata(
                self.spec.name, self.spec.namespace, labels=labels, annotations={"reloader.stakater.com/auto": "true"}
            ),
            fields={"spec": deployment_spec},
        )

    def _add_service(self) -> None:
        port: dict[str, object] = {"name": "http", "port": 4000, "targetPort": self.spec.service.target_port}
        if self.spec.service.protocol is not None:
            port["protocol"] = self.spec.service.protocol
        service_spec: dict[str, object] = {"selector": {"app.kubernetes.io/name": self.spec.name}, "ports": [port]}
        if self.spec.service.type is not None:
            service_spec["type"] = self.spec.service.type
        _api_resource(
            self,
            "service",
            api_version="v1",
            kind="Service",
            metadata=_metadata(self.spec.name, self.spec.namespace, labels=self.spec.service.labels),
            fields={"spec": service_spec},
        )

    def _add_service_account(self) -> None:
        assert self.spec.service_account_name is not None
        _api_resource(
            self,
            "serviceaccount",
            api_version="v1",
            kind="ServiceAccount",
            metadata=_metadata(self.spec.service_account_name, self.spec.namespace),
            fields={"automountServiceAccountToken": False},
        )

    def _add_http_route(self) -> None:
        _api_resource(
            self,
            "httproute",
            api_version="gateway.networking.k8s.io/v1",
            kind="HTTPRoute",
            metadata=_metadata(self.spec.name, self.spec.namespace),
            fields={
                "spec": {
                    "parentRefs": [{"name": "cluster-gateway", "namespace": "gateway-system"}],
                    "hostnames": [self.spec.hostname],
                    "rules": [
                        {
                            "timeouts": {"request": "600s", "backendRequest": "600s"},
                            "backendRefs": [{"name": self.spec.name, "port": 4000}],
                        }
                    ],
                }
            },
        )

    def _add_forgejo_image_credentials(self) -> None:
        _api_resource(
            self,
            "forgejo-images-creds",
            api_version="external-secrets.io/v1",
            kind="ExternalSecret",
            metadata=_metadata("forgejo-images-creds", self.spec.namespace),
            fields={
                "spec": {
                    "refreshInterval": "1h",
                    "secretStoreRef": {"name": "kubernetes-forgejo-images-secret-store", "kind": "ClusterSecretStore"},
                    "target": {
                        "name": "forgejo-images-creds",
                        "template": {"type": "kubernetes.io/dockerconfigjson", "mergePolicy": "Merge"},
                    },
                    "dataFrom": [{"extract": {"key": "forgejo-images-creds"}}],
                }
            },
        )


class LiteLLMServiceMonitor(Construct):
    """The monitoring resource shared by the main LiteLLM service."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        ServiceMonitor(
            self,
            "servicemonitor",
            metadata=_metadata("litellm", "litellm"),
            spec=ServiceMonitorSpec(
                selector=ServiceMonitorSpecSelector(match_labels={"app.kubernetes.io/name": "litellm"}),
                endpoints=[
                    ServiceMonitorSpecEndpoints(
                        port="http",
                        path="/metrics",
                        interval="15s",
                        scrape_timeout="10s",
                        bearer_token_secret=ServiceMonitorSpecEndpointsBearerTokenSecret(
                            name="litellm-master-key", key="api-key"
                        ),
                    )
                ],
            ),
        )

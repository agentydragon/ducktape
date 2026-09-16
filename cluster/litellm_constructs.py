"""Reusable cdk8s constructs for the LiteLLM proxy deployments.

deployment.yaml itself stays hand-written (see cluster/generate_manifests.py) --
this module only builds the rest of the app manifests.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cdk8s import ApiObject, JsonPatch, Yaml
from cdk8s_plus_33 import ConfigMap
from constructs import Construct

from cluster.litellm_config import ConfigMapSpec, proxy_configs


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
    service_account_name: str | None = None
    service: ServiceSpec = field(default_factory=ServiceSpec)
    hostname: str | None = None
    forgejo_image_credentials: bool = False

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def namespace(self) -> str:
        return self.config.namespace


def proxy_specs() -> tuple[ProxySpec, ...]:
    """Return the proxy-specific values consumed by :class:`LiteLLMProxy`."""
    configs = {config.name: config for config in proxy_configs()}
    return (
        ProxySpec(
            config=configs["litellm"],
            service_account_name="litellm",
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


class LiteLLMProxy(Construct):
    """Compose one LiteLLM ConfigMap, Service, and optional extras."""

    def __init__(self, scope: Construct, id: str, spec: ProxySpec) -> None:
        super().__init__(scope, id)
        self.spec = spec

        self._add_config_map()
        if spec.forgejo_image_credentials:
            self._add_forgejo_image_credentials()
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
        _api_resource(
            self,
            "servicemonitor",
            api_version="monitoring.coreos.com/v1",
            kind="ServiceMonitor",
            metadata=_metadata("litellm", "litellm"),
            fields={
                "spec": {
                    "selector": {"matchLabels": {"app.kubernetes.io/name": "litellm"}},
                    "endpoints": [
                        {
                            "port": "http",
                            "path": "/metrics",
                            "interval": "15s",
                            "scrapeTimeout": "10s",
                            "bearerTokenSecret": {"name": "litellm-master-key", "key": "api-key"},
                        }
                    ],
                }
            },
        )

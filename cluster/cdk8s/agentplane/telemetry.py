"""Shared Agentplane trace exporter settings and the pod label Alloy admits."""

from __future__ import annotations

from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

EMITTER_LABEL = "agentplane.io/otel-emitter"
EMITTER_LABEL_VALUE = "true"
EMITTER_LABELS = {EMITTER_LABEL: EMITTER_LABEL_VALUE}
EMITTER_SERVICE_CLASS_LABELS = {**EMITTER_LABELS, "agentplane.io/otel-class": "service"}
EMITTER_ENVIRONMENT_RUNTIME_CLASS_LABELS = {**EMITTER_LABELS, "agentplane.io/otel-class": "environment-runtime"}

ALLOY_NAMESPACE = "monitoring"
ALLOY_NAME = "alloy"
ALLOY_LABELS = {"app.kubernetes.io/name": ALLOY_NAME}
OTLP_GRPC_PORT = 4317
OTLP_HTTP_PORT = 4318
OTLP_GRPC_ENDPOINT = f"http://{ALLOY_NAME}.{ALLOY_NAMESPACE}.svc.cluster.local:{OTLP_GRPC_PORT}"
OTLP_HTTP_ENDPOINT = f"http://{ALLOY_NAME}.{ALLOY_NAMESPACE}.svc.cluster.local:{OTLP_HTTP_PORT}"

ALLOY_OTLP_GRPC = ServiceRef(
    name=ALLOY_NAME,
    port=Port(name="otlp-grpc", number=OTLP_GRPC_PORT),
    pods=Pods(namespace=ALLOY_NAMESPACE, labels=tuple(ALLOY_LABELS.items())),
)
ALLOY_OTLP_HTTP = ServiceRef(
    name=ALLOY_NAME,
    port=Port(name="otlp-http", number=OTLP_HTTP_PORT),
    pods=Pods(namespace=ALLOY_NAMESPACE, labels=tuple(ALLOY_LABELS.items())),
)


def service_environment(namespace: str, service_name: str, *, endpoint: str = OTLP_GRPC_ENDPOINT) -> dict[str, str]:
    """The SDK's standard environment contract for one long-lived Agentplane service."""
    return {
        "OTEL_SERVICE_NAME": service_name,
        "OTEL_RESOURCE_ATTRIBUTES": f"service.namespace=agentplane,deployment.environment.name={namespace}",
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
        "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
        "OTEL_TRACES_SAMPLER": "parentbased_always_on",
    }
